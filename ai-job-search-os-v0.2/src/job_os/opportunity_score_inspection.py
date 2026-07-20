from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from .candidate_evidence import DEFAULT_CANDIDATE_EVIDENCE_PATH
from .opportunity_scoring import (
    DEFAULT_SCORING_CONFIG_PATH,
    ScoringBlockedError,
    load_scoring_config,
    prepare_score_input,
)


CLASSIFICATION_LABELS = {
    "A": "A — Apply",
    "B": "B — Investigate",
    "C": "C — Ignore",
}


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return bool(
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
    )


def _json(value: str) -> Any:
    return json.loads(value)


def show_opportunity_score(
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
    if not _table_exists(conn, "opportunity_fit_scores"):
        return {
            "job": dict(job),
            "score": None,
            "freshness": {"stale": None, "reasons": ["scoring_schema_absent"]},
        }
    row = conn.execute(
        "SELECT * FROM opportunity_fit_scores WHERE job_id=? ORDER BY id DESC LIMIT 1",
        (job_id,),
    ).fetchone()
    if not row:
        return {
            "job": dict(job),
            "score": None,
            "freshness": {"stale": None, "reasons": ["not_scored"]},
        }

    reasons: list[str] = []
    current: dict[str, Any] = {}
    try:
        config = load_scoring_config(scoring_config_path)
        current["scoring_config_checksum"] = config.checksum
        if config.checksum != row["scoring_config_checksum"]:
            reasons.append("scoring_config_changed")
    except (OSError, ValueError) as exc:
        reasons.append("scoring_config_unavailable")
        current["scoring_config_error"] = str(exc)

    try:
        score_input = prepare_score_input(
            conn, job_id, candidate_evidence_path=candidate_evidence_path
        )
        current.update(
            {
                "mapping_run_id": score_input.mapping_run["id"],
                "job_content_checksum": score_input.mapping_run[
                    "job_content_checksum"
                ],
                "candidate_evidence_checksum": score_input.mapping_run[
                    "candidate_evidence_checksum"
                ],
                "mapping_version": score_input.mapping_run["mapping_version"],
                "calibration_versions": list(score_input.calibration_versions),
                "assessment_manifest_checksum": (
                    score_input.assessment_manifest_checksum
                ),
            }
        )
        comparisons = (
            ("mapping_run_id", row["mapping_run_id"], "mapping_run_changed"),
            (
                "job_content_checksum",
                row["job_content_checksum"],
                "job_content_changed",
            ),
            (
                "candidate_evidence_checksum",
                row["candidate_evidence_checksum"],
                "candidate_evidence_changed",
            ),
            ("mapping_version", row["mapping_version"], "mapping_version_changed"),
            (
                "assessment_manifest_checksum",
                row["assessment_manifest_checksum"],
                "reviewed_assessment_changed",
            ),
        )
        for key, stored, reason in comparisons:
            if current[key] != stored:
                reasons.append(reason)
        if current["calibration_versions"] != _json(
            row["calibration_versions_json"]
        ):
            reasons.append("calibration_version_changed")
    except ScoringBlockedError as exc:
        reasons.append(exc.code)
        current["scoring_block_reason"] = str(exc)
    except (OSError, ValueError) as exc:
        reasons.append("candidate_evidence_unavailable")
        current["candidate_evidence_error"] = str(exc)

    reasons = list(dict.fromkeys(reasons))
    return {
        "job": dict(job),
        "score": {
            "score_id": row["id"],
            "opportunity_fit_score": row["opportunity_fit_score"],
            "pre_gate_fit_score": row["pre_gate_fit_score"],
            "evidence_confidence_score": row["evidence_confidence_score"],
            "provisional_classification": row["provisional_classification"],
            "classification_label": CLASSIFICATION_LABELS[
                row["provisional_classification"]
            ],
            "hard_constraint_failed": bool(row["hard_constraint_failed"]),
            "hard_constraints": _json(row["hard_constraints_json"]),
            "dimension_breakdown": _json(row["dimension_breakdown_json"]),
            "requirement_contributions": _json(
                row["contribution_manifest_json"]
            ),
            "excluded_requirements": _json(row["excluded_requirements_json"]),
            "review_reasons": _json(row["review_reasons_json"]),
            "confidence_components": _json(row["confidence_components_json"]),
            "scored_at": row["scored_at"],
            "provenance": {
                "mapping_run_id": row["mapping_run_id"],
                "scoring_version": row["scoring_version"],
                "job_content_checksum": row["job_content_checksum"],
                "candidate_evidence_checksum": row[
                    "candidate_evidence_checksum"
                ],
                "mapping_version": row["mapping_version"],
                "calibration_versions": _json(
                    row["calibration_versions_json"]
                ),
                "scoring_config_checksum": row["scoring_config_checksum"],
                "assessment_manifest_checksum": row[
                    "assessment_manifest_checksum"
                ],
            },
        },
        "freshness": {
            "stale": bool(reasons),
            "reasons": reasons,
            "current": current,
        },
    }
