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

CREATE TABLE IF NOT EXISTS opportunity_fit_scores (
  id INTEGER PRIMARY KEY,
  job_id INTEGER NOT NULL REFERENCES jobs(id),
  mapping_run_id INTEGER NOT NULL REFERENCES job_evidence_mapping_runs(id),
  scoring_version TEXT NOT NULL,
  job_content_checksum TEXT NOT NULL,
  candidate_evidence_checksum TEXT NOT NULL,
  mapping_version TEXT NOT NULL,
  calibration_versions_json TEXT NOT NULL,
  scoring_config_checksum TEXT NOT NULL,
  assessment_manifest_checksum TEXT NOT NULL,
  opportunity_fit_score REAL NOT NULL CHECK(opportunity_fit_score >= 0 AND opportunity_fit_score <= 100),
  pre_gate_fit_score REAL NOT NULL CHECK(pre_gate_fit_score >= 0 AND pre_gate_fit_score <= 100),
  evidence_confidence_score REAL NOT NULL CHECK(evidence_confidence_score >= 0 AND evidence_confidence_score <= 100),
  provisional_classification TEXT NOT NULL CHECK(provisional_classification IN ('A', 'B', 'C')),
  hard_constraint_failed INTEGER NOT NULL DEFAULT 0,
  hard_constraints_json TEXT NOT NULL DEFAULT '[]',
  dimension_breakdown_json TEXT NOT NULL,
  contribution_manifest_json TEXT NOT NULL,
  excluded_requirements_json TEXT NOT NULL DEFAULT '[]',
  review_reasons_json TEXT NOT NULL DEFAULT '[]',
  confidence_components_json TEXT NOT NULL,
  scored_at TEXT NOT NULL,
  UNIQUE(
    job_id, scoring_version, job_content_checksum,
    candidate_evidence_checksum, mapping_version,
    scoring_config_checksum, assessment_manifest_checksum
  )
);
CREATE INDEX IF NOT EXISTS idx_opportunity_fit_scores_job
ON opportunity_fit_scores(job_id, scored_at);
CREATE TRIGGER IF NOT EXISTS protect_opportunity_fit_scores_update
BEFORE UPDATE ON opportunity_fit_scores
BEGIN
  SELECT RAISE(ABORT, 'opportunity fit scores are immutable');
END;
CREATE TRIGGER IF NOT EXISTS protect_opportunity_fit_scores_delete
BEFORE DELETE ON opportunity_fit_scores
BEGIN
  SELECT RAISE(ABORT, 'opportunity fit scores are immutable');
END;

CREATE TABLE IF NOT EXISTS opportunity_score_review_plans (
  id INTEGER PRIMARY KEY,
  score_id INTEGER NOT NULL REFERENCES opportunity_fit_scores(id),
  planning_version TEXT NOT NULL,
  scoring_config_checksum TEXT NOT NULL,
  current_score REAL NOT NULL,
  conservative_lower_bound REAL NOT NULL,
  plausible_upper_bound REAL NOT NULL,
  classification_range_json TEXT NOT NULL,
  classification_stability TEXT NOT NULL,
  score_needed_next_band REAL NOT NULL,
  review_priority TEXT NOT NULL CHECK(review_priority IN (
    'no_review_needed', 'optional_review', 'targeted_review', 'blocking_review'
  )),
  feasibility_results_json TEXT NOT NULL,
  blockers_json TEXT NOT NULL DEFAULT '[]',
  unresolved_manifest_json TEXT NOT NULL DEFAULT '[]',
  prioritized_review_items_json TEXT NOT NULL DEFAULT '[]',
  requirements_before_prioritization INTEGER NOT NULL,
  requirements_after_prioritization INTEGER NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE(score_id, planning_version, scoring_config_checksum)
);
CREATE INDEX IF NOT EXISTS idx_opportunity_score_review_plans_score
ON opportunity_score_review_plans(score_id, created_at);
CREATE TRIGGER IF NOT EXISTS protect_opportunity_score_review_plans_update
BEFORE UPDATE ON opportunity_score_review_plans
BEGIN
  SELECT RAISE(ABORT, 'opportunity score review plans are immutable');
END;
CREATE TRIGGER IF NOT EXISTS protect_opportunity_score_review_plans_delete
BEFORE DELETE ON opportunity_score_review_plans
BEGIN
  SELECT RAISE(ABORT, 'opportunity score review plans are immutable');
END;

