from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

import yaml

from .candidate_evidence import (
    DEFAULT_CANDIDATE_EVIDENCE_PATH,
    candidate_evidence_checksum,
    load_candidate_evidence,
)
from .requirement_mapping import EvidenceFact, _evidence_facts

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SCORING_CONFIG_PATH = PROJECT_ROOT / "config" / "scoring.yaml"
SCORING_VERSION = "opportunity-fit-v1.1"
ASSESSMENTS = {"confirmed", "partial", "unsupported", "contradicted", "unknown"}


class ScoringBlockedError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class ScoringConfig:
    version: str
    assessment_values: Mapping[str, float]
    importance_multipliers: Mapping[str, float]
    dimensions: Mapping[str, Mapping[str, Any]]
    feasibility_categories: tuple[str, ...]
    target_geographies: Mapping[str, tuple[str, ...]]
    duplicate_similarity_threshold: float
    confidence: Mapping[str, Any]
    classification: Mapping[str, float]
    review_planning: Mapping[str, Any]
    checksum: str


@dataclass(frozen=True)
class RequirementInput:
    requirement_row_id: int
    requirement_id: str
    sequence_number: int
    source_text: str
    normalized_requirement: str
    category: str
    requirement_status: str
    assessment: str
    supporting_claim_ids: tuple[str, ...]
    verified_leaf_claim_ids: tuple[str, ...]
    unsupported_gap_claim_ids: tuple[str, ...]
    confidence: float
    calibration_id: int
    calibration_version: str
    deterministic_assessment: str
    ai_proposed_assessment: str | None
    ai_validation_status: str | None
    ai_validation_errors: tuple[str, ...]
    review_status: str
    human_review_id: int | None
    reviewer: str | None
    reviewed_at: str | None
    hard_constraint_failed: bool
    hard_constraint_reason: str | None


@dataclass(frozen=True)
class ScoreInput:
    job: Mapping[str, Any]
    mapping_run: Mapping[str, Any]
    requirements: tuple[RequirementInput, ...]
    assessment_manifest_checksum: str
    calibration_versions: tuple[str, ...]


STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "by", "for", "from",
    "in", "is", "of", "on", "or", "the", "to", "with", "you", "your",
    "required", "preferred", "experience", "strong", "ability",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical_checksum(value: Any) -> str:
    serialized = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(serialized).hexdigest()


def load_scoring_config(
    path: str | Path = DEFAULT_SCORING_CONFIG_PATH,
) -> ScoringConfig:
    payload = yaml.safe_load(Path(path).read_text())
    if not isinstance(payload, dict) or not isinstance(payload.get("opportunity_fit_v1"), dict):
        raise ValueError("scoring config must contain opportunity_fit_v1")
    data = payload["opportunity_fit_v1"]
    assessment_values = data.get("assessment_values") or {}
    importance = data.get("importance_multipliers") or {}
    dimensions = data.get("dimensions") or {}
    confidence = data.get("confidence") or {}
    classification = data.get("classification") or {}
    review_planning = data.get("review_planning") or {}
    missing_assessments = ASSESSMENTS - set(assessment_values)
    if missing_assessments:
        raise ValueError(
            "scoring config is missing assessment values: "
            + ", ".join(sorted(missing_assessments))
        )
    if set(importance) < {"mandatory", "preferred", "unspecified"}:
        raise ValueError("scoring config must define all importance multipliers")
    dimension_weight = sum(float(item.get("weight", 0)) for item in dimensions.values())
    if abs(dimension_weight - 100) > 1e-9:
        raise ValueError("opportunity dimension weights must sum to 100")
    component_weight = sum(
        float(value) for value in (confidence.get("component_weights") or {}).values()
    )
    if abs(component_weight - 1) > 1e-9:
        raise ValueError("confidence component weights must sum to 1")
    if "desired_company" in dimensions:
        raise ValueError("desired-company weight is prohibited in Opportunity Fit")
    canonical = json.loads(json.dumps(data, sort_keys=True))
    return ScoringConfig(
        version=str(data.get("version") or SCORING_VERSION),
        assessment_values={key: float(value) for key, value in assessment_values.items()},
        importance_multipliers={key: float(value) for key, value in importance.items()},
        dimensions=dimensions,
        feasibility_categories=tuple(data.get("feasibility_categories") or ()),
        target_geographies={
            str(name): tuple(str(token).lower() for token in tokens)
            for name, tokens in (data.get("target_geographies") or {}).items()
        },
        duplicate_similarity_threshold=float(
            data.get("duplicate_similarity_threshold", 0.82)
        ),
        confidence=confidence,
        classification={key: float(value) for key, value in classification.items()},
        review_planning=review_planning,
        checksum=_canonical_checksum(canonical),
    )


def _json_tuple(value: str | None) -> tuple[str, ...]:
    return tuple(str(item) for item in json.loads(value or "[]"))


