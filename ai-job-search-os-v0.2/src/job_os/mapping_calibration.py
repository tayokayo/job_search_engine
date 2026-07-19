from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Protocol

from .candidate_evidence import (
    DEFAULT_CANDIDATE_EVIDENCE_PATH,
    CandidateEvidenceDocument,
    load_candidate_evidence,
    validate_claim_transformation,
)
from .requirement_mapping import (
    EvidenceFact,
    MappingDecision,
    Requirement,
    _evidence_facts,
    map_requirement,
)

ASSESSMENTS = {"confirmed", "partial", "unsupported", "contradicted", "unknown"}
CALIBRATION_VERSION = "evidence-calibration-v2"


@dataclass(frozen=True)
class AIProposal:
    assessment: str
    supporting_claim_ids: tuple[str, ...]
    unsupported_gap_claim_ids: tuple[str, ...]
    explanation: str
    confidence: float
    raw: Mapping[str, Any]


class SemanticMappingProvider(Protocol):
    name: str
    model: str
    mapper_version: str

    def propose(
        self,
        *,
        job_id: int,
        requirement: Requirement,
        deterministic: MappingDecision,
        candidate_facts: Mapping[str, EvidenceFact],
    ) -> AIProposal | None: ...


class NoAIProvider:
    name = "none"
    model = "none"
    mapper_version = "none"

    def propose(self, **_: Any) -> None:
        return None


class CapturedAIProvider:
    def __init__(self, payload: Mapping[str, Any]):
        self.name = str(payload.get("provider") or "captured_ai")
        self.model = str(payload.get("model") or "unspecified")
        self.mapper_version = str(payload.get("mapper_version") or "captured-v1")
        proposals = payload.get("proposals") or {}
        if isinstance(proposals, list):
            proposals = {str(item["key"]): item for item in proposals}
        if not isinstance(proposals, Mapping):
            raise ValueError("captured AI proposals must be an object or keyed list")
        self._proposals = proposals
        self._default = payload.get("default_proposal")

    @classmethod
    def from_json(cls, path: str | Path) -> "CapturedAIProvider":
        payload = json.loads(Path(path).read_text())
        if not isinstance(payload, Mapping):
            raise ValueError("captured AI proposal file must contain a JSON object")
        return cls(payload)

    def propose(
        self,
        *,
        job_id: int,
        requirement: Requirement,
        deterministic: MappingDecision,
        candidate_facts: Mapping[str, EvidenceFact],
    ) -> AIProposal | None:
        del candidate_facts
        raw = self._proposals.get(f"{job_id}:{requirement.requirement_id}")
        if raw is None:
            raw = self._proposals.get(requirement.requirement_id)
        if raw is None:
            raw = self._default
        if raw == "mirror_deterministic":
            raw = {
                "assessment": deterministic.assessment,
                "supporting_claim_ids": [fact.fact_id for fact in deterministic.supporting],
                "unsupported_gap_claim_ids": [fact.fact_id for fact in deterministic.gaps],
                "explanation": deterministic.explanation,
                "confidence": deterministic.confidence,
                "capture_policy": "codex_session_reviewed_deterministic_result",
            }
        if raw is None:
            return None
        if not isinstance(raw, Mapping):
            raise ValueError(f"proposal for {requirement.requirement_id} must be an object")
        return AIProposal(
            assessment=str(raw.get("assessment") or ""),
            supporting_claim_ids=tuple(str(item) for item in raw.get("supporting_claim_ids", [])),
            unsupported_gap_claim_ids=tuple(str(item) for item in raw.get("unsupported_gap_claim_ids", [])),
            explanation=str(raw.get("explanation") or ""),
            confidence=float(raw.get("confidence", 0)),
            raw=dict(raw),
        )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json_ids(value: str) -> tuple[str, ...]:
    return tuple(str(item) for item in json.loads(value))


def _requirement_from_row(row: sqlite3.Row) -> Requirement:
    return Requirement(
        requirement_id=row["requirement_id"],
        sequence_number=row["sequence_number"],
        source_text=row["source_text"],
        source_span_start=row["source_span_start"],
        source_span_end=row["source_span_end"],
        normalized_requirement=row["normalized_requirement"],
        category=row["category"],
        importance=row["importance"],
        requirement_status=row["requirement_status"],
        extraction_confidence=row["extraction_confidence"],
    )


