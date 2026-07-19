from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from job_os.cli import load_json_messages, main
from job_os.enrichment import (
    FixtureRetriever,
    PublicHttpRetriever,
    classify_source_url,
    enrich_opportunities,
    extract_posting,
    is_official_ats_url,
    sanitized_html_to_text,
)
from job_os.parser import parse_alert_message
from job_os.store import connect, insert_job

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