def _score_job_row(conn: sqlite3.Connection, job_id: int) -> sqlite3.Row:
    row = conn.execute(
        """
        SELECT jobs.id, jobs.title, jobs.company, jobs.location,
               jobs.canonical_job_url, eligibility.decision AS eligibility,
               eligibility.verification_status AS eligibility_verification_status,
               eligibility.complete_description,
               enrichments.verification_status AS enrichment_verification_status,
               current.source_snapshot_id,
               snapshots.content_checksum AS current_job_content_checksum
        FROM jobs
        LEFT JOIN job_eligibility_decisions eligibility ON eligibility.job_id=jobs.id
        LEFT JOIN job_enrichments enrichments ON enrichments.job_id=jobs.id
        LEFT JOIN job_current_fields current
          ON current.job_id=jobs.id AND current.field_name='job_description'
        LEFT JOIN job_source_snapshots snapshots ON snapshots.id=current.source_snapshot_id
        WHERE jobs.id=?
        """,
        (job_id,),
    ).fetchone()
    if not row:
        raise KeyError(f"unknown job id: {job_id}")
    return row


def _latest_mapping_run(conn: sqlite3.Connection, job_id: int) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM job_evidence_mapping_runs WHERE job_id=? ORDER BY id DESC LIMIT 1",
        (job_id,),
    ).fetchone()
    if not row:
        raise ScoringBlockedError("mapping_missing", f"job {job_id} has no evidence mapping")
    return row


def _requirement_inputs(
    conn: sqlite3.Connection,
    mapping_run_id: int,
    facts: Mapping[str, EvidenceFact],
) -> tuple[RequirementInput, ...]:
    rows = conn.execute(
        """
        WITH latest_calibrations AS (
          SELECT requirement_row_id, MAX(id) calibration_id
          FROM job_requirement_calibrations GROUP BY requirement_row_id
        ), latest_reviews AS (
          SELECT requirement_row_id, MAX(id) review_id
          FROM job_requirement_human_reviews GROUP BY requirement_row_id
        )
        SELECT requirements.id requirement_row_id,
               requirements.requirement_id, requirements.sequence_number,
               requirements.source_text, requirements.normalized_requirement,
               requirements.category, requirements.requirement_status,
               calibrations.id calibration_id,
               calibrations.calibration_version,
               calibrations.deterministic_assessment,
               calibrations.ai_proposed_assessment,
               proposals.validation_status ai_validation_status,
               proposals.validation_errors_json ai_validation_errors_json,
               calibrations.final_assessment machine_assessment,
               calibrations.supporting_claim_ids_json machine_supporting_claim_ids_json,
               calibrations.verified_leaf_claim_ids_json,
               calibrations.unsupported_gap_claim_ids_json machine_gap_claim_ids_json,
               calibrations.confidence machine_confidence,
               calibrations.review_status machine_review_status,
               calibrations.hard_constraint_failed,
               calibrations.hard_constraint_reason,
               reviews.id human_review_id,
               reviews.final_assessment human_assessment,
               reviews.supporting_claim_ids_json human_supporting_claim_ids_json,
               reviews.unsupported_gap_claim_ids_json human_gap_claim_ids_json,
               reviews.hard_constraint_failed human_hard_constraint_failed,
               reviews.confidence human_confidence,
               reviews.reviewer, reviews.reviewed_at
        FROM job_requirements requirements
        LEFT JOIN latest_calibrations latest
          ON latest.requirement_row_id=requirements.id
        LEFT JOIN job_requirement_calibrations calibrations
          ON calibrations.id=latest.calibration_id
        LEFT JOIN job_requirement_ai_proposals proposals
          ON proposals.id=calibrations.ai_proposal_id
        LEFT JOIN latest_reviews latest_review
          ON latest_review.requirement_row_id=requirements.id
        LEFT JOIN job_requirement_human_reviews reviews
          ON reviews.id=latest_review.review_id
         AND reviews.calibration_id=calibrations.id
        WHERE requirements.run_id=?
        ORDER BY requirements.sequence_number
        """,
        (mapping_run_id,),
    ).fetchall()
    if not rows:
        raise ScoringBlockedError(
            "requirements_missing", "mapping contains no extracted requirements"
        )
    if any(row["calibration_id"] is None for row in rows):
        missing = [row["requirement_id"] for row in rows if row["calibration_id"] is None]
        raise ScoringBlockedError(
            "calibration_missing",
            "requirements lack calibrated assessments: " + ", ".join(missing),
        )
    result: list[RequirementInput] = []
    for row in rows:
        human = row["human_review_id"] is not None
        supporting_claim_ids = _json_tuple(
            row["human_supporting_claim_ids_json"]
            if human else row["machine_supporting_claim_ids_json"]
        )
        verified_leaf_claim_ids = (
            tuple(dict.fromkeys(
                leaf
                for claim_id in supporting_claim_ids
                if claim_id in facts
                for leaf in facts[claim_id].verified_leaf_ids
            ))
            if human else _json_tuple(row["verified_leaf_claim_ids_json"])
        )
        result.append(
            RequirementInput(
                requirement_row_id=row["requirement_row_id"],
                requirement_id=row["requirement_id"],
                sequence_number=row["sequence_number"],
                source_text=row["source_text"],
                normalized_requirement=row["normalized_requirement"],
                category=row["category"],
                requirement_status=row["requirement_status"],
                assessment=(row["human_assessment"] if human else row["machine_assessment"]),
                supporting_claim_ids=supporting_claim_ids,
                verified_leaf_claim_ids=verified_leaf_claim_ids,
                unsupported_gap_claim_ids=_json_tuple(
                    row["human_gap_claim_ids_json"]
                    if human else row["machine_gap_claim_ids_json"]
                ),
                confidence=float(
                    row["human_confidence"] if human else row["machine_confidence"]
                ),
                calibration_id=row["calibration_id"],
                calibration_version=row["calibration_version"],
                deterministic_assessment=row["deterministic_assessment"],
                ai_proposed_assessment=row["ai_proposed_assessment"],
                ai_validation_status=row["ai_validation_status"],
                ai_validation_errors=_json_tuple(row["ai_validation_errors_json"]),
                review_status="reviewed" if human else row["machine_review_status"],
                human_review_id=row["human_review_id"],
                reviewer=row["reviewer"],
                reviewed_at=row["reviewed_at"],
                hard_constraint_failed=bool(
                    row["human_hard_constraint_failed"]
                    if human else row["hard_constraint_failed"]
                ),
                hard_constraint_reason=row["hard_constraint_reason"],
            )
        )
    return tuple(result)