CREATE TABLE IF NOT EXISTS companies (
  id TEXT PRIMARY KEY,
  canonical_name TEXT NOT NULL,
  legal_name TEXT,
  parent_company_id TEXT REFERENCES companies(id),
  identity_confidence REAL NOT NULL CHECK(identity_confidence >= 0 AND identity_confidence <= 1),
  identity_evidence_json TEXT NOT NULL DEFAULT '[]',
  identity_checksum TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_companies_canonical_name
ON companies(canonical_name COLLATE NOCASE);

CREATE TABLE IF NOT EXISTS company_aliases (
  company_id TEXT NOT NULL REFERENCES companies(id),
  alias TEXT NOT NULL,
  alias_kind TEXT NOT NULL CHECK(alias_kind IN ('alias', 'legal_name', 'seed_label')),
  normalized_alias TEXT NOT NULL,
  evidence_url TEXT,
  PRIMARY KEY(company_id, normalized_alias)
);
CREATE INDEX IF NOT EXISTS idx_company_aliases_normalized
ON company_aliases(normalized_alias);

CREATE TABLE IF NOT EXISTS company_domains (
  company_id TEXT NOT NULL REFERENCES companies(id),
  domain TEXT NOT NULL,
  domain_kind TEXT NOT NULL CHECK(domain_kind IN ('corporate', 'careers', 'investor_relations', 'operating_brand')),
  verified INTEGER NOT NULL DEFAULT 0,
  evidence_url TEXT,
  PRIMARY KEY(company_id, domain)
);

CREATE TABLE IF NOT EXISTS company_target_markets (
  company_id TEXT NOT NULL REFERENCES companies(id),
  market TEXT NOT NULL,
  source TEXT NOT NULL,
  PRIMARY KEY(company_id, market)
);

CREATE TABLE IF NOT EXISTS company_seed_imports (
  id INTEGER PRIMARY KEY,
  source_path TEXT NOT NULL,
  source_row INTEGER NOT NULL,
  source_company TEXT NOT NULL,
  market TEXT NOT NULL,
  seed_tier TEXT NOT NULL CHECK(seed_tier IN ('tier_1', 'tier_2')),
  imported_at TEXT NOT NULL,
  UNIQUE(source_path, source_row)
);
CREATE TABLE IF NOT EXISTS company_seed_import_links (
  seed_import_id INTEGER NOT NULL REFERENCES company_seed_imports(id),
  company_id TEXT NOT NULL REFERENCES companies(id),
  resolution_reason TEXT NOT NULL,
  PRIMARY KEY(seed_import_id, company_id)
);

CREATE TABLE IF NOT EXISTS job_company_resolutions (
  job_id INTEGER PRIMARY KEY REFERENCES jobs(id),
  named_company_id TEXT REFERENCES companies(id),
  underlying_company_id TEXT REFERENCES companies(id),
  relationship TEXT NOT NULL CHECK(relationship IN (
    'direct_employer', 'parent_company', 'official_recruitment_partner',
    'staffing_intermediary', 'job_board', 'unknown'
  )),
  underlying_company_unknown INTEGER NOT NULL DEFAULT 0,
  identity_confidence REAL NOT NULL CHECK(identity_confidence >= 0 AND identity_confidence <= 1),
  identity_evidence_json TEXT NOT NULL DEFAULT '[]',
  resolution_checksum TEXT NOT NULL,
  resolved_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS company_facts (
  id INTEGER PRIMARY KEY,
  fact_id TEXT NOT NULL,
  company_id TEXT NOT NULL REFERENCES companies(id),
  version INTEGER NOT NULL,
  dimension TEXT NOT NULL CHECK(dimension IN (
    'candidate_background_fit', 'operating_complexity',
    'product_operations_intersection', 'ai_transformation_relevance',
    'geographic_fit', 'international_environment',
    'future_role_likelihood', 'identity', 'hiring_activity'
  )),
  statement TEXT NOT NULL,
  source_url TEXT NOT NULL,
  source_type TEXT NOT NULL CHECK(source_type IN (
    'official_corporate', 'official_careers', 'investor_relations',
    'regulatory', 'official_announcement', 'reputable_reporting'
  )),
  published_date TEXT,
  retrieved_at TEXT NOT NULL,
  freshness_policy TEXT NOT NULL CHECK(freshness_policy IN (
    'business_model', 'strategy', 'leadership', 'hiring_activity'
  )),
  freshness_days INTEGER NOT NULL CHECK(freshness_days > 0),
  confidence REAL NOT NULL CHECK(confidence >= 0 AND confidence <= 1),
  status TEXT NOT NULL CHECK(status IN ('accepted', 'rejected')),
  rejection_reason TEXT,
  fit_value REAL CHECK(fit_value IS NULL OR (fit_value >= 0 AND fit_value <= 1)),
  fact_checksum TEXT NOT NULL,
  UNIQUE(company_id, fact_id, version),
  UNIQUE(company_id, fact_checksum)
);
CREATE INDEX IF NOT EXISTS idx_company_facts_company
ON company_facts(company_id, dimension, status);
CREATE TRIGGER IF NOT EXISTS protect_company_facts_update
BEFORE UPDATE ON company_facts
BEGIN
  SELECT RAISE(ABORT, 'company facts are immutable');
END;
CREATE TRIGGER IF NOT EXISTS protect_company_facts_delete
BEFORE DELETE ON company_facts
BEGIN
  SELECT RAISE(ABORT, 'company facts are immutable');
END;

CREATE TABLE IF NOT EXISTS company_desired_tier_history (
  id INTEGER PRIMARY KEY,
  company_id TEXT NOT NULL REFERENCES companies(id),
  desired_tier TEXT NOT NULL CHECK(desired_tier IN ('tier_1', 'tier_2', 'dynamic', 'none')),
  reason TEXT NOT NULL,
  actor TEXT NOT NULL,
  source_seed_import_id INTEGER REFERENCES company_seed_imports(id),
  event_checksum TEXT NOT NULL UNIQUE,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_company_desired_tier_history_company
ON company_desired_tier_history(company_id, id);
CREATE TRIGGER IF NOT EXISTS protect_company_desired_tier_history_update
BEFORE UPDATE ON company_desired_tier_history
BEGIN
  SELECT RAISE(ABORT, 'desired-company history is append-only');
END;
CREATE TRIGGER IF NOT EXISTS protect_company_desired_tier_history_delete
BEFORE DELETE ON company_desired_tier_history
BEGIN
  SELECT RAISE(ABORT, 'desired-company history is append-only');
END;

CREATE TABLE IF NOT EXISTS company_fit_scores (
  id INTEGER PRIMARY KEY,
  company_id TEXT NOT NULL REFERENCES companies(id),
  scoring_version TEXT NOT NULL,
  scoring_config_checksum TEXT NOT NULL,
  identity_checksum TEXT NOT NULL,
  facts_checksum TEXT NOT NULL,
  desired_tier_checksum TEXT NOT NULL,
  company_fit_score REAL CHECK(company_fit_score IS NULL OR (company_fit_score >= 0 AND company_fit_score <= 100)),
  company_confidence_score REAL NOT NULL CHECK(company_confidence_score >= 0 AND company_confidence_score <= 100),
  watch_recommendation TEXT NOT NULL CHECK(watch_recommendation IN (
    'priority_watch', 'active_watch', 'monitor', 'do_not_watch',
    'needs_research', 'identity_unresolved'
  )),
  dimension_breakdown_json TEXT NOT NULL,
  evidence_manifest_json TEXT NOT NULL,
  missing_research_json TEXT NOT NULL,
  stale_facts_json TEXT NOT NULL,
  conflict_facts_json TEXT NOT NULL,
  scored_at TEXT NOT NULL,
  UNIQUE(company_id, scoring_version, scoring_config_checksum, identity_checksum, facts_checksum, desired_tier_checksum)
);
CREATE INDEX IF NOT EXISTS idx_company_fit_scores_company
ON company_fit_scores(company_id, id);
CREATE TRIGGER IF NOT EXISTS protect_company_fit_scores_update
BEFORE UPDATE ON company_fit_scores
BEGIN
  SELECT RAISE(ABORT, 'company fit scores are immutable');
END;
CREATE TRIGGER IF NOT EXISTS protect_company_fit_scores_delete
BEFORE DELETE ON company_fit_scores
BEGIN
  SELECT RAISE(ABORT, 'company fit scores are immutable');
END;

CREATE TABLE IF NOT EXISTS company_watch_history (
  id INTEGER PRIMARY KEY,
  company_id TEXT NOT NULL REFERENCES companies(id),
  previous_state TEXT,
  new_state TEXT NOT NULL CHECK(new_state IN (
    'priority_watch', 'active_watch', 'monitor', 'do_not_watch',
    'needs_research', 'identity_unresolved'
  )),
  event_type TEXT NOT NULL CHECK(event_type IN (
    'seeded', 'promoted', 'demoted', 'manual', 'dynamic_added'
  )),
  trigger_type TEXT NOT NULL CHECK(trigger_type IN (
    'seed_import', 'a_opportunity', 'multiple_b_opportunities',
    'company_fit_threshold', 'manual', 'score_recommendation'
  )),
  reason TEXT NOT NULL,
  related_job_ids_json TEXT NOT NULL DEFAULT '[]',
  actor TEXT NOT NULL,
  event_checksum TEXT NOT NULL UNIQUE,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_company_watch_history_company
ON company_watch_history(company_id, id);
CREATE TRIGGER IF NOT EXISTS protect_company_watch_history_update
BEFORE UPDATE ON company_watch_history
BEGIN
  SELECT RAISE(ABORT, 'company watch history is append-only');
END;
CREATE TRIGGER IF NOT EXISTS protect_company_watch_history_delete
BEFORE DELETE ON company_watch_history
BEGIN
  SELECT RAISE(ABORT, 'company watch history is append-only');
END;
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
