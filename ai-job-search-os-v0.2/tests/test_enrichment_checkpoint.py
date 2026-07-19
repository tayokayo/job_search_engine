from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import httpx
import pytest

from job_os.cli import load_json_messages, main
from job_os.enrichment import (
    FixtureRetriever,
    PublicHttpRetriever,
    UnsafePublicUrl,
    _identity_values_conflict,
    classify_source_url,
    eligibility_for,
    enrich_opportunities,
    extract_posting,
    is_official_ats_url,
    sanitized_html_to_text,
    validate_public_url,
)
from job_os.enrichment_inspection import show_enrichment
from job_os.parser import parse_alert_message
from job_os.store import connect, insert_job
from job_os.source_resolver import (
    CapturedSearchProvider,
    CompanySourceHint,
    OfficialSourceResolver,
    SourceSearchCandidate,
    _reviewed_organization_acronym_matches,
    search_queries,
)

ROOT = Path(__file__).parents[1]
ALERT_FIXTURE = ROOT / "tests" / "fixtures" / "linkedin_alerts.json"
RESPONSE_FIXTURE = ROOT / "tests" / "fixtures" / "enrichment_responses.json"
CANDIDATE_EVIDENCE = ROOT / "config" / "candidate_evidence.yaml"
SAMPLE_SOURCE_IDS = (
    "4441439743",  # Bangkok, official company and ATS
    "4418114331",  # Singapore, LinkedIn only
    "4441617824",  # Tokyo, unavailable
    "4440230946",  # Singapore, closed
    "4418598063",  # Bangkok, conflicting official title
    "4382237942",  # Bangkok, verified ATS
)


def ingested_database(path: Path):
    conn = connect(path)
    for message in load_json_messages(ALERT_FIXTURE):
        for job in parse_alert_message(message):
            insert_job(conn, job)
    return conn


def sample_job_ids(conn) -> list[int]:
    placeholders = ",".join("?" for _ in SAMPLE_SOURCE_IDS)
    rows = conn.execute(
        f"SELECT id, source_id FROM jobs WHERE source_id IN ({placeholders})",
        SAMPLE_SOURCE_IDS,
    ).fetchall()
    by_source = {row["source_id"]: row["id"] for row in rows}
    return [by_source[source_id] for source_id in SAMPLE_SOURCE_IDS]


@pytest.fixture
def enriched_sample(tmp_path):
    conn = ingested_database(tmp_path / "jobs.sqlite")
    retriever = FixtureRetriever.from_json(RESPONSE_FIXTURE)
    summary = enrich_opportunities(
        conn,
        retriever,
        job_ids=sample_job_ids(conn),
        refresh=False,
    )
    yield conn, retriever, summary
    conn.close()


def test_domain_recognition_and_sanitized_json_ld_extraction():
    assert is_official_ats_url("https://jobs.smartrecruiters.com/Example/123")
    assert classify_source_url("https://boards.greenhouse.io/example/jobs/123") == "official_ats"
    assert classify_source_url("https://hrmos.co/pages/example/jobs/123") == "official_ats"
    assert classify_source_url("https://careers.example.com/jobs/123") == "official_company"
    html = """
      <html><head>
      <script>alert('not content')</script>
      <script type="application/ld+json">
        {"@type":"JobPosting","title":"Director, Product",
         "hiringOrganization":{"name":"Example Co"},
         "jobLocation":{"address":{"addressLocality":"Tokyo"}},
         "description":"Responsibilities\\nLead the roadmap.\\nQualifications\\nTen years of experience."}
      </script></head><body><nav>Navigation</nav><main>Visible posting</main></body></html>
    """
    extracted = extract_posting(html, "https://careers.example.com/jobs/123")
    assert extracted.fields["job_title"] == "Director, Product"
    assert extracted.fields["company"] == "Example Co"
    assert extracted.fields["location"] == "Tokyo"
    assert extracted.fields["responsibilities"] == ["Lead the roadmap."]
    assert "Navigation" not in sanitized_html_to_text(html)
    assert "alert(" not in sanitized_html_to_text(html)


