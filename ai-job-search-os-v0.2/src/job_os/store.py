from __future__ import annotations

import sqlite3
from pathlib import Path

from .parser import ParsedJobAlert

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
  id INTEGER PRIMARY KEY,
  source TEXT NOT NULL,
  source_id TEXT NOT NULL,
  gmail_message_id TEXT NOT NULL,
  source_url TEXT,
  canonical_job_url TEXT,
  title TEXT NOT NULL,
  company TEXT NOT NULL,
  location TEXT NOT NULL,
  alert_timestamp TEXT NOT NULL,
  dedupe_key TEXT NOT NULL UNIQUE,
  created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_jobs_gmail_message_id ON jobs(gmail_message_id);
"""


def connect(path: str | Path = "job_os.sqlite") -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def dedupe_key(job: ParsedJobAlert) -> str:
    if job.job_identifier and job.job_identifier.isdigit():
        return f"linkedin-job-id:{job.job_identifier}"
    if job.canonical_job_url:
        return f"canonical-url:{job.canonical_job_url}"
    return "company-title-location:" + "|".join([job.company.lower().strip(), job.title.lower().strip(), job.location.lower().strip()])


def insert_job(conn: sqlite3.Connection, job: ParsedJobAlert) -> bool:
    cur = conn.execute(
        """
        INSERT OR IGNORE INTO jobs(source, source_id, gmail_message_id, source_url, canonical_job_url, title, company, location, alert_timestamp, dedupe_key)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        ("linkedin_gmail_alert", job.job_identifier, job.gmail_message_id, job.source_url, job.canonical_job_url, job.title, job.company, job.location, job.alert_timestamp.isoformat(), dedupe_key(job)),
    )
    conn.commit()
    return cur.rowcount == 1