def _is_ambiguous(requirement: Requirement) -> bool:
    text = requirement.normalized_requirement.lower()
    if len(text.split()) < 3:
        return True
    if requirement.category == "language" and not re.search(
        r"\b(?:english|thai|japanese|mandarin|chinese)\b", text
    ):
        return True
    return False


def validate_ai_proposal(
    requirement: Requirement,
    deterministic: MappingDecision,
    proposal: AIProposal,
    facts: Mapping[str, EvidenceFact],
) -> tuple[str, ...]:
    errors: list[str] = []
    if proposal.assessment not in ASSESSMENTS:
        errors.append("invalid_assessment")
    if not 0 <= proposal.confidence <= 1:
        errors.append("invalid_confidence")
    if not proposal.explanation.strip():
        errors.append("missing_explanation")
    unknown_support = [item for item in proposal.supporting_claim_ids if item not in facts]
    unknown_gaps = [item for item in proposal.unsupported_gap_claim_ids if item not in facts]
    if unknown_support:
        errors.append("unknown_supporting_claim_ids:" + ",".join(sorted(unknown_support)))
    if unknown_gaps:
        errors.append("unknown_gap_claim_ids:" + ",".join(sorted(unknown_gaps)))
    if any(facts[item].status == "unsupported" for item in proposal.supporting_claim_ids if item in facts):
        errors.append("unsupported_claim_used_as_affirmative_evidence")
    if any(facts[item].status != "unsupported" for item in proposal.unsupported_gap_claim_ids if item in facts):
        errors.append("affirmative_claim_used_as_gap")
    if set(proposal.supporting_claim_ids) & set(proposal.unsupported_gap_claim_ids):
        errors.append("claim_used_as_support_and_gap")
    if proposal.assessment in {"confirmed", "partial"} and not proposal.supporting_claim_ids:
        errors.append("affirmative_assessment_without_support")
    if proposal.assessment == "unknown" and not _is_ambiguous(requirement):
        errors.append("unknown_for_clear_requirement")
    if deterministic.hard_constraint_failed:
        if proposal.assessment != "contradicted":
            errors.append("hard_constraint_override")
        deterministic_gaps = {fact.fact_id for fact in deterministic.gaps}
        if not deterministic_gaps.issubset(set(proposal.unsupported_gap_claim_ids)):
            errors.append("hard_constraint_gap_omitted")
    protected_gaps = {fact.fact_id for fact in deterministic.gaps} & {
        "gap_direct_reports_01",
        "gap_people_authority_01",
        "gap_paid_acquisition_01",
        "gap_pnl_ownership_01",
        "gap_budget_01",
        "gap_ml_engineering_leadership_01",
        "gap_software_engineering_management_01",
    }
    if protected_gaps and proposal.assessment == "confirmed":
        errors.append("protected_scope_upgrade")
    protected_terms = (
        (r"paid acquisition|\bseo\b|\baeo\b", "gap_paid_acquisition_01"),
        (r"\bp&l\b|profit and loss", "gap_pnl_ownership_01"),
        (r"\bbudget", "gap_budget_01"),
        (r"direct reports?", "gap_direct_reports_01"),
        (r"hiring authority|performance-management authority", "gap_people_authority_01"),
        (r"engineering management|manage engineers", "gap_software_engineering_management_01"),
    )
    for pattern, gap_id in protected_terms:
        if re.search(pattern, proposal.explanation, re.I) and gap_id not in proposal.unsupported_gap_claim_ids:
            errors.append(f"uncited_protected_scope:{gap_id}")
    allowed_metric_text = " ".join(
        [requirement.source_text]
        + [facts[item].text for item in proposal.supporting_claim_ids if item in facts]
    )
    proposed_numbers = set(re.findall(r"\b\d+(?:\.\d+)?\b", proposal.explanation))
    allowed_numbers = set(re.findall(r"\b\d+(?:\.\d+)?\b", allowed_metric_text))
    if proposed_numbers - allowed_numbers:
        errors.append("introduced_numeric_fact")
    for claim_id in proposal.supporting_claim_ids:
        if claim_id not in facts:
            continue
        requirement_lower = requirement.source_text.lower()
        claim_lower = facts[claim_id].text.lower()
        if (
            ("%" in requirement_lower and re.search(r"percentage points?", claim_lower))
            or (re.search(r"percentage points?", requirement_lower) and "%" in claim_lower)
        ):
            errors.append(f"metric_mismatch:{claim_id}")
        if any(
            issue.code == "attribution_upgrade"
            for issue in validate_claim_transformation(
                facts[claim_id].text, proposal.explanation
            )
        ):
            errors.append(f"attribution_upgrade:{claim_id}")
    return tuple(dict.fromkeys(errors))


