from __future__ import annotations

import ast
import json
from collections import Counter
from email.message import EmailMessage
from pathlib import Path

from job_os.cli import (
    load_json_messages,
    load_raw_mime_messages,
    main,
    proposed_gmail_query,
)
from job_os.gmail import credential_paths
from job_os.parser import parse_alert_message
from job_os.url_utils import normalize_url

FIXTURE = Path(__file__).parent / "fixtures" / "linkedin_alerts.json"


def fixture_data():
    return json.loads(FIXTURE.read_text())


def test_documented_gmail_path_environment_names_take_precedence(monkeypatch):
    monkeypatch.setenv("GMAIL_CREDENTIALS", "legacy-credentials.json")
    monkeypatch.setenv("GMAIL_TOKEN", "legacy-token.json")
    monkeypatch.setenv("GMAIL_CREDENTIALS_PATH", "documented-credentials.json")
    monkeypatch.setenv("GMAIL_TOKEN_PATH", "documented-token.json")
    assert credential_paths() == (
        "documented-credentials.json",
        "documented-token.json",
    )


def test_three_observed_formats_parse_exactly_17_jobs():
    source = fixture_data()
    messages = load_json_messages(FIXTURE)
    actual = []
    rejected = Counter()
    for raw, message in zip(source, messages, strict=True):
        jobs = parse_alert_message(message, rejected)
        expected = raw["expected_jobs"]
        assert len(jobs) == len(expected)
        for job, fields in zip(jobs, expected, strict=True):
            job_id, title, company, location = fields
            assert (
                job.job_identifier,
                job.title,
                job.company,
                job.location,
                job.canonical_job_url,
            ) == (
                job_id,
                title,
                company,
                location,
                f"https://linkedin.com/jobs/view/{job_id}",
            )
            actual.append(job)
    assert len(actual) == 17
    assert rejected == Counter(
        {
            "alert_management": 3,
            "duplicate_job_link": 1,
            "navigation": 3,
            "search": 3,
            "settings": 2,
            "unsubscribe": 2,
        }
    )


def test_malformed_and_navigation_links_never_create_records():
    message = {
        "id": "malformed",
        "payload": {"headers": []},
        "html": """
            <a href="https://linkedin.com/jobs/search?keywords=product">Search</a>
            <a href="https://linkedin.com/jobs/alerts">Manage alerts</a>
            <a href="https://linkedin.com/jobs/view/111">Title without company/location</a>
            <a href="https://notlinkedin.com/jobs/view/222"><span>Fake role</span><span>Fake Co · Tokyo</span></a>
            <a href="https://linkedin.com/settings">Settings</a>
            <a href="https://linkedin.com/job-alert-email-unsubscribe">Unsubscribe</a>
        """,
        "text": "",
    }
    rejected = Counter()
    assert parse_alert_message(message, rejected) == []
    assert rejected == Counter(
        {
            "alert_management": 1,
            "malformed_listing": 1,
            "search": 1,
            "settings": 1,
            "unsubscribe": 1,
        }
    )


def test_connector_markdown_uses_bounded_link_label():
    message = {
        "id": "connector-markdown",
        "payload": {"headers": []},
        "body": """
            [Search](https://linkedin.com/jobs/search?keywords=product)
            [Director, Product\nExample Co · Tokyo (Hybrid)\nFast growing](https://www.linkedin.com/comm/jobs/view/999/?trackingId=sample)
        """,
    }
    jobs = parse_alert_message(message)
    assert [(job.title, job.company, job.location) for job in jobs] == [
        ("Director, Product", "Example Co", "Tokyo")
    ]


def test_job_urls_discard_tracking_and_authentication_parameters():
    variants = [
        "https://www.linkedin.com/comm/jobs/view/123456/?trackingId=x&otpToken=temporary-secret",
        "https://linkedin.com/jobs/view/123456?refId=y&utm_source=email",
        "https://linkedin.com/jobs/collections/?currentJobId=123456&midToken=z",
    ]
    assert {normalize_url(url) for url in variants} == {
        "https://linkedin.com/jobs/view/123456"
    }
    normalized_search = normalize_url(
        "https://linkedin.com/jobs/search?keywords=product&otpToken=secret&trk=email"
    )
    assert normalized_search == "https://linkedin.com/jobs/search?keywords=product"


def test_raw_mime_is_an_explicit_attachment_skipping_input_mode(tmp_path):
    email = EmailMessage()
    email["From"] = "LinkedIn Job Alerts <jobalerts-noreply@linkedin.com>"
    email["Subject"] = "Product Director at Example Co"
    email["Date"] = "Sat, 18 Jul 2026 00:00:00 +0000"
    email.set_content("Plain-text fallback without a listing contract")
    email.add_alternative(
        '<a href="https://linkedin.com/jobs/view/777"><span>Product Director</span><span>Example Co · Tokyo</span></a>',
        subtype="html",
    )
    email.add_attachment("ignored", filename="ignore.txt")
    path = tmp_path / "alert.eml"
    path.write_bytes(email.as_bytes())
    messages = load_raw_mime_messages([str(path)])
    jobs = parse_alert_message(messages[0])
    assert [(job.job_identifier, job.title) for job in jobs] == [
        ("777", "Product Director")
    ]


def test_discovery_query_uses_address_and_stable_subject_patterns():
    query = proposed_gmail_query(
        Counter({"jobalerts-noreply@linkedin.com": 3}),
        Counter({"posted on": 2, "title at company": 1}),
    )
    assert query == (
        "newer_than:30d from:jobalerts-noreply@linkedin.com "
        '{subject:"posted on" subject:" at "} '
        "-has:attachment -in:spam -in:trash"
    )
    assert "LinkedIn Job Alerts" not in query


def test_cli_repeated_ingestion_inserts_17_then_17_duplicates(tmp_path, capsys):
    database = tmp_path / "jobs.sqlite"
    arguments = ["ingest", "--input-json", str(FIXTURE), "--db", str(database)]
    main(arguments)
    first = ast.literal_eval(capsys.readouterr().out.splitlines()[-1])
    main(arguments)
    second = ast.literal_eval(capsys.readouterr().out.splitlines()[-1])
    assert first["parsed"] == 17
    assert first["inserted"] == 17
    assert first["duplicates"] == 0
    assert second["parsed"] == 17
    assert second["inserted"] == 0
    assert second["duplicates"] == 17
    assert first["rejected_links"] == second["rejected_links"]


def test_sanitized_fixture_contains_no_mailbox_identity_or_live_tokens():
    contents = FIXTURE.read_text().lower()
    assert "gmail.com" not in contents
    assert "googlemail.com" not in contents
    assert "otptoken" not in contents
    assert "midtoken" not in contents
    assert "midsig" not in contents