def test_linkedin_extraction_excludes_volatile_page_chrome():
    description = """
    <div class="description__text description__text--rich">
      <h2>About the role</h2>
      <p>Lead product strategy for the Bangkok team.</p>
      <h2>Qualifications</h2>
      <p>Eight years of product leadership experience.</p>
    </div>
    """
    first = extract_posting(
        f"<main><p>17 applicants</p>{description}<aside>Suggested role A</aside></main>",
        "https://linkedin.com/jobs/view/123",
    )
    second = extract_posting(
        f"<main><p>18 applicants</p>{description}<aside>Suggested role B</aside></main>",
        "https://linkedin.com/jobs/view/123",
    )
    assert first.fields == second.fields
    assert first.sanitized_text != second.sanitized_text
    assert "applicants" not in first.fields["job_description"]


def test_six_real_alert_derived_opportunities_cover_all_outcomes(enriched_sample):
    _, _, summary = enriched_sample
    assert summary["attempted"] == 6
    assert summary["verification_status_counts"] == {
        "closed": 1,
        "conflicting": 1,
        "linkedin_only": 1,
        "unavailable": 1,
        "verified_ats": 1,
        "verified_official": 1,
    }
    assert summary["source_type_counts"] == {
        "alert_email": 6,
        "linkedin": 6,
        "official_ats": 2,
        "official_company": 2,
    }
    assert summary["official_posting_matches"] == 3
    assert summary["official_posting_match_rate"] == pytest.approx(0.5)
    assert summary["complete_descriptions"] == 4
    assert summary["complete_description_rate"] == pytest.approx(4 / 6)


def test_official_company_and_ats_sources_beat_linkedin_and_alert(enriched_sample):
    conn, _, _ = enriched_sample
    jnj = conn.execute(
        "SELECT id FROM jobs WHERE source_id = '4441439743'"
    ).fetchone()[0]
    selected_title = conn.execute(
        """
        SELECT snapshots.source_type, snapshots.source_url
        FROM job_current_fields AS current
        JOIN job_source_snapshots AS snapshots
          ON snapshots.id = current.source_snapshot_id
        WHERE current.job_id = ? AND current.field_name = 'job_title'
        """,
        (jnj,),
    ).fetchone()
    assert tuple(selected_title) == (
        "official_company",
        "https://careers.jnj.example/jobs/sea-na-4441439743",
    )
    shopee = conn.execute(
        "SELECT id FROM jobs WHERE source_id = '4382237942'"
    ).fetchone()[0]
    shopee_source = conn.execute(
        """
        SELECT snapshots.source_type
        FROM job_current_fields AS current
        JOIN job_source_snapshots AS snapshots
          ON snapshots.id = current.source_snapshot_id
        WHERE current.job_id = ? AND current.field_name = 'job_description'
        """,
        (shopee,),
    ).fetchone()[0]
    assert shopee_source == "official_ats"


def test_every_selected_field_has_source_url_and_retrieval_time(enriched_sample):
    conn, _, _ = enriched_sample
    selected_count = conn.execute("SELECT COUNT(*) FROM job_current_fields").fetchone()[0]
    traced_count = conn.execute(
        """
        SELECT COUNT(*)
        FROM job_current_fields AS current
        JOIN job_source_snapshots AS snapshots
          ON snapshots.id = current.source_snapshot_id
        WHERE snapshots.source_url != '' AND snapshots.retrieved_at != ''
        """
    ).fetchone()[0]
    assert selected_count > 0
    assert traced_count == selected_count


def test_repeated_refresh_creates_no_duplicate_source_or_field_records(enriched_sample):
    conn, retriever, _ = enriched_sample
    before = {
        table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in ("job_source_snapshots", "job_field_values")
    }
    second = enrich_opportunities(
        conn,
        retriever,
        job_ids=sample_job_ids(conn),
        refresh=True,
    )
    after = {
        table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in before
    }
    assert second["attempted"] == 6
    assert after == before