def _verified_leaves(facts: Mapping[str, EvidenceFact], ids: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(
        leaf for claim_id in ids if claim_id in facts for leaf in facts[claim_id].verified_leaf_ids
    ))


def _persist_proposal(
    conn: sqlite3.Connection,
    requirement_row_id: int,
    provider: SemanticMappingProvider,
    proposal: AIProposal,
    errors: tuple[str, ...],
) -> int:
    conn.execute(
        """
        INSERT OR IGNORE INTO job_requirement_ai_proposals(
          requirement_row_id, provider, model, mapper_version, proposed_assessment,
          supporting_claim_ids_json, unsupported_gap_claim_ids_json, explanation,
          confidence, raw_response_json, validation_status, validation_errors_json,
          created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            requirement_row_id, provider.name, provider.model, provider.mapper_version,
            proposal.assessment, json.dumps(proposal.supporting_claim_ids),
            json.dumps(proposal.unsupported_gap_claim_ids), proposal.explanation,
            proposal.confidence, json.dumps(proposal.raw, ensure_ascii=False, sort_keys=True),
            "rejected" if errors else "accepted", json.dumps(errors), _now(),
        ),
    )
    return conn.execute(
        """SELECT id FROM job_requirement_ai_proposals
           WHERE requirement_row_id=? AND provider=? AND model=? AND mapper_version=?""",
        (requirement_row_id, provider.name, provider.model, provider.mapper_version),
    ).fetchone()[0]


def calibrate_requirement(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    artifact: CandidateEvidenceDocument,
    provider: SemanticMappingProvider,
) -> dict[str, Any]:
    requirement = _requirement_from_row(row)
    facts = _evidence_facts(artifact)
    deterministic = map_requirement(requirement, facts)
    calibration_version = ":".join(
        (CALIBRATION_VERSION, provider.name, provider.model, provider.mapper_version)
    )
    existing = conn.execute(
        "SELECT * FROM job_requirement_calibrations WHERE requirement_row_id=? AND calibration_version=?",
        (row["requirement_row_id"], calibration_version),
    ).fetchone()
    if existing:
        errors: tuple[str, ...] = ()
        if existing["ai_proposal_id"]:
            proposal_row = conn.execute(
                "SELECT validation_errors_json FROM job_requirement_ai_proposals WHERE id=?",
                (existing["ai_proposal_id"],),
            ).fetchone()
            errors = tuple(json.loads(proposal_row[0])) if proposal_row else ()
        return {
            "calibration_id": existing["id"],
            "created": False,
            "deterministic_assessment": existing["deterministic_assessment"],
            "ai_assessment": existing["ai_proposed_assessment"],
            "final_assessment": existing["final_assessment"],
            "proposal_errors": errors,
            "review_status": existing["review_status"],
            "hard_constraint_failed": bool(existing["hard_constraint_failed"]),
        }

    proposal = provider.propose(
        job_id=row["job_id"], requirement=requirement,
        deterministic=deterministic, candidate_facts=facts,
    )
    proposal_id = None
    proposal_errors: tuple[str, ...] = ()
    if proposal is not None:
        proposal_errors = validate_ai_proposal(requirement, deterministic, proposal, facts)
        proposal_id = _persist_proposal(conn, row["requirement_row_id"], provider, proposal, proposal_errors)

    final_assessment = deterministic.assessment
    final_support = tuple(fact.fact_id for fact in deterministic.supporting)
    final_gaps = tuple(fact.fact_id for fact in deterministic.gaps)
    confidence = deterministic.confidence
    ai_assessment = proposal.assessment if proposal else None
    if proposal is not None and not proposal_errors and not deterministic.hard_constraint_failed:
        final_assessment = proposal.assessment
        final_support = proposal.supporting_claim_ids
        final_gaps = proposal.unsupported_gap_claim_ids
        confidence = proposal.confidence
    if deterministic.hard_constraint_failed:
        final_assessment = "contradicted"
        final_support = tuple(fact.fact_id for fact in deterministic.supporting)
        final_gaps = tuple(fact.fact_id for fact in deterministic.gaps)
        confidence = deterministic.confidence

    reasons: list[str] = []
    if deterministic.hard_constraint_failed:
        reasons.append("hard_constraint_failed")
    if proposal_errors:
        reasons.append("invalid_ai_proposal")
    if proposal and proposal.assessment != deterministic.assessment:
        reasons.append("deterministic_ai_disagreement")
    if final_assessment == "partial":
        reasons.append("partial_scope_match")
    elif final_assessment == "unsupported":
        reasons.append("clear_requirement_without_support")
    elif final_assessment == "contradicted":
        reasons.append("contradicting_evidence")
    elif final_assessment == "unknown":
        reasons.append("ambiguous_requirement")
    review_status = "not_required" if final_assessment == "confirmed" and not proposal_errors else "pending"
    cursor = conn.execute(
        """
        INSERT INTO job_requirement_calibrations(
          requirement_row_id, calibration_version, deterministic_assessment,
          ai_proposal_id, ai_proposed_assessment, final_assessment,
          supporting_claim_ids_json, verified_leaf_claim_ids_json,
          unsupported_gap_claim_ids_json, hard_constraint_failed,
          hard_constraint_reason, confidence, review_reason, review_status, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            row["requirement_row_id"], calibration_version, deterministic.assessment,
            proposal_id, ai_assessment, final_assessment, json.dumps(final_support),
            json.dumps(_verified_leaves(facts, final_support)), json.dumps(final_gaps),
            int(deterministic.hard_constraint_failed), deterministic.hard_constraint_reason,
            confidence, ";".join(reasons) or None, review_status, _now(),
        ),
    )
    return {
        "calibration_id": cursor.lastrowid,
        "created": True,
        "deterministic_assessment": deterministic.assessment,
        "ai_assessment": ai_assessment,
        "final_assessment": final_assessment,
        "proposal_errors": proposal_errors,
        "review_status": review_status,
        "hard_constraint_failed": deterministic.hard_constraint_failed,
    }