def _assessment_manifest(requirements: Iterable[RequirementInput]) -> list[dict[str, Any]]:
    return [
        {
            "requirement_row_id": item.requirement_row_id,
            "requirement_id": item.requirement_id,
            "calibration_id": item.calibration_id,
            "calibration_version": item.calibration_version,
            "human_review_id": item.human_review_id,
            "assessment": item.assessment,
            "supporting_claim_ids": item.supporting_claim_ids,
            "verified_leaf_claim_ids": item.verified_leaf_claim_ids,
            "unsupported_gap_claim_ids": item.unsupported_gap_claim_ids,
            "confidence": item.confidence,
            "hard_constraint_failed": item.hard_constraint_failed,
        }
        for item in requirements
    ]


def prepare_score_input(
    conn: sqlite3.Connection,
    job_id: int,
    *,
    candidate_evidence_path: str | Path = DEFAULT_CANDIDATE_EVIDENCE_PATH,
) -> ScoreInput:
    job = _score_job_row(conn, job_id)
    verification = (
        job["enrichment_verification_status"]
        or job["eligibility_verification_status"]
    )
    if job["eligibility"] not in {"eligible", "conditionally_eligible"}:
        raise ScoringBlockedError(
            "eligibility_blocked",
            f"job {job_id} eligibility is {job['eligibility'] or 'unset'}",
        )
    if verification in {"closed", "unavailable"}:
        raise ScoringBlockedError(
            verification, f"job {job_id} is {verification} and cannot be scored"
        )
    if not bool(job["complete_description"]):
        raise ScoringBlockedError(
            "description_incomplete",
            f"job {job_id} lacks a sufficiently complete description",
        )
    if not job["current_job_content_checksum"]:
        raise ScoringBlockedError(
            "description_missing", f"job {job_id} has no selected job description"
        )
    mapping = _latest_mapping_run(conn, job_id)
    artifact = load_candidate_evidence(candidate_evidence_path)
    current_candidate_checksum = candidate_evidence_checksum(artifact)
    if mapping["job_content_checksum"] != job["current_job_content_checksum"]:
        raise ScoringBlockedError(
            "mapping_stale_job_content",
            f"job {job_id} mapping is stale because job content changed",
        )
    if mapping["candidate_evidence_checksum"] != current_candidate_checksum:
        raise ScoringBlockedError(
            "mapping_stale_candidate_evidence",
            f"job {job_id} mapping is stale because candidate evidence changed",
        )
    requirements = _requirement_inputs(
        conn, mapping["id"], _evidence_facts(artifact)
    )
    manifest = _assessment_manifest(requirements)
    score_job = dict(job)
    score_job["candidate_current_location"] = str(artifact.identity.location.value)
    score_job["candidate_current_location_status"] = artifact.identity.location.status
    score_job["candidate_work_authorization_status"] = (
        "unknown" if "gap_work_authorization_01" in _evidence_facts(artifact)
        else "not_established"
    )
    score_job["candidate_relocation_willingness_status"] = "unknown"
    return ScoreInput(
        job=score_job,
        mapping_run=dict(mapping),
        requirements=requirements,
        assessment_manifest_checksum=_canonical_checksum(manifest),
        calibration_versions=tuple(
            sorted({item.calibration_version for item in requirements})
        ),
    )


def _tokens(value: str) -> frozenset[str]:
    return frozenset(
        token for token in re.findall(r"[a-z0-9]+", value.lower())
        if token not in STOPWORDS and len(token) > 1
    )