def test_source_snapshots_are_database_immutable(enriched_sample):
    conn, _, _ = enriched_sample
    snapshot_id = conn.execute("SELECT MIN(id) FROM job_source_snapshots").fetchone()[0]
    with pytest.raises(sqlite3.DatabaseError, match="snapshots are immutable"):
        conn.execute(
            "UPDATE job_source_snapshots SET content_text = 'changed' WHERE id = ?",
            (snapshot_id,),
        )
    conn.rollback()


def test_accessible_page_without_job_content_is_partial_with_reason(tmp_path):
    conn = ingested_database(tmp_path / "jobs.sqlite")
    job_id = conn.execute(
        "SELECT id FROM jobs WHERE source_id = '4441617824'"
    ).fetchone()[0]
    url = "https://linkedin.com/jobs/view/4441617824"
    retriever = FixtureRetriever(
        {url: {"status_code": 200, "body": "<html><body><main>Generic page</main></body></html>"}}
    )
    result = enrich_opportunities(conn, retriever, job_ids=[job_id])
    assert result["verification_status_counts"] == {"partial": 1}
    assert "no extractable job posting content" in result["failures"][0]["failure_reason"]
    conn.close()


def test_refresh_adds_new_immutable_snapshot_only_when_content_changes(enriched_sample):
    conn, retriever, _ = enriched_sample
    job_id = conn.execute(
        "SELECT id FROM jobs WHERE source_id = '4441439743'"
    ).fetchone()[0]
    url = "https://careers.jnj.example/jobs/sea-na-4441439743"
    before_rows = conn.execute(
        "SELECT id, content_checksum FROM job_source_snapshots WHERE job_id = ? AND source_url = ?",
        (job_id, url),
    ).fetchall()
    retriever.responses[url]["body"] = retriever.responses[url]["body"].replace(
        "monitor delivery.", "monitor delivery and publish monthly decisions."
    )
    enrich_opportunities(conn, retriever, job_ids=[job_id], refresh=True)
    after_rows = conn.execute(
        "SELECT id, content_checksum FROM job_source_snapshots WHERE job_id = ? AND source_url = ?",
        (job_id, url),
    ).fetchall()
    assert len(before_rows) == 1
    assert len(after_rows) == 2
    assert before_rows[0]["content_checksum"] != after_rows[1]["content_checksum"]


def test_refresh_ignores_volatile_linkedin_page_chrome(tmp_path):
    conn = ingested_database(tmp_path / "jobs.sqlite")
    job_id = conn.execute(
        "SELECT id FROM jobs WHERE source_id = '4441439743'"
    ).fetchone()[0]
    url = "https://linkedin.com/jobs/view/4441439743"
    description = """
      <div class="description__text">
        <h2>About the role</h2><p>Lead product strategy in Bangkok.</p>
        <h2>Qualifications</h2><p>Eight years of leadership experience.</p>
      </div>
    """
    retriever = FixtureRetriever(
        {url: {"status_code": 200, "body": f"<main>17 applicants{description}</main>"}}
    )
    enrich_opportunities(conn, retriever, job_ids=[job_id])
    before = conn.execute(
        "SELECT COUNT(*) FROM job_source_snapshots WHERE job_id = ? AND source_url = ?",
        (job_id, url),
    ).fetchone()[0]
    retriever.responses[url]["body"] = f"<main>18 applicants{description}</main>"
    enrich_opportunities(conn, retriever, job_ids=[job_id], refresh=True)
    after = conn.execute(
        "SELECT COUNT(*) FROM job_source_snapshots WHERE job_id = ? AND source_url = ?",
        (job_id, url),
    ).fetchone()[0]
    assert before == after == 1