def _latest_requirement_rows(conn: sqlite3.Connection, job_ids: list[int] | None) -> list[sqlite3.Row]:
    parameters: list[Any] = []
    job_filter = ""
    if job_ids:
        job_filter = "AND runs.job_id IN (" + ",".join("?" for _ in job_ids) + ")"
        parameters.extend(job_ids)
    return conn.execute(
        f"""
        WITH latest_runs AS (
          SELECT job_id, MAX(id) AS run_id FROM job_evidence_mapping_runs GROUP BY job_id
        )
        SELECT runs.job_id, requirements.id AS requirement_row_id, requirements.*
        FROM latest_runs
        JOIN job_evidence_mapping_runs runs ON runs.id = latest_runs.run_id
        JOIN job_requirements requirements ON requirements.run_id = runs.id
        WHERE 1=1 {job_filter}
        ORDER BY runs.job_id, requirements.sequence_number
        """,
        parameters,
    ).fetchall()


def calibrate_jobs(
    conn: sqlite3.Connection,
    *,
    provider: SemanticMappingProvider | None = None,
    job_ids: list[int] | None = None,
    candidate_evidence_path: str | Path = DEFAULT_CANDIDATE_EVIDENCE_PATH,
) -> dict[str, Any]:
    provider = provider or NoAIProvider()
    artifact = load_candidate_evidence(candidate_evidence_path)
    rows = _latest_requirement_rows(conn, job_ids)
    results: list[dict[str, Any]] = []
    with conn:
        for row in rows:
            result = calibrate_requirement(conn, row, artifact, provider)
            result.update(job_id=row["job_id"], requirement_id=row["requirement_id"])
            results.append(result)
    final_counts: dict[str, int] = {}
    review_reasons: dict[str, int] = {}
    requirement_row_ids = [row["requirement_row_id"] for row in rows]
    if requirement_row_ids:
        placeholders = ",".join("?" for _ in requirement_row_ids)
        for count_row in conn.execute(
            f"""
            SELECT final_assessment, COUNT(*) FROM job_requirement_calibrations
            WHERE requirement_row_id IN ({placeholders})
              AND id IN (SELECT MAX(id) FROM job_requirement_calibrations GROUP BY requirement_row_id)
            GROUP BY final_assessment
            """,
            requirement_row_ids,
        ):
            final_counts[count_row[0]] = count_row[1]
    queue = show_review_queue(conn, job_ids=job_ids)
    for item in queue["requirements"]:
        for reason in (item.get("review_reason") or "unspecified").split(";"):
            review_reasons[reason] = review_reasons.get(reason, 0) + 1
    return {
        "provider": provider.name,
        "model": provider.model,
        "mapper_version": provider.mapper_version,
        "requirements": len(rows),
        "created_calibrations": sum(bool(item["created"]) for item in results),
        "reused_calibrations": sum(not item["created"] for item in results),
        "ai_proposals": sum(item.get("ai_assessment") is not None for item in results),
        "ai_disagreements": sum(
            item.get("ai_assessment") is not None
            and item.get("ai_assessment") != item.get("deterministic_assessment")
            for item in results
        ),
        "invalid_ai_proposals": sum(bool(item.get("proposal_errors")) for item in results),
        "hard_constraint_failures": sum(bool(item.get("hard_constraint_failed")) for item in results),
        "final_assessments": dict(sorted(final_counts.items())),
        "review_queue": queue["count"],
        "review_reasons": dict(sorted(review_reasons.items())),
    }


