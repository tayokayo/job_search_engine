from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from .candidate_evidence import DEFAULT_CANDIDATE_EVIDENCE_PATH
from .opportunity_score_inspection import show_opportunity_score
from .opportunity_scoring import DEFAULT_SCORING_CONFIG_PATH


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return bool(
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
    )


def show_score_review_plan(
    conn: sqlite3.Connection,
    job_id: int,
    *,
    scoring_config_path: str | Path = DEFAULT_SCORING_CONFIG_PATH,
    candidate_evidence_path: str | Path = DEFAULT_CANDIDATE_EVIDENCE_PATH,
) -> dict[str, Any]:
    job = conn.execute(
        "SELECT id, title, company, location, canonical_job_url FROM jobs WHERE id=?",
        (job_id,),
    ).fetchone()
    if not job:
        raise KeyError(f"unknown job id: {job_id}")
    if not _table_exists(conn, "opportunity_score_review_plans"):
        return {
            "job": dict(job),
            "score": None,
            "review_plan": None,
            "freshness": {"stale": None, "reasons": ["review_plan_schema_absent"]},
        }
    row = conn.execute(
        """
        SELECT plans.*, scores.opportunity_fit_score,
               scores.evidence_confidence_score,
               scores.provisional_classification,
               scores.hard_constraint_failed, scores.scored_at
        FROM opportunity_fit_scores scores
        JOIN opportunity_score_review_plans plans ON plans.score_id=scores.id
        WHERE scores.job_id=?
        ORDER BY scores.id DESC, plans.id DESC LIMIT 1
        """,
        (job_id,),
    ).fetchone()
    if not row:
        return {
            "job": dict(job),
            "score": None,
            "review_plan": None,
            "freshness": {"stale": None, "reasons": ["not_planned"]},
        }
    score_inspection = show_opportunity_score(
        conn,
        job_id,
        scoring_config_path=scoring_config_path,
        candidate_evidence_path=candidate_evidence_path,
    )
    return {
        "job": dict(job),
        "score": {
            "score_id": row["score_id"],
            "opportunity_fit_score": row["opportunity_fit_score"],
            "evidence_confidence_score": row["evidence_confidence_score"],
            "provisional_classification": row["provisional_classification"],
            "hard_constraint_failed": bool(row["hard_constraint_failed"]),
            "scored_at": row["scored_at"],
        },
        "review_plan": {
            "review_plan_id": row["id"],
            "planning_version": row["planning_version"],
            "current_score": row["current_score"],
            "conservative_lower_bound": row["conservative_lower_bound"],
            "plausible_upper_bound": row["plausible_upper_bound"],
            "classification_range": json.loads(row["classification_range_json"]),
            "classification_stability": row["classification_stability"],
            "score_needed_next_band": row["score_needed_next_band"],
            "review_priority": row["review_priority"],
            "feasibility_results": json.loads(row["feasibility_results_json"]),
            "blockers": json.loads(row["blockers_json"]),
            "requirements_before_prioritization": row[
                "requirements_before_prioritization"
            ],
            "requirements_after_prioritization": row[
                "requirements_after_prioritization"
            ],
            "review_questions": json.loads(
                row["prioritized_review_items_json"]
            ),
            "created_at": row["created_at"],
        },
        "freshness": score_inspection["freshness"],
    }