def test_unavailable_and_closed_are_distinct_and_original_jobs_are_preserved(enriched_sample):
    conn, _, _ = enriched_sample
    rows = conn.execute(
        """
        SELECT jobs.source_id, jobs.title, jobs.company, jobs.location,
               enrichment.verification_status, enrichment.failure_reason
        FROM jobs
        JOIN job_enrichments AS enrichment ON enrichment.job_id = jobs.id
        WHERE jobs.source_id IN ('4441617824', '4440230946')
        ORDER BY jobs.source_id
        """
    ).fetchall()
    values = {row["source_id"]: dict(row) for row in rows}
    assert values["4441617824"]["verification_status"] == "unavailable"
    assert "HTTP 404" in values["4441617824"]["failure_reason"]
    assert values["4441617824"]["title"].startswith("【田町】")
    assert values["4441617824"]["company"] == "doda (デューダ)"
    assert values["4441617824"]["location"] == "Tokyo"
    assert values["4440230946"]["verification_status"] == "closed"


def test_conflicting_values_are_retained_with_separate_provenance(enriched_sample):
    conn, _, summary = enriched_sample
    conflict = summary["conflicts"]
    assert len(conflict) == 1
    assert conflict[0]["company"] == "Grab"
    assert conflict[0]["conflict_fields"] == ["job_title"]
    job_id = conn.execute(
        "SELECT id FROM jobs WHERE source_id = '4418598063'"
    ).fetchone()[0]
    rows = conn.execute(
        """
        SELECT values_table.value_json, snapshots.source_type, snapshots.source_url
        FROM job_field_values AS values_table
        JOIN job_source_snapshots AS snapshots
          ON snapshots.id = values_table.source_snapshot_id
        WHERE values_table.job_id = ? AND values_table.field_name = 'job_title'
        ORDER BY snapshots.source_type
        """,
        (job_id,),
    ).fetchall()
    assert {json.loads(row["value_json"]) for row in rows} == {
        "People Operations Business Partner",
        "Senior People Operations Business Partner",
    }
    assert all(row["source_url"] for row in rows)


def test_enrichment_cli_does_not_touch_gmail_or_candidate_evidence(
    tmp_path, monkeypatch, capsys
):
    database = tmp_path / "jobs.sqlite"
    conn = ingested_database(database)
    job_id = conn.execute(
        "SELECT id FROM jobs WHERE source_id = '4441439743'"
    ).fetchone()[0]
    conn.close()
    evidence_before = hashlib.sha256(CANDIDATE_EVIDENCE.read_bytes()).hexdigest()

    def forbidden_gmail_call(*_args, **_kwargs):
        raise AssertionError("Gmail must not be called by enrichment")

    monkeypatch.setattr("job_os.cli.gmail_service", forbidden_gmail_call)
    main(
        [
            "enrich",
            "--db",
            str(database),
            "--job-id",
            str(job_id),
            "--responses-json",
            str(RESPONSE_FIXTURE),
        ]
    )
    output = json.loads(capsys.readouterr().out)
    evidence_after = hashlib.sha256(CANDIDATE_EVIDENCE.read_bytes()).hexdigest()
    assert output["attempted"] == 1
    assert evidence_after == evidence_before


def test_default_http_retriever_carries_no_authentication_or_cookie_headers():
    retriever = PublicHttpRetriever(timeout_seconds=0.1)
    try:
        headers = {key.lower() for key in retriever._client.headers}
        assert "authorization" not in headers
        assert "cookie" not in headers
    finally:
        retriever.close()


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "ftp://example.com/job",
        "http://localhost/job",
        "http://127.0.0.1/job",
        "http://169.254.169.254/latest/meta-data",
        "http://[::1]/job",
    ],
)
def test_public_url_validation_rejects_unsupported_and_private_targets(url):
    with pytest.raises(UnsafePublicUrl):
        validate_public_url(url)