def show_review_queue(
    conn: sqlite3.Connection,
    *,
    job_ids: list[int] | None = None,
) -> dict[str, Any]:
    parameters: list[Any] = []
    job_filter = ""
    if job_ids:
        job_filter = "AND runs.job_id IN (" + ",".join("?" for _ in job_ids) + ")"
        parameters.extend(job_ids)
    rows = conn.execute(
        f"""
        WITH latest_runs AS (
          SELECT job_id, MAX(id) run_id FROM job_evidence_mapping_runs GROUP BY job_id
        ), latest_calibrations AS (
          SELECT requirement_row_id, MAX(id) calibration_id
          FROM job_requirement_calibrations GROUP BY requirement_row_id
        ), latest_reviews AS (
          SELECT requirement_row_id, MAX(id) review_id
          FROM job_requirement_human_reviews GROUP BY requirement_row_id
        )
        SELECT jobs.id job_id, jobs.title, requirements.id requirement_row_id,
               requirements.requirement_id, requirements.source_text,
               requirements.category, requirements.requirement_status,
               calibrations.id calibration_id,
               calibrations.deterministic_assessment,
               calibrations.ai_proposed_assessment,
               calibrations.final_assessment machine_final_assessment,
               calibrations.supporting_claim_ids_json,
               calibrations.unsupported_gap_claim_ids_json,
               calibrations.hard_constraint_failed,
               calibrations.hard_constraint_reason,
               calibrations.confidence, calibrations.review_reason,
               proposals.validation_status ai_validation_status,
               proposals.validation_errors_json,
               reviews.final_assessment human_final_assessment,
               reviews.review_reason human_review_reason,
               reviews.reviewer, reviews.reviewed_at
        FROM latest_runs
        JOIN job_evidence_mapping_runs runs ON runs.id=latest_runs.run_id
        JOIN jobs ON jobs.id=runs.job_id
        JOIN job_requirements requirements ON requirements.run_id=runs.id
        JOIN latest_calibrations lc ON lc.requirement_row_id=requirements.id
        JOIN job_requirement_calibrations calibrations ON calibrations.id=lc.calibration_id
        LEFT JOIN job_requirement_ai_proposals proposals ON proposals.id=calibrations.ai_proposal_id
        LEFT JOIN latest_reviews lr ON lr.requirement_row_id=requirements.id
        LEFT JOIN job_requirement_human_reviews reviews ON reviews.id=lr.review_id
        WHERE calibrations.review_status='pending' AND reviews.id IS NULL {job_filter}
        ORDER BY calibrations.hard_constraint_failed DESC, jobs.id, requirements.sequence_number
        """,
        parameters,
    ).fetchall()
    requirements = [
        {
            **{key: row[key] for key in (
                "job_id", "title", "requirement_row_id", "requirement_id",
                "source_text", "category", "requirement_status", "calibration_id",
                "deterministic_assessment", "ai_proposed_assessment",
                "machine_final_assessment", "hard_constraint_reason", "confidence",
                "review_reason", "ai_validation_status", "human_final_assessment",
                "human_review_reason", "reviewer", "reviewed_at",
            )},
            "supporting_claim_ids": json.loads(row["supporting_claim_ids_json"]),
            "unsupported_gap_claim_ids": json.loads(row["unsupported_gap_claim_ids_json"]),
            "hard_constraint_failed": bool(row["hard_constraint_failed"]),
            "ai_validation_errors": json.loads(row["validation_errors_json"] or "[]"),
            "review_status": "pending",
        }
        for row in rows
    ]
    return {"count": len(requirements), "requirements": requirements}


