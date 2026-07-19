from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from .candidate_evidence import (
    DEFAULT_CANDIDATE_EVIDENCE_PATH,
    candidate_evidence_checksum,
    load_candidate_evidence,
)


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return bool(conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
    ).fetchone())


def show_evidence_map(
    conn: sqlite3.Connection,
    job_id: int,
    candidate_evidence_path: str | Path = DEFAULT_CANDIDATE_EVIDENCE_PATH,
) -> dict[str, Any]:
    job = conn.execute(
        "SELECT id, title, company, location, canonical_job_url FROM jobs WHERE id = ?",
        (job_id,),
    ).fetchone()
    if not job:
        raise KeyError(f"unknown job id: {job_id}")
    if not _table_exists(conn, "job_evidence_mapping_runs"):
        return {"job": dict(job), "mapping": None, "freshness": {"stale": None, "reasons": ["mapping_schema_absent"]}}
    run = conn.execute(
        "SELECT * FROM job_evidence_mapping_runs WHERE job_id = ? ORDER BY created_at DESC, id DESC LIMIT 1",
        (job_id,),
    ).fetchone()
    if not run:
        return {"job": dict(job), "mapping": None, "freshness": {"stale": None, "reasons": ["not_mapped"]}}

    current_job = conn.execute(
        """
        SELECT snapshots.content_checksum
        FROM job_current_fields current
        JOIN job_source_snapshots snapshots ON snapshots.id = current.source_snapshot_id
        WHERE current.job_id = ? AND current.field_name = 'job_description'
        """,
        (job_id,),
    ).fetchone()
    current_candidate_checksum = candidate_evidence_checksum(load_candidate_evidence(candidate_evidence_path))
    reasons: list[str] = []
    if not current_job or current_job["content_checksum"] != run["job_content_checksum"]:
        reasons.append("job_content_changed")
    if current_candidate_checksum != run["candidate_evidence_checksum"]:
        reasons.append("candidate_evidence_changed")

    rows = conn.execute(
        """
        SELECT requirements.*, mappings.assessment,
               mappings.supporting_claim_ids_json, mappings.supporting_claims_json,
               mappings.verified_leaf_claim_ids_json, mappings.verified_leaf_claims_json,
               mappings.unsupported_gap_claim_ids_json, mappings.unsupported_gap_claims_json,
               mappings.explanation, mappings.mapping_confidence, mappings.human_review_flag
        FROM job_requirements requirements
        JOIN job_requirement_mappings mappings ON mappings.requirement_row_id = requirements.id
        WHERE requirements.run_id = ?
        ORDER BY requirements.sequence_number
        """,
        (run["id"],),
    ).fetchall()
    requirements = []
    for row in rows:
        calibration = None
        proposal = None
        human_review = None
        if _table_exists(conn, "job_requirement_calibrations"):
            calibration = conn.execute(
                "SELECT * FROM job_requirement_calibrations WHERE requirement_row_id=? ORDER BY id DESC LIMIT 1",
                (row["id"],),
            ).fetchone()
        if calibration and calibration["ai_proposal_id"]:
            proposal = conn.execute(
                "SELECT * FROM job_requirement_ai_proposals WHERE id=?",
                (calibration["ai_proposal_id"],),
            ).fetchone()
        if calibration and _table_exists(conn, "job_requirement_human_reviews"):
            human_review = conn.execute(
                "SELECT * FROM job_requirement_human_reviews WHERE requirement_row_id=? ORDER BY id DESC LIMIT 1",
                (row["id"],),
            ).fetchone()
        review = None
        if calibration:
            review = {
                "deterministic_assessment": calibration["deterministic_assessment"],
                "ai_proposed_assessment": calibration["ai_proposed_assessment"],
                "machine_final_assessment": calibration["final_assessment"],
                "final_assessment": human_review["final_assessment"] if human_review else calibration["final_assessment"],
                "supporting_claim_ids": json.loads(
                    human_review["supporting_claim_ids_json"] if human_review else calibration["supporting_claim_ids_json"]
                ),
                "verified_leaf_claim_ids": json.loads(calibration["verified_leaf_claim_ids_json"]),
                "unsupported_gap_claim_ids": json.loads(
                    human_review["unsupported_gap_claim_ids_json"] if human_review else calibration["unsupported_gap_claim_ids_json"]
                ),
                "hard_constraint_failed": bool(calibration["hard_constraint_failed"]),
                "hard_constraint_reason": calibration["hard_constraint_reason"],
                "confidence": human_review["confidence"] if human_review else calibration["confidence"],
                "review_reason": human_review["review_reason"] if human_review else calibration["review_reason"],
                "reviewer": human_review["reviewer"] if human_review else None,
                "reviewed_at": human_review["reviewed_at"] if human_review else None,
                "review_status": "reviewed" if human_review else calibration["review_status"],
                "ai_proposal": (
                    {
                        "proposal_id": proposal["id"],
                        "provider": proposal["provider"],
                        "model": proposal["model"],
                        "mapper_version": proposal["mapper_version"],
                        "assessment": proposal["proposed_assessment"],
                        "supporting_claim_ids": json.loads(proposal["supporting_claim_ids_json"]),
                        "unsupported_gap_claim_ids": json.loads(proposal["unsupported_gap_claim_ids_json"]),
                        "explanation": proposal["explanation"],
                        "confidence": proposal["confidence"],
                        "validation_status": proposal["validation_status"],
                        "validation_errors": json.loads(proposal["validation_errors_json"]),
                        "created_at": proposal["created_at"],
                    }
                    if proposal else None
                ),
            }
        requirements.append({
            "requirement_id": row["requirement_id"],
            "source_text": row["source_text"],
            "source_span": {"start": row["source_span_start"], "end": row["source_span_end"]},
            "normalized_requirement": row["normalized_requirement"],
            "category": row["category"],
            "importance": row["importance"],
            "mandatory_preferred": row["requirement_status"],
            "explicitness": row["explicitness"],
            "extraction_confidence": row["extraction_confidence"],
            "provenance": {
                "source_url": row["source_url"],
                "source_snapshot_id": row["source_snapshot_id"],
                "job_content_checksum": row["job_content_checksum"],
            },
            "mapping": {
                "assessment": row["assessment"],
                "supporting_claim_ids": json.loads(row["supporting_claim_ids_json"]),
                "supporting_claims": json.loads(row["supporting_claims_json"]),
                "verified_leaf_claim_ids": json.loads(row["verified_leaf_claim_ids_json"]),
                "verified_leaf_claims": json.loads(row["verified_leaf_claims_json"]),
                "unsupported_gap_claim_ids": json.loads(row["unsupported_gap_claim_ids_json"]),
                "unsupported_gap_claims": json.loads(row["unsupported_gap_claims_json"]),
                "explanation": row["explanation"],
                "confidence": row["mapping_confidence"],
                "human_review": bool(row["human_review_flag"]),
            },
            "calibration": review,
        })
    return {
        "job": dict(job),
        "mapping": {
            "run_id": run["id"],
            "created_at": run["created_at"],
            "source_snapshot_id": run["source_snapshot_id"],
            "job_content_checksum": run["job_content_checksum"],
            "candidate_evidence_checksum": run["candidate_evidence_checksum"],
            "extraction_version": run["extraction_version"],
            "mapping_version": run["mapping_version"],
            "extraction_provider": run["extraction_provider"],
            "extraction_model": run["extraction_model"],
            "mapping_provider": run["mapping_provider"],
            "mapping_model": run["mapping_model"],
            "human_override": bool(run["human_override"]),
            "override_reason": run["override_reason"],
            "override_reviewer": run["override_reviewer"],
            "human_review_status": run["human_review_status"],
            "requirements": requirements,
        },
        "freshness": {
            "stale": bool(reasons),
            "reasons": reasons,
            "current_job_content_checksum": current_job["content_checksum"] if current_job else None,
            "current_candidate_evidence_checksum": current_candidate_checksum,
        },
    }