def test_http_retriever_rejects_untrusted_redirect_and_oversized_response(monkeypatch):
    monkeypatch.setattr(
        "job_os.enrichment.socket.getaddrinfo",
        lambda *_args, **_kwargs: [(2, 1, 6, "", ("93.184.216.34", 443))],
    )

    def redirect_handler(request):
        return httpx.Response(
            302,
            headers={"Location": "https://untrusted.example/job"},
            request=request,
        )

    retriever = PublicHttpRetriever(timeout_seconds=0.1)
    retriever._client.close()
    retriever._client = httpx.Client(transport=httpx.MockTransport(redirect_handler))
    redirected = retriever.retrieve("https://careers.example.com/job")
    retriever.close()
    assert redirected.retrieval_status == "security_rejected"
    assert redirected.failure_reason == "untrusted_redirect"

    def oversized_handler(request):
        return httpx.Response(
            200,
            headers={"Content-Type": "text/html", "Content-Length": "500"},
            content=b"x" * 500,
            request=request,
        )

    retriever = PublicHttpRetriever(timeout_seconds=0.1, max_response_bytes=100)
    retriever._client.close()
    retriever._client = httpx.Client(transport=httpx.MockTransport(oversized_handler))
    oversized = retriever.retrieve("https://careers.example.com/job")
    retriever.close()
    assert oversized.retrieval_status == "security_rejected"
    assert oversized.failure_reason == "response_too_large"


def test_http_retriever_rejects_cross_provider_ats_redirect(monkeypatch):
    monkeypatch.setattr(
        "job_os.enrichment.socket.getaddrinfo",
        lambda *_args, **_kwargs: [(2, 1, 6, "", ("93.184.216.34", 443))],
    )

    def handler(request):
        return httpx.Response(
            302,
            headers={"Location": "https://boards.greenhouse.io/example/jobs/1"},
            request=request,
        )

    retriever = PublicHttpRetriever(timeout_seconds=0.1)
    retriever._client.close()
    retriever._client = httpx.Client(
        transport=httpx.MockTransport(handler), follow_redirects=False
    )
    result = retriever.retrieve("https://jobs.smartrecruiters.com/Example/1")
    retriever.close()
    assert result.retrieval_status == "security_rejected"
    assert result.failure_reason == "untrusted_redirect"


def test_http_retriever_ignores_script_only_captcha_references(monkeypatch):
    def handler(request):
        return httpx.Response(
            200,
            text=(
                "<html><head><script>const captchaProvider = true;</script></head>"
                "<body><h1>Public job posting</h1></body></html>"
            ),
            request=request,
        )

    retriever = PublicHttpRetriever()
    retriever._client.close()
    retriever._client = httpx.Client(
        transport=httpx.MockTransport(handler), follow_redirects=False
    )
    monkeypatch.setattr(
        "job_os.enrichment.socket.getaddrinfo",
        lambda *args, **kwargs: [(None, None, None, None, ("93.184.216.34", 443))],
    )
    result = retriever.retrieve("https://careers.example.com/job/1")
    retriever.close()
    assert result.retrieval_status == "success"


def test_http_retriever_recognizes_visible_access_challenge(monkeypatch):
    def handler(request):
        return httpx.Response(
            200,
            text="<html><body><h1>Verify you are human</h1></body></html>",
            request=request,
        )

    retriever = PublicHttpRetriever()
    retriever._client.close()
    retriever._client = httpx.Client(
        transport=httpx.MockTransport(handler), follow_redirects=False
    )
    monkeypatch.setattr(
        "job_os.enrichment.socket.getaddrinfo",
        lambda *args, **kwargs: [(None, None, None, None, ("93.184.216.34", 443))],
    )
    result = retriever.retrieve("https://careers.example.com/job/1")
    retriever.close()
    assert result.retrieval_status == "access_restricted"