def review_requirement(
    conn: sqlite3.Connection,
    *,
    requirement_row_id: int,
    final_assessment: str,
    reviewer: str,
    review_reason: str,
    supporting_claim_ids: tuple[str, ...] | None = None,
    unsupported_gap_claim_ids: tuple[str, ...] | None = None,
    confidence: float = 1.0,
    candidate_evidence_path: str | Path = DEFAULT_CANDIDATE_EVIDENCE_PATH,
) -> dict[str, Any]:
    if final_assessment not in ASSESSMENTS:
        raise ValueError("invalid final assessment")
    if not 0 <= confidence <= 1:
        raise ValueError("confidence must be between 0 and 1")
    if not reviewer.strip() or not review_reason.strip():
        raise ValueError("reviewer and review reason are required")
    calibration = conn.execute(
        "SELECT * FROM job_requirement_calibrations WHERE requirement_row_id=? ORDER BY id DESC LIMIT 1",
        (requirement_row_id,),
    ).fetchone()
    if not calibration:
        raise KeyError(f"no calibration for requirement row id: {requirement_row_id}")
    if calibration["hard_constraint_failed"] and final_assessment != "contradicted":
        raise ValueError("human review cannot override a deterministic hard-constraint failure")
    facts = _evidence_facts(load_candidate_evidence(candidate_evidence_path))
    supports = supporting_claim_ids if supporting_claim_ids is not None else _json_ids(calibration["supporting_claim_ids_json"])
    gaps = unsupported_gap_claim_ids if unsupported_gap_claim_ids is not None else _json_ids(calibration["unsupported_gap_claim_ids_json"])
    unknown = [item for item in supports + gaps if item not in facts]
    if unknown:
        raise ValueError("unknown claim IDs: " + ", ".join(sorted(unknown)))
    if any(facts[item].status == "unsupported" for item in supports):
        raise ValueError("unsupported claims cannot be affirmative evidence")
    if any(facts[item].status != "unsupported" for item in gaps):
        raise ValueError("gap claims must have unsupported status")
    if set(supports) & set(gaps):
        raise ValueError("a claim cannot be both supporting evidence and a gap")
    if final_assessment in {"confirmed", "partial"} and not supports:
        raise ValueError("confirmed and partial reviews require supporting claims")
    if calibration["hard_constraint_failed"]:
        required_gaps = set(_json_ids(calibration["unsupported_gap_claim_ids_json"]))
        if not required_gaps.issubset(set(gaps)):
            raise ValueError("human review cannot omit deterministic hard-constraint gaps")
    reviewed_at = _now()
    with conn:
        cursor = conn.execute(
            """
            INSERT INTO job_requirement_human_reviews(
              requirement_row_id, calibration_id, deterministic_assessment,
              ai_proposed_assessment, final_assessment, supporting_claim_ids_json,
              unsupported_gap_claim_ids_json, hard_constraint_failed, confidence,
              review_reason, reviewer, reviewed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                requirement_row_id, calibration["id"], calibration["deterministic_assessment"],
                calibration["ai_proposed_assessment"], final_assessment,
                json.dumps(supports), json.dumps(gaps), calibration["hard_constraint_failed"],
                confidence, review_reason, reviewer, reviewed_at,
            ),
        )
        conn.execute(
            "UPDATE job_requirement_calibrations SET review_status='reviewed' WHERE id=?",
            (calibration["id"],),
        )
    return {
        "review_id": cursor.lastrowid,
        "requirement_row_id": requirement_row_id,
        "deterministic_assessment": calibration["deterministic_assessment"],
        "ai_proposed_assessment": calibration["ai_proposed_assessment"],
        "final_assessment": final_assessment,
        "reviewer": reviewer,
        "reviewed_at": reviewed_at,
        "review_status": "reviewed",
    }