def requirement_similarity(left: str, right: str) -> float:
    left_tokens = _tokens(left)
    right_tokens = _tokens(right)
    if not left_tokens or not right_tokens:
        return 0.0
    return len(left_tokens & right_tokens) / len(left_tokens | right_tokens)


def _contains_keyword(text: str, keyword: str) -> bool:
    escaped = re.escape(keyword.lower()).replace(r"\ ", r"\s+")
    return bool(re.search(rf"(?<![a-z0-9]){escaped}(?![a-z0-9])", text.lower()))


def assign_dimension(
    requirement: RequirementInput, config: ScoringConfig
) -> tuple[str | None, str | None]:
    if requirement.category in config.feasibility_categories:
        return None, "feasibility_gate"
    priority = (
        "ai_relevance",
        "career_trajectory",
        "commercial_or_operational_relevance",
    )
    for dimension_name in priority:
        dimension = config.dimensions.get(dimension_name) or {}
        allowed_categories = tuple(dimension.get("keyword_categories") or ())
        if allowed_categories and requirement.category not in allowed_categories:
            continue
        if any(
            _contains_keyword(requirement.normalized_requirement, str(keyword))
            for keyword in dimension.get("keywords", [])
        ):
            return dimension_name, None
    for dimension_name, dimension in config.dimensions.items():
        if requirement.category in tuple(dimension.get("categories") or ()):
            return dimension_name, None
    return None, "unmapped_dimension"


def _geography_gate(
    location: str, config: ScoringConfig
) -> tuple[bool, dict[str, Any]]:
    normalized = location.lower().strip()
    matched = [
        market for market, tokens in config.target_geographies.items()
        if any(token in normalized for token in tokens)
    ]
    failed = bool(normalized) and not matched
    return failed, {
        "code": "outside_target_geography",
        "feasibility_type": "target_geography",
        "status": "hard_failure" if failed else "satisfied",
        "failed": failed,
        "blocking": failed,
        "observed_location": location,
        "matched_target_geographies": matched,
        "reason": (
            f"location {location!r} is outside configured targets"
            if failed else "location matches a configured target geography"
        ),
    }


def _language_gate(requirement: RequirementInput) -> str | None:
    text = requirement.normalized_requirement.lower()
    japanese = re.search(
        r"(?:business|native)(?:[- ]level)?(?:\s+\w+){0,3}\s+japanese"
        r"|japanese(?:\s+\w+){0,5}\s+(?:business|native)(?:[- ]level)?",
        text,
    )
    mandarin = re.search(
        r"(?:business|native)(?:[- ]level)?(?:\s+\w+){0,3}\s+(?:mandarin|chinese)"
        r"|(?:mandarin|chinese)(?:\s+\w+){0,5}\s+(?:business|native)(?:[- ]level)?",
        text,
    )
    if japanese:
        return "business_or_native_japanese_required"
    if mandarin:
        return "business_or_native_mandarin_chinese_required"
    return None


def _residency_market(
    requirement: RequirementInput, config: ScoringConfig
) -> str | None:
    text = requirement.normalized_requirement.lower()
    if not re.search(
        r"\b(?:already|currently)\b.{0,30}\b(?:based|resid(?:e|ing)|living)\b"
        r"|\b(?:based|resid(?:e|ing)|living)\b.{0,30}\b(?:already|currently)\b",
        text,
    ):
        return None
    for market, tokens in config.target_geographies.items():
        if any(_contains_keyword(text, token) for token in tokens):
            return market
    return "unspecified_location"