def test_http_retriever_uses_visible_text_for_closed_status(monkeypatch):
    responses = iter(
        (
            "<script>const closedLabel = 'position has been filled';</script>"
            "<main>Public job posting</main>",
            "<main>This job is no longer available</main>",
        )
    )

    def handler(request):
        return httpx.Response(200, text=next(responses), request=request)

    retriever = PublicHttpRetriever()
    retriever._client.close()
    retriever._client = httpx.Client(
        transport=httpx.MockTransport(handler), follow_redirects=False
    )
    monkeypatch.setattr(
        "job_os.enrichment.socket.getaddrinfo",
        lambda *_args, **_kwargs: [(2, 1, 6, "", ("93.184.216.34", 443))],
    )
    live = retriever.retrieve("https://careers.example.com/job/1")
    closed = retriever.retrieve("https://careers.example.com/job/1")
    retriever.close()
    assert live.retrieval_status == "success"
    assert closed.retrieval_status == "closed"


def test_identity_conflicts_treat_bounded_geography_variants_as_equivalent():
    assert not _identity_values_conflict("location", {"bangkok", "bangkokthailand"})
    assert not _identity_values_conflict("company", {"grab", "grabholdings"})
    assert _identity_values_conflict("location", {"bangkok", "singapore"})
    assert _identity_values_conflict(
        "job_title", {"peopleoperationsbusinesspartner", "seniorpeopleoperationsbusinesspartner"}
    )


def test_organization_matching_supports_reviewable_legal_name_acronyms():
    assert _reviewed_organization_acronym_matches(
        "UOB", "1011 United Overseas Bank Ltd"
    )
    assert not _reviewed_organization_acronym_matches("UOB", "Unified Online Ltd")


def test_resolver_records_accepted_and_rejected_candidates_idempotently(tmp_path):
    conn = ingested_database(tmp_path / "jobs.sqlite")
    job = conn.execute(
        "SELECT * FROM jobs WHERE source_id = '4441439743'"
    ).fetchone()
    responses = json.loads(RESPONSE_FIXTURE.read_text())["responses"]
    retriever = FixtureRetriever(responses)
    candidates = (
        SourceSearchCandidate(
            "https://careers.jnj.example/jobs/sea-na-4441439743",
            "captured_search",
            "official query",
            1,
        ),
        SourceSearchCandidate(
            "https://careers.grab.example/jobs/people-partner-4418598063",
            "captured_search",
            "wrong company query",
            2,
        ),
        SourceSearchCandidate(
            "http://127.0.0.1/internal",
            "captured_search",
            "unsafe query",
            3,
        ),
        SourceSearchCandidate(
            "https://unknown.example/job",
            "captured_search",
            "unknown query",
            4,
        ),
        SourceSearchCandidate(
            "https://linkedin.com/jobs/view/4441439743",
            "captured_search",
            "linkedin result",
            5,
        ),
    )
    provider = CapturedSearchProvider({"4441439743": candidates})
    hint = CompanySourceHint(
        reviewed=True,
        official_domains=("careers.jnj.example", "careers.grab.example"),
        ats_domains=(),
        candidate_urls=(),
    )
    resolver = OfficialSourceResolver(
        retriever,
        search_provider=provider,
        hints={"johnsonjohnsonmedtech": hint},
    )
    resolver.resolve(conn, job)
    first_count = conn.execute(
        "SELECT COUNT(*) FROM job_source_candidates WHERE job_id = ?", (job["id"],)
    ).fetchone()[0]
    resolver.resolve(conn, job)
    second_count = conn.execute(
        "SELECT COUNT(*) FROM job_source_candidates WHERE job_id = ?", (job["id"],)
    ).fetchone()[0]
    decisions = dict(
        conn.execute(
            "SELECT candidate_url, decision_reason FROM job_source_candidates WHERE job_id = ?",
            (job["id"],),
        ).fetchall()
    )
    assert first_count == second_count == 5
    assert decisions[
        "https://careers.jnj.example/jobs/sea-na-4441439743"
    ] == "identity_verified"
    assert decisions[
        "https://careers.grab.example/jobs/people-partner-4418598063"
    ] == "company_mismatch"
    assert decisions["http://127.0.0.1/internal"] == "private_network_target"
    assert decisions["https://unknown.example/job"] == "unreviewed_or_unrecognized_domain"
    assert decisions[
        "https://linkedin.com/jobs/view/4441439743"
    ] == "linkedin_not_official_candidate"
    columns = {
        row[1] for row in conn.execute("PRAGMA table_info(job_source_candidates)")
    }
    assert "snippet" not in columns
    conn.close()


