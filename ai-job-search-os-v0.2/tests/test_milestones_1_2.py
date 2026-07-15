from datetime import datetime, timezone

from job_os.parser import extract_job_id, parse_alert_message
from job_os.store import connect, insert_job
from job_os.url_utils import normalize_url


def fake_message():
    return {
        "id": "gmail-1",
        "payload": {"headers": [{"name": "Date", "value": "Tue, 14 Jul 2026 10:00:00 +0000"}]},
        "html": '<a href="https://www.linkedin.com/jobs/view/123456/?utm_source=email&trackingId=x">View job</a>',
        "text": "Senior Product Manager\nGrab\nSingapore\nhttps://www.linkedin.com/jobs/view/123456/?utm_source=email&trackingId=x",
    }


def test_url_normalization_removes_safe_tracking():
    assert normalize_url("https://www.linkedin.com/jobs/view/123/?utm_source=email&currentJobId=123&trackingId=x#frag") == "https://linkedin.com/jobs/view/123?currentJobId=123"


def test_extract_job_id():
    assert extract_job_id("https://linkedin.com/jobs/view/123456/?trk=email") == "123456"


def test_parse_alert_message_extracts_traceability_fields():
    jobs = parse_alert_message(fake_message())
    assert len(jobs) == 1
    assert jobs[0].gmail_message_id == "gmail-1"
    assert jobs[0].job_identifier == "123456"
    assert jobs[0].canonical_job_url == "https://linkedin.com/jobs/view/123456"


def test_ingestion_idempotency(tmp_path):
    conn = connect(tmp_path / "jobs.sqlite")
    job = parse_alert_message(fake_message())[0]
    assert insert_job(conn, job) is True
    assert insert_job(conn, job) is False
    assert conn.execute("select count(*) from jobs").fetchone()[0] == 1