def feasibility_results(
    score_input: ScoreInput, config: ScoringConfig
) -> list[dict[str, Any]]:
    constraints: list[dict[str, Any]] = []
    _, geography = _geography_gate(str(score_input.job.get("location") or ""), config)
    constraints.append(geography)
    seen: set[tuple[str, int | None]] = set()
    candidate_location = str(
        score_input.job.get("candidate_current_location") or ""
    )
    candidate_location_status = str(
        score_input.job.get("candidate_current_location_status") or "unknown"
    )
    for requirement in score_input.requirements:
        text = requirement.normalized_requirement.lower()
        residency_market = _residency_market(requirement, config)
        if residency_market:
            market_tokens = config.target_geographies.get(residency_market, ())
            residence_matches = bool(candidate_location) and any(
                token in candidate_location.lower() for token in market_tokens
            )
            verified = candidate_location_status == "verified"
            failed = verified and not residence_matches
            blocking = failed or not verified
            constraints.append({
                "code": (
                    "current_residency_conflict" if failed
                    else "current_residency_unknown" if blocking
                    else "current_residency_satisfied"
                ),
                "feasibility_type": "current_residence",
                "status": (
                    "hard_failure" if failed
                    else "manual_blocker" if blocking else "satisfied"
                ),
                "failed": failed,
                "blocking": blocking,
                "requirement_row_id": requirement.requirement_row_id,
                "requirement_id": requirement.requirement_id,
                "source_text": requirement.source_text,
                "required_market": residency_market,
                "candidate_current_location": candidate_location or None,
                "candidate_location_status": candidate_location_status,
                "reason": (
                    "verified current residence conflicts with the explicit current-residency requirement"
                    if failed else
                    "current residence is not verified for the explicit residency requirement"
                    if blocking else
                    "verified current residence satisfies the explicit residency requirement"
                ),
            })
            seen.add(("validated_hard_constraint", requirement.requirement_row_id))

        work_authorization = bool(re.search(
            r"work authori[sz]ation|right to work|legally authori[sz]ed|valid work (?:permit|visa)",
            text,
        ))
        no_sponsorship = bool(re.search(
            r"no (?:visa )?sponsorship|without (?:visa )?sponsorship|"
            r"sponsorship (?:is )?not available|(?:will|can) not sponsor|unable to sponsor",
            text,
        ))
        authorization_unknown = (
            score_input.job.get("candidate_work_authorization_status")
            in {"unknown", "not_established", None}
            or
            "gap_work_authorization_01" in requirement.unsupported_gap_claim_ids
            or (work_authorization and not requirement.supporting_claim_ids)
        )
        if (work_authorization or no_sponsorship) and authorization_unknown:
            constraints.append({
                "code": (
                    "no_sponsorship_authorization_unknown"
                    if no_sponsorship else "work_authorization_unknown"
                ),
                "feasibility_type": (
                    "sponsorship_availability" if no_sponsorship
                    else "work_authorization"
                ),
                "status": "manual_blocker",
                "failed": False,
                "blocking": True,
                "requirement_row_id": requirement.requirement_row_id,
                "requirement_id": requirement.requirement_id,
                "source_text": requirement.source_text,
                "unsupported_gap_claim_ids": list(
                    requirement.unsupported_gap_claim_ids
                ),
                "reason": (
                    "the posting explicitly offers no sponsorship and candidate work authorization is unknown"
                    if no_sponsorship else
                    "candidate work authorization is unknown for an explicit authorization requirement"
                ),
            })

        if re.search(r"willing(?:ness)? to relocate|must relocate|relocation required", text):
            constraints.append({
                "code": "willingness_to_relocate_unknown",
                "feasibility_type": "willingness_to_relocate",
                "status": "manual_blocker",
                "failed": False,
                "blocking": True,
                "requirement_row_id": requirement.requirement_row_id,
                "requirement_id": requirement.requirement_id,
                "source_text": requirement.source_text,
                "reason": "candidate willingness to relocate is not established in verified evidence",
            })

        language_code = _language_gate(requirement)
        if language_code:
            key = (language_code, requirement.requirement_row_id)
            if key not in seen:
                seen.add(key)
                constraints.append({
                    "code": language_code,
                    "feasibility_type": "language",
                    "status": "hard_failure",
                    "failed": True,
                    "blocking": True,
                    "requirement_row_id": requirement.requirement_row_id,
                    "requirement_id": requirement.requirement_id,
                    "source_text": requirement.source_text,
                    "reason": "configured hard language constraint is present",
                })
        if requirement.hard_constraint_failed:
            key = ("validated_hard_constraint", requirement.requirement_row_id)
            if key not in seen:
                seen.add(key)
                constraints.append({
                    "code": "validated_hard_constraint",
                    "feasibility_type": "validated_hard_constraint",
                    "status": "hard_failure",
                    "failed": True,
                    "blocking": True,
                    "requirement_row_id": requirement.requirement_row_id,
                    "requirement_id": requirement.requirement_id,
                    "source_text": requirement.source_text,
                    "reason": requirement.hard_constraint_reason,
                })
    return constraints


def hard_constraint_results(
    score_input: ScoreInput, config: ScoringConfig
) -> list[dict[str, Any]]:
    return feasibility_results(score_input, config)


def _protected_scope(requirement: RequirementInput, config: ScoringConfig) -> bool:
    planning = config.review_planning
    lowered = requirement.normalized_requirement.lower()
    patterns = tuple(str(item).lower() for item in planning.get("protected_scope_patterns", ()))
    protected_gaps = set(str(item) for item in planning.get("protected_gap_ids", ()))
    return (
        any(pattern in lowered for pattern in patterns)
        or bool(protected_gaps & set(requirement.unsupported_gap_claim_ids))
    )


def _material_requirement_reason(
    requirement: RequirementInput, config: ScoringConfig
) -> str | None:
    if requirement.review_status == "reviewed":
        return None
    if requirement.hard_constraint_failed:
        return "hard_constraint_pending_review"
    if requirement.assessment == "contradicted":
        return "contradicted_requirement"
    if requirement.assessment == "unknown":
        return "unknown_requirement"
    if _protected_scope(requirement, config) and requirement.assessment != "confirmed":
        return "protected_scope_unresolved"
    minimum = float(config.review_planning.get("semantic_confidence_minimum", 0.8))
    if (
        requirement.ai_proposed_assessment
        and requirement.ai_proposed_assessment != requirement.deterministic_assessment
        and requirement.confidence < minimum
    ):
        return "low_confidence_semantic_mapping"
    if (
        requirement.ai_validation_status == "rejected"
        and requirement.assessment == requirement.ai_proposed_assessment
    ):
        return "invalid_semantic_mapping_dependency"
    return None