def test_search_queries_use_company_title_location_identifier_and_reviewed_domains(tmp_path):
    conn = ingested_database(tmp_path / "jobs.sqlite")
    job = conn.execute(
        "SELECT * FROM jobs WHERE source_id = '4441439743'"
    ).fetchone()
    hint = CompanySourceHint(
        reviewed=True,
        official_domains=("careers.jnj.com",),
        ats_domains=("jnj.wd5.myworkdayjobs.com",),
        candidate_urls=(),
    )
    queries = search_queries(job, hint)
    assert all(job["company"] in query for query in queries)
    assert all(job["title"] in query for query in queries)
    assert all(job["location"] in query for query in queries)
    assert all(job["source_id"] in query for query in queries)
    assert any("site:careers.jnj.com" in query for query in queries)
    conn.close()


@pytest.mark.parametrize(
    ("status", "complete", "expected"),
    [
        ("verified_official", True, "eligible"),
        ("verified_ats", True, "eligible"),
        ("linkedin_only", True, "conditionally_eligible"),
        ("partial", False, "manual_review"),
        ("conflicting", True, "manual_review"),
        ("unavailable", False, "ineligible"),
        ("closed", False, "ineligible"),
    ],
)
def test_eligibility_policy_is_non_numeric_and_deterministic(status, complete, expected):
    decision, reason = eligibility_for(status, complete)
    assert decision == expected
    assert status in reason or "official" in reason or "LinkedIn" in reason


def test_inspection_is_read_only_and_excludes_mailbox_and_candidate_private_data(
    enriched_sample, tmp_path, capsys
):
    conn, _, _ = enriched_sample
    job_id = conn.execute(
        "SELECT id FROM jobs WHERE source_id = '4441439743'"
    ).fetchone()[0]
    database_path = Path(conn.execute("PRAGMA database_list").fetchone()[2])
    before = hashlib.sha256(database_path.read_bytes()).hexdigest()
    inspected = show_enrichment(conn, job_id)
    assert inspected["verification"]["status"] == "verified_official"
    assert inspected["eligibility"]["decision"] == "eligible"
    assert inspected["selected_fields"]["job_title"]["source"]["source_url"]
    assert inspected["sources"][0]["content_checksum"]
    serialized = json.dumps(inspected)
    assert "raw_mime" not in serialized
    assert "gmail_message_id" not in serialized
    assert "candidate_evidence" not in serialized
    conn.commit()
    after = hashlib.sha256(database_path.read_bytes()).hexdigest()
    assert before == after

    main(["show-enrichment", "--job-id", str(job_id), "--db", str(database_path)])
    cli_result = json.loads(capsys.readouterr().out)
    assert cli_result["job"]["id"] == job_id


def test_inspection_handles_a_pre_enrichment_database(tmp_path):
    database_path = tmp_path / "pre-enrichment.sqlite"
    conn = sqlite3.connect(database_path)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE jobs (
          id INTEGER PRIMARY KEY,
          source_id TEXT NOT NULL,
          canonical_job_url TEXT,
          title TEXT NOT NULL,
          company TEXT NOT NULL,
          location TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        INSERT INTO jobs(id, source_id, canonical_job_url, title, company, location)
        VALUES (1, '4441439743', 'https://linkedin.com/jobs/view/4441439743',
                'Sr. Manager, SEA NA', 'Johnson & Johnson MedTech', 'Bangkok')
        """
    )
    conn.commit()
    inspected = show_enrichment(conn, 1)
    assert inspected["verification"] is None
    assert inspected["eligibility"] is None
    assert inspected["selected_fields"] == {}
    assert inspected["alternative_values"] == {}
    assert inspected["sources"] == []
    assert inspected["source_candidates"] == []
    assert inspected["failures"] == []
