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

CREATE TABLE IF NOT EXISTS job_source_snapshots (
  id INTEGER PRIMARY KEY,
  job_id INTEGER NOT NULL REFERENCES jobs(id),
  source_url TEXT NOT NULL,
  source_type TEXT NOT NULL CHECK(source_type IN ('official_company', 'official_ats', 'linkedin', 'alert_email', 'other')),
  retrieved_at TEXT NOT NULL,
  http_status INTEGER,
  retrieval_status TEXT NOT NULL,
  verification_status TEXT CHECK(verification_status IS NULL OR verification_status IN ('verified_official', 'verified_ats', 'linkedin_only', 'partial', 'unavailable', 'closed', 'conflicting')),
  content_checksum TEXT NOT NULL,
  content_text TEXT NOT NULL,
  extracted_json TEXT NOT NULL,
  failure_reason TEXT,
  UNIQUE(job_id, source_url, content_checksum)
);
CREATE INDEX IF NOT EXISTS idx_job_source_snapshots_job_id ON job_source_snapshots(job_id);
CREATE TRIGGER IF NOT EXISTS protect_job_source_snapshots_update
BEFORE UPDATE ON job_source_snapshots
BEGIN
  SELECT RAISE(ABORT, 'job source snapshots are immutable');
END;
CREATE TRIGGER IF NOT EXISTS protect_job_source_snapshots_delete
BEFORE DELETE ON job_source_snapshots
BEGIN
  SELECT RAISE(ABORT, 'job source snapshots are immutable');
END;

CREATE TABLE IF NOT EXISTS job_source_state (
  job_id INTEGER NOT NULL REFERENCES jobs(id),
  source_url TEXT NOT NULL,
  source_type TEXT NOT NULL,
  last_checked_at TEXT NOT NULL,
  last_successfully_checked_at TEXT,
  http_status INTEGER,
  retrieval_status TEXT NOT NULL,
  failure_reason TEXT,
  current_snapshot_id INTEGER REFERENCES job_source_snapshots(id),
  PRIMARY KEY(job_id, source_url)
);

CREATE TABLE IF NOT EXISTS job_field_values (
  id INTEGER PRIMARY KEY,
  job_id INTEGER NOT NULL REFERENCES jobs(id),
  field_name TEXT NOT NULL,
  value_json TEXT NOT NULL,
  value_checksum TEXT NOT NULL,
  source_snapshot_id INTEGER NOT NULL REFERENCES job_source_snapshots(id),
  UNIQUE(job_id, field_name, value_checksum, source_snapshot_id)
);
CREATE INDEX IF NOT EXISTS idx_job_field_values_job_field ON job_field_values(job_id, field_name);

CREATE TABLE IF NOT EXISTS job_current_fields (
  job_id INTEGER NOT NULL REFERENCES jobs(id),
  field_name TEXT NOT NULL,
  value_json TEXT NOT NULL,
  source_snapshot_id INTEGER NOT NULL REFERENCES job_source_snapshots(id),
  selected_at TEXT NOT NULL,
  PRIMARY KEY(job_id, field_name)
);

CREATE TABLE IF NOT EXISTS job_enrichments (
  job_id INTEGER PRIMARY KEY REFERENCES jobs(id),
  verification_status TEXT NOT NULL CHECK(verification_status IN ('verified_official', 'verified_ats', 'linkedin_only', 'partial', 'unavailable', 'closed', 'conflicting')),
  official_posting_url TEXT,
  company_careers_url TEXT,
  complete_description INTEGER NOT NULL DEFAULT 0,
  conflict_fields_json TEXT NOT NULL DEFAULT '[]',
  last_attempted_at TEXT NOT NULL,
  last_successfully_checked_at TEXT,
  failure_reason TEXT,
  updated_at TEXT NOT NULL
);
"""


def connect(path: str | Path = "job_os.sqlite") -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
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