def calculate_opportunity_score(
    score_input: ScoreInput, config: ScoringConfig
) -> dict[str, Any]:
    hard_constraints = hard_constraint_results(score_input, config)
    hard_failed = any(item["failed"] for item in hard_constraints)
    feasibility_blocked = any(
        item.get("blocking") and not item["failed"] for item in hard_constraints
    )
    assigned: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    for requirement in score_input.requirements:
        dimension, exclusion_reason = assign_dimension(requirement, config)
        importance = config.importance_multipliers[requirement.requirement_status]
        item = {
            "requirement_row_id": requirement.requirement_row_id,
            "requirement_id": requirement.requirement_id,
            "source_text": requirement.source_text,
            "category": requirement.category,
            "requirement_status": requirement.requirement_status,
            "assessment": requirement.assessment,
            "deterministic_assessment": requirement.deterministic_assessment,
            "ai_proposed_assessment": requirement.ai_proposed_assessment,
            "ai_validation_status": requirement.ai_validation_status,
            "ai_validation_errors": list(requirement.ai_validation_errors),
            "fit_value": config.assessment_values[requirement.assessment],
            "importance_multiplier": importance,
            "calibration_id": requirement.calibration_id,
            "calibration_version": requirement.calibration_version,
            "human_review_id": requirement.human_review_id,
            "supporting_claim_ids": list(requirement.supporting_claim_ids),
            "verified_leaf_claim_ids": list(requirement.verified_leaf_claim_ids),
            "unsupported_gap_claim_ids": list(requirement.unsupported_gap_claim_ids),
            "assessment_confidence": requirement.confidence,
            "review_status": requirement.review_status,
            "hard_constraint_failed": requirement.hard_constraint_failed,
            "hard_constraint_reason": requirement.hard_constraint_reason,
            "dimension": dimension,
            "excluded": exclusion_reason is not None,
            "exclusion_reason": exclusion_reason,
        }
        if exclusion_reason:
            excluded.append({
                "requirement_row_id": requirement.requirement_row_id,
                "requirement_id": requirement.requirement_id,
                "source_text": requirement.source_text,
                "reason": exclusion_reason,
            })
        assigned.append(item)

    for dimension_name in config.dimensions:
        candidates = [
            item for item in assigned
            if item["dimension"] == dimension_name and not item["excluded"]
        ]
        candidates.sort(
            key=lambda item: (-item["importance_multiplier"], item["requirement_row_id"])
        )
        retained: list[dict[str, Any]] = []
        for candidate in candidates:
            duplicate = next(
                (
                    prior for prior in retained
                    if requirement_similarity(
                        candidate["source_text"], prior["source_text"]
                    ) >= config.duplicate_similarity_threshold
                ),
                None,
            )
            if duplicate:
                candidate["excluded"] = True
                candidate["exclusion_reason"] = "material_duplicate"
                candidate["duplicate_of_requirement_row_id"] = duplicate[
                    "requirement_row_id"
                ]
                excluded.append({
                    "requirement_row_id": candidate["requirement_row_id"],
                    "requirement_id": candidate["requirement_id"],
                    "source_text": candidate["source_text"],
                    "reason": "material_duplicate",
                    "duplicate_of_requirement_row_id": duplicate[
                        "requirement_row_id"
                    ],
                })
            else:
                retained.append(candidate)

    dimensions: dict[str, dict[str, Any]] = {}
    assessed_dimension_weight = 0.0
    weighted_fit_total = 0.0
    for dimension_name, definition in config.dimensions.items():
        weight = float(definition["weight"])
        items = [
            item for item in assigned
            if item["dimension"] == dimension_name and not item["excluded"]
        ]
        denominator = sum(item["importance_multiplier"] for item in items)
        numerator = sum(
            item["fit_value"] * item["importance_multiplier"] for item in items
        )
        if denominator:
            ratio = numerator / denominator
            weighted_points = ratio * weight
            status = "assessed"
            assessed_dimension_weight += weight
            weighted_fit_total += weighted_points
        else:
            ratio = None
            weighted_points = None
            status = "incomplete"
        dimensions[dimension_name] = {
            "weight": weight,
            "status": status,
            "ratio": round(ratio, 4) if ratio is not None else None,
            "weighted_points": (
                round(weighted_points, 2) if weighted_points is not None else None
            ),
            "requirement_count": len(items),
            "normalization_denominator": round(denominator, 4),
            "incomplete_reason": (
                None if denominator else "no explicit scored requirements"
            ),
        }
    pre_gate_fit = (
        100 * weighted_fit_total / assessed_dimension_weight
        if assessed_dimension_weight else 0.0
    )
    final_fit = 0.0 if hard_failed else pre_gate_fit

    duplicate_requirement_ids = {
        item["requirement_row_id"]
        for item in assigned if item["exclusion_reason"] == "material_duplicate"
    }
    confidence_requirements = [
        item for item in score_input.requirements
        if item.requirement_row_id not in duplicate_requirement_ids
    ]
    importance_total = sum(
        config.importance_multipliers[item.requirement_status]
        for item in confidence_requirements
    )
    certainty = config.confidence.get("assessment_certainty") or {}
    assessment_quality = (
        sum(
            config.importance_multipliers[item.requirement_status]
            * item.confidence
            * float(certainty[item.assessment])
            for item in confidence_requirements
        ) / importance_total
        if importance_total else 0.0
    )
    dimension_coverage = assessed_dimension_weight / 100
    material_by_requirement: dict[int, str] = {}
    for requirement in confidence_requirements:
        reason = _material_requirement_reason(requirement, config)
        if reason:
            material_by_requirement[requirement.requirement_row_id] = reason
    unresolved_weight = sum(
        config.importance_multipliers[item.requirement_status]
        for item in confidence_requirements
        if item.requirement_row_id in material_by_requirement
    )
    review_resolution = (
        max(0.0, 1 - unresolved_weight / importance_total)
        if importance_total else 0.0
    )
    component_weights = config.confidence.get("component_weights") or {}
    confidence_score = 100 * (
        assessment_quality * float(component_weights["assessment_quality"])
        + dimension_coverage * float(component_weights["dimension_coverage"])
        + review_resolution * float(component_weights["review_resolution"])
    )
    unknown_weight = sum(
        config.importance_multipliers[item.requirement_status]
        for item in confidence_requirements if item.assessment == "unknown"
    )
    if importance_total:
        confidence_score -= (
            float(config.confidence.get("unknown_penalty", 0))
            * unknown_weight / importance_total
        )
    confidence_score = max(0.0, min(100.0, confidence_score))

    review_reasons: list[dict[str, Any]] = []
    for constraint in hard_constraints:
        if constraint["failed"]:
            review_reasons.append({
                "code": "hard_constraint_failed",
                "detail": constraint,
                "material": True,
            })
        elif constraint.get("blocking"):
            review_reasons.append({
                "code": "feasibility_confirmation_required",
                "detail": constraint,
                "material": True,
            })
    for requirement in confidence_requirements:
        reason = material_by_requirement.get(requirement.requirement_row_id)
        if reason:
            review_reasons.append({
                "code": reason,
                "requirement_row_id": requirement.requirement_row_id,
                "requirement_id": requirement.requirement_id,
                "source_text": requirement.source_text,
                "assessment": requirement.assessment,
                "material": True,
            })
    adequate_confidence = float(config.confidence.get("adequate_minimum", 70))
    material_issues = [item for item in review_reasons if item.get("material")]
    apply_minimum = float(config.classification["apply_minimum"])
    investigate_minimum = float(config.classification["investigate_minimum"])
    if hard_failed or final_fit < investigate_minimum:
        classification = "C"
    elif (
        final_fit >= apply_minimum
        and confidence_score >= adequate_confidence
        and not material_issues
        and not feasibility_blocked
    ):
        classification = "A"
    else:
        classification = "B"
    if final_fit >= apply_minimum and confidence_score < adequate_confidence:
        review_reasons.append({
            "code": "high_fit_low_confidence",
            "confidence": round(confidence_score, 2),
            "required_confidence": adequate_confidence,
            "material": True,
        })
    if final_fit >= apply_minimum and material_issues:
        review_reasons.append({
            "code": "high_fit_material_review_unresolved",
            "material_issue_count": len(material_issues),
            "material": True,
        })

    for item in assigned:
        if item["excluded"]:
            item["dimension_contribution_points"] = 0.0
            item["normalized_opportunity_contribution_points"] = 0.0
            continue
        dimension = dimensions[item["dimension"]]
        denominator = dimension["normalization_denominator"]
        item["dimension_contribution_points"] = round(
            dimension["weight"]
            * item["fit_value"]
            * item["importance_multiplier"]
            / denominator,
            4,
        ) if denominator else 0.0
        item["normalized_opportunity_contribution_points"] = round(
            item["dimension_contribution_points"]
            * 100 / assessed_dimension_weight,
            4,
        ) if assessed_dimension_weight else 0.0

    return {
        "opportunity_fit_score": round(final_fit, 2),
        "pre_gate_fit_score": round(pre_gate_fit, 2),
        "evidence_confidence_score": round(confidence_score, 2),
        "provisional_classification": classification,
        "hard_constraint_failed": hard_failed,
        "feasibility_blocked": feasibility_blocked,
        "hard_constraints": hard_constraints,
        "dimension_breakdown": dimensions,
        "requirement_contributions": assigned,
        "excluded_requirements": excluded,
        "review_reasons": review_reasons,
        "confidence_components": {
            "assessment_quality": round(100 * assessment_quality, 2),
            "dimension_coverage": round(100 * dimension_coverage, 2),
            "review_resolution": round(100 * review_resolution, 2),
            "unknown_penalty": round(
                float(config.confidence.get("unknown_penalty", 0))
                * unknown_weight / importance_total,
                2,
            ) if importance_total else 0.0,
        },
    }


