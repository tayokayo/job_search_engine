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

CREATE TABLE IF NOT EXISTS job_source_candidates (
  id INTEGER PRIMARY KEY,
  job_id INTEGER NOT NULL REFERENCES jobs(id),
  candidate_url TEXT NOT NULL,
  domain TEXT NOT NULL,
  source_type TEXT NOT NULL CHECK(source_type IN ('official_company', 'official_ats', 'other')),
  discovery_method TEXT NOT NULL,
  provider TEXT NOT NULL,
  search_query TEXT,
  provider_rank INTEGER,
  discovered_at TEXT NOT NULL,
  evaluated_at TEXT NOT NULL,
  decision TEXT NOT NULL CHECK(decision IN ('accepted', 'rejected', 'pending')),
  decision_reason TEXT NOT NULL,
  confidence_reasons_json TEXT NOT NULL DEFAULT '[]',
  retrieval_status TEXT,
  http_status INTEGER,
  content_checksum TEXT,
  UNIQUE(job_id, candidate_url, provider)
);
CREATE INDEX IF NOT EXISTS idx_job_source_candidates_job_id
ON job_source_candidates(job_id);
CREATE INDEX IF NOT EXISTS idx_job_source_candidates_decision
ON job_source_candidates(decision, decision_reason);

CREATE TABLE IF NOT EXISTS job_eligibility_decisions (
  job_id INTEGER PRIMARY KEY REFERENCES jobs(id),
  decision TEXT NOT NULL CHECK(decision IN ('eligible', 'conditionally_eligible', 'manual_review', 'ineligible')),
  reason TEXT NOT NULL,
  verification_status TEXT NOT NULL,
  complete_description INTEGER NOT NULL,
  decided_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS job_evidence_mapping_runs (
  id INTEGER PRIMARY KEY,
  job_id INTEGER NOT NULL REFERENCES jobs(id),
  source_snapshot_id INTEGER NOT NULL REFERENCES job_source_snapshots(id),
  job_content_checksum TEXT NOT NULL,
  candidate_evidence_checksum TEXT NOT NULL,
  extraction_version TEXT NOT NULL,
  mapping_version TEXT NOT NULL,
  extraction_provider TEXT NOT NULL,
  extraction_model TEXT,
  mapping_provider TEXT NOT NULL,
  mapping_model TEXT,
  created_at TEXT NOT NULL,
  human_override INTEGER NOT NULL DEFAULT 0,
  override_reason TEXT,
  override_reviewer TEXT,
  human_review_status TEXT NOT NULL CHECK(human_review_status IN ('not_required', 'pending', 'reviewed')),
  UNIQUE(job_id, job_content_checksum, candidate_evidence_checksum, extraction_version, mapping_version)
);
CREATE INDEX IF NOT EXISTS idx_job_evidence_mapping_runs_job
ON job_evidence_mapping_runs(job_id, created_at);

CREATE TABLE IF NOT EXISTS job_requirements (
  id INTEGER PRIMARY KEY,
  run_id INTEGER NOT NULL REFERENCES job_evidence_mapping_runs(id),
  requirement_id TEXT NOT NULL,
  sequence_number INTEGER NOT NULL,
  source_text TEXT NOT NULL,
  source_span_start INTEGER NOT NULL,
  source_span_end INTEGER NOT NULL,
  normalized_requirement TEXT NOT NULL,
  category TEXT NOT NULL CHECK(category IN (
    'responsibilities', 'leadership_scope', 'seniority',
    'functional_experience', 'domain_experience', 'geography', 'language',
    'years_of_experience', 'education', 'technical_skills',
    'commercial_or_operational_ownership', 'other_explicit_constraints'
  )),
  importance TEXT NOT NULL CHECK(importance IN ('high', 'medium', 'low')),
  requirement_status TEXT NOT NULL CHECK(requirement_status IN ('mandatory', 'preferred', 'unspecified')),
  explicitness TEXT NOT NULL CHECK(explicitness IN ('explicit', 'inferred')),
  source_url TEXT NOT NULL,
  source_snapshot_id INTEGER NOT NULL REFERENCES job_source_snapshots(id),
  job_content_checksum TEXT NOT NULL,
  extraction_confidence REAL NOT NULL CHECK(extraction_confidence >= 0 AND extraction_confidence <= 1),
  UNIQUE(run_id, requirement_id),
  UNIQUE(run_id, sequence_number)
);
CREATE INDEX IF NOT EXISTS idx_job_requirements_run
ON job_requirements(run_id, sequence_number);

CREATE TABLE IF NOT EXISTS job_requirement_mappings (
  requirement_row_id INTEGER PRIMARY KEY REFERENCES job_requirements(id),
  assessment TEXT NOT NULL CHECK(assessment IN ('confirmed', 'partial', 'unsupported', 'contradicted', 'unknown')),
  supporting_claim_ids_json TEXT NOT NULL DEFAULT '[]',
  supporting_claims_json TEXT NOT NULL DEFAULT '[]',
  verified_leaf_claim_ids_json TEXT NOT NULL DEFAULT '[]',
  verified_leaf_claims_json TEXT NOT NULL DEFAULT '[]',
  unsupported_gap_claim_ids_json TEXT NOT NULL DEFAULT '[]',
  unsupported_gap_claims_json TEXT NOT NULL DEFAULT '[]',
  explanation TEXT NOT NULL,
  mapping_confidence REAL NOT NULL CHECK(mapping_confidence >= 0 AND mapping_confidence <= 1),
  human_review_flag INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS job_requirement_ai_proposals (
  id INTEGER PRIMARY KEY,
  requirement_row_id INTEGER NOT NULL REFERENCES job_requirements(id),
  provider TEXT NOT NULL,
  model TEXT NOT NULL,
  mapper_version TEXT NOT NULL,
  proposed_assessment TEXT NOT NULL CHECK(proposed_assessment IN ('confirmed', 'partial', 'unsupported', 'contradicted', 'unknown')),
  supporting_claim_ids_json TEXT NOT NULL DEFAULT '[]',
  unsupported_gap_claim_ids_json TEXT NOT NULL DEFAULT '[]',
  explanation TEXT NOT NULL,
  confidence REAL NOT NULL CHECK(confidence >= 0 AND confidence <= 1),
  raw_response_json TEXT NOT NULL,
  validation_status TEXT NOT NULL CHECK(validation_status IN ('accepted', 'rejected')),
  validation_errors_json TEXT NOT NULL DEFAULT '[]',
  created_at TEXT NOT NULL,
  UNIQUE(requirement_row_id, provider, model, mapper_version)
);
CREATE INDEX IF NOT EXISTS idx_job_requirement_ai_proposals_requirement
ON job_requirement_ai_proposals(requirement_row_id, created_at);

CREATE TABLE IF NOT EXISTS job_requirement_calibrations (
  id INTEGER PRIMARY KEY,
  requirement_row_id INTEGER NOT NULL REFERENCES job_requirements(id),
  calibration_version TEXT NOT NULL,
  deterministic_assessment TEXT NOT NULL CHECK(deterministic_assessment IN ('confirmed', 'partial', 'unsupported', 'contradicted', 'unknown')),
  ai_proposal_id INTEGER REFERENCES job_requirement_ai_proposals(id),
  ai_proposed_assessment TEXT CHECK(ai_proposed_assessment IS NULL OR ai_proposed_assessment IN ('confirmed', 'partial', 'unsupported', 'contradicted', 'unknown')),
  final_assessment TEXT NOT NULL CHECK(final_assessment IN ('confirmed', 'partial', 'unsupported', 'contradicted', 'unknown')),
  supporting_claim_ids_json TEXT NOT NULL DEFAULT '[]',
  verified_leaf_claim_ids_json TEXT NOT NULL DEFAULT '[]',
  unsupported_gap_claim_ids_json TEXT NOT NULL DEFAULT '[]',
  hard_constraint_failed INTEGER NOT NULL DEFAULT 0,
  hard_constraint_reason TEXT,
  confidence REAL NOT NULL CHECK(confidence >= 0 AND confidence <= 1),
  review_reason TEXT,
  review_status TEXT NOT NULL CHECK(review_status IN ('not_required', 'pending', 'reviewed')),
  created_at TEXT NOT NULL,
  UNIQUE(requirement_row_id, calibration_version)
);
CREATE INDEX IF NOT EXISTS idx_job_requirement_calibrations_review
ON job_requirement_calibrations(review_status, final_assessment);

CREATE TABLE IF NOT EXISTS job_requirement_human_reviews (
  id INTEGER PRIMARY KEY,
  requirement_row_id INTEGER NOT NULL REFERENCES job_requirements(id),
  calibration_id INTEGER NOT NULL REFERENCES job_requirement_calibrations(id),
  deterministic_assessment TEXT NOT NULL CHECK(deterministic_assessment IN ('confirmed', 'partial', 'unsupported', 'contradicted', 'unknown')),
  ai_proposed_assessment TEXT CHECK(ai_proposed_assessment IS NULL OR ai_proposed_assessment IN ('confirmed', 'partial', 'unsupported', 'contradicted', 'unknown')),
  final_assessment TEXT NOT NULL CHECK(final_assessment IN ('confirmed', 'partial', 'unsupported', 'contradicted', 'unknown')),
  supporting_claim_ids_json TEXT NOT NULL DEFAULT '[]',
  unsupported_gap_claim_ids_json TEXT NOT NULL DEFAULT '[]',
  hard_constraint_failed INTEGER NOT NULL DEFAULT 0,
  confidence REAL NOT NULL CHECK(confidence >= 0 AND confidence <= 1),
  review_reason TEXT NOT NULL,
  reviewer TEXT NOT NULL,
  reviewed_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_job_requirement_human_reviews_requirement
ON job_requirement_human_reviews(requirement_row_id, reviewed_at);
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