def score_opportunity(
    conn: sqlite3.Connection,
    job_id: int,
    *,
    scoring_config_path: str | Path = DEFAULT_SCORING_CONFIG_PATH,
    candidate_evidence_path: str | Path = DEFAULT_CANDIDATE_EVIDENCE_PATH,
) -> dict[str, Any]:
    config = load_scoring_config(scoring_config_path)
    score_input = prepare_score_input(
        conn, job_id, candidate_evidence_path=candidate_evidence_path
    )
    calculated = calculate_opportunity_score(score_input, config)
    existing = conn.execute(
        """
        SELECT id FROM opportunity_fit_scores
        WHERE job_id=? AND scoring_version=? AND job_content_checksum=?
          AND candidate_evidence_checksum=? AND mapping_version=?
          AND scoring_config_checksum=? AND assessment_manifest_checksum=?
        """,
        (
            job_id, config.version,
            score_input.mapping_run["job_content_checksum"],
            score_input.mapping_run["candidate_evidence_checksum"],
            score_input.mapping_run["mapping_version"], config.checksum,
            score_input.assessment_manifest_checksum,
        ),
    ).fetchone()
    if existing:
        score_id = int(existing["id"])
        created = False
    else:
        scored_at = _now()
        with conn:
            cursor = conn.execute(
            """
            INSERT INTO opportunity_fit_scores(
              job_id, mapping_run_id, scoring_version, job_content_checksum,
              candidate_evidence_checksum, mapping_version,
              calibration_versions_json, scoring_config_checksum,
              assessment_manifest_checksum, opportunity_fit_score,
              pre_gate_fit_score, evidence_confidence_score,
              provisional_classification, hard_constraint_failed,
              hard_constraints_json, dimension_breakdown_json,
              contribution_manifest_json, excluded_requirements_json,
              review_reasons_json, confidence_components_json, scored_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
                (
                    job_id, score_input.mapping_run["id"], config.version,
                    score_input.mapping_run["job_content_checksum"],
                    score_input.mapping_run["candidate_evidence_checksum"],
                    score_input.mapping_run["mapping_version"],
                    json.dumps(score_input.calibration_versions), config.checksum,
                    score_input.assessment_manifest_checksum,
                    calculated["opportunity_fit_score"],
                    calculated["pre_gate_fit_score"],
                    calculated["evidence_confidence_score"],
                    calculated["provisional_classification"],
                    int(calculated["hard_constraint_failed"]),
                    json.dumps(calculated["hard_constraints"], ensure_ascii=False),
                    json.dumps(calculated["dimension_breakdown"], ensure_ascii=False),
                    json.dumps(calculated["requirement_contributions"], ensure_ascii=False),
                    json.dumps(calculated["excluded_requirements"], ensure_ascii=False),
                    json.dumps(calculated["review_reasons"], ensure_ascii=False),
                    json.dumps(calculated["confidence_components"], ensure_ascii=False),
                    scored_at,
                ),
            )
        score_id = int(cursor.lastrowid)
        created = True
    from .score_review_planning import persist_score_review_plan

    review_plan, review_plan_created, review_plan_id = persist_score_review_plan(
        conn, score_id, score_input, calculated, config
    )
    return {
        "job_id": job_id,
        "score_id": score_id,
        "created": created,
        "review_plan_id": review_plan_id,
        "review_plan_created": review_plan_created,
        "review_plan": review_plan,
        **calculated,
    }


def score_opportunities(
    conn: sqlite3.Connection,
    *,
    job_ids: list[int] | None = None,
    scoring_config_path: str | Path = DEFAULT_SCORING_CONFIG_PATH,
    candidate_evidence_path: str | Path = DEFAULT_CANDIDATE_EVIDENCE_PATH,
) -> dict[str, Any]:
    if job_ids is None:
        job_ids = [
            row[0] for row in conn.execute(
                """
                SELECT jobs.id FROM jobs
                JOIN job_eligibility_decisions eligibility ON eligibility.job_id=jobs.id
                WHERE eligibility.decision IN ('eligible', 'conditionally_eligible')
                  AND eligibility.complete_description=1
                ORDER BY jobs.id
                """
            )
        ]
    results: list[dict[str, Any]] = []
    blocked: list[dict[str, Any]] = []
    for job_id in job_ids:
        try:
            results.append(
                score_opportunity(
                    conn, job_id,
                    scoring_config_path=scoring_config_path,
                    candidate_evidence_path=candidate_evidence_path,
                )
            )
        except ScoringBlockedError as exc:
            blocked.append({"job_id": job_id, "code": exc.code, "reason": str(exc)})
    return {
        "attempted": len(job_ids),
        "scored": len(results),
        "created_scores": sum(bool(item["created"]) for item in results),
        "reused_scores": sum(not item["created"] for item in results),
        "blocked": blocked,
        "results": results,
    }
