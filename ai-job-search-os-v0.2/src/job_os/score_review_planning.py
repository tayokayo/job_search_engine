from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any

from .opportunity_scoring import (
    RequirementInput,
    ScoreInput,
    ScoringConfig,
    _protected_scope,
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _numeric_band(score: float, config: ScoringConfig) -> str:
    if score >= float(config.classification["apply_minimum"]):
        return "A"
    if score >= float(config.classification["investigate_minimum"]):
        return "B"
    return "C"


def _classification_range(
    lower: float, upper: float, config: ScoringConfig
) -> list[str]:
    ordered = ("C", "B", "A")
    lower_index = ordered.index(_numeric_band(lower, config))
    upper_index = ordered.index(_numeric_band(upper, config))
    return list(ordered[lower_index:upper_index + 1])


def _upper_fit_value(
    requirement: RequirementInput, config: ScoringConfig
) -> tuple[float, str]:
    current = float(config.assessment_values[requirement.assessment])
    if requirement.review_status == "reviewed":
        return current, "human_review_is_final_for_current_evidence"
    if requirement.assessment == "confirmed":
        return current, "already_confirmed"
    if requirement.assessment in {"contradicted", "unknown"}:
        return current, f"{requirement.assessment}_has_no_fit_upside"
    has_relevant_evidence = bool(
        requirement.supporting_claim_ids and requirement.verified_leaf_claim_ids
    )
    if not has_relevant_evidence:
        return current, "no_relevant_candidate_evidence"
    if requirement.unsupported_gap_claim_ids:
        return current, "cited_evidence_does_not_resolve_recorded_gaps"
    if _protected_scope(requirement, config):
        return current, "protected_scope_cannot_be_upgraded_without_new_evidence"
    minimum = float(
        config.review_planning.get("semantic_confidence_minimum", 0.8)
    )
    if requirement.confidence < minimum:
        return current, "mapping_confidence_too_low_for_plausible_upgrade"
    if requirement.ai_validation_status == "rejected":
        return current, "invalid_ai_proposal_cannot_create_upside"
    return 1.0, "cited_verified_evidence_may_support_confirmed_after_review"


def _lower_fit_value(
    requirement: RequirementInput, config: ScoringConfig
) -> float:
    current = float(config.assessment_values[requirement.assessment])
    if requirement.review_status == "reviewed":
        return current
    if requirement.assessment == "partial":
        return 0.0
    if (
        requirement.assessment == "confirmed"
        and requirement.ai_proposed_assessment == "confirmed"
        and requirement.deterministic_assessment != "confirmed"
        and requirement.confidence < float(
            config.review_planning.get("semantic_confidence_minimum", 0.8)
        )
    ):
        return 0.0
    return current


def _question(
    requirement: RequirementInput, reason: str, max_impact: float
) -> str:
    source = " ".join(requirement.source_text.split())
    if reason == "protected_scope_unresolved":
        return (
            f"Confirm protected scope for: {source} Current evidence records an "
            "explicit gap; do not infer ownership or authority."
        )
    if reason == "contradicted_requirement":
        return f"Resolve the contradictory evidence for: {source}"
    if reason == "unknown_requirement":
        return f"Clarify the requirement and applicable candidate evidence for: {source}"
    if reason in {
        "low_confidence_semantic_mapping",
        "invalid_semantic_mapping_dependency",
    }:
        return f"Validate the semantic mapping and cited evidence for: {source}"
    return (
        f"Could the cited verified evidence support confirmed scope for: {source} "
        f"Maximum fit impact: {max_impact:.2f} points."
    )


def build_score_review_plan(
    score_input: ScoreInput,
    calculated: dict[str, Any],
    config: ScoringConfig,
) -> dict[str, Any]:
    requirements = {
        item.requirement_row_id: item for item in score_input.requirements
    }
    assessed_weight = sum(
        float(item["weight"])
        for item in calculated["dimension_breakdown"].values()
        if item["status"] == "assessed"
    )
    unresolved: list[dict[str, Any]] = []
    lower_reduction = 0.0
    upper_improvement = 0.0
    high_weight_minimum = float(
        config.review_planning.get("high_weight_dimension_minimum", 15)
    )
    material_impact_minimum = float(
        config.review_planning.get("material_score_impact_minimum", 3)
    )

    for contribution in calculated["requirement_contributions"]:
        requirement = requirements[contribution["requirement_row_id"]]
        current_value = float(contribution["fit_value"])
        upper_value, upper_reason = _upper_fit_value(requirement, config)
        lower_value = _lower_fit_value(requirement, config)
        coefficient = 0.0
        dimension_weight = 0.0
        if not contribution["excluded"] and assessed_weight:
            dimension = calculated["dimension_breakdown"][
                contribution["dimension"]
            ]
            dimension_weight = float(dimension["weight"])
            denominator = float(dimension["normalization_denominator"])
            if denominator:
                coefficient = (
                    dimension_weight
                    * float(contribution["importance_multiplier"])
                    / denominator
                    * 100
                    / assessed_weight
                )
        lower_impact = max(0.0, (current_value - lower_value) * coefficient)
        maximum_impact = max(0.0, (upper_value - current_value) * coefficient)
        lower_reduction += lower_impact
        upper_improvement += maximum_impact
        protected = _protected_scope(requirement, config)
        semantic_issue = (
            requirement.ai_validation_status == "rejected"
            and requirement.assessment == requirement.ai_proposed_assessment
        ) or (
            requirement.ai_proposed_assessment
            and requirement.ai_proposed_assessment
            != requirement.deterministic_assessment
            and requirement.confidence
            < float(config.review_planning.get("semantic_confidence_minimum", 0.8))
        )
        unresolved.append({
            "requirement_row_id": requirement.requirement_row_id,
            "requirement_id": requirement.requirement_id,
            "source_text": requirement.source_text,
            "category": requirement.category,
            "dimension": contribution["dimension"],
            "dimension_weight": dimension_weight,
            "assessment": requirement.assessment,
            "review_status": requirement.review_status,
            "current_fit_value": current_value,
            "conservative_fit_value": lower_value,
            "plausible_fit_value": upper_value,
            "maximum_score_impact": round(maximum_impact, 4),
            "maximum_downside": round(lower_impact, 4),
            "upper_bound_reason": upper_reason,
            "supporting_claim_ids": list(requirement.supporting_claim_ids),
            "verified_leaf_claim_ids": list(requirement.verified_leaf_claim_ids),
            "unsupported_gap_claim_ids": list(
                requirement.unsupported_gap_claim_ids
            ),
            "protected_scope": protected,
            "semantic_mapping_issue": bool(semantic_issue),
            "high_weight_material_impact": (
                dimension_weight >= high_weight_minimum
                and maximum_impact >= material_impact_minimum
            ),
        })

    hard_failed = bool(calculated["hard_constraint_failed"])
    current = float(calculated["opportunity_fit_score"])
    if hard_failed:
        lower = upper = 0.0
    else:
        lower = max(0.0, current - lower_reduction)
        upper = min(100.0, current + upper_improvement)
    lower = round(lower, 2)
    upper = round(upper, 2)
    classification_range = _classification_range(lower, upper, config)
    if len(classification_range) == 1:
        stability = f"stable_{classification_range[0]}"
    elif "A" in classification_range:
        stability = "crosses_A"
    elif "B" in classification_range:
        stability = "crosses_B"
    else:
        stability = "variable"

    blockers = [
        item for item in calculated["hard_constraints"]
        if item.get("blocking")
    ]
    current_band = _numeric_band(current, config)
    upper_band = _numeric_band(upper, config)
    classification_can_change = current_band != upper_band
    candidate_items: list[dict[str, Any]] = []

    for blocker in blockers:
        question = (
            "Confirm current residence against the posting's explicit residency "
            f"condition: {blocker.get('source_text')} Verified location: "
            f"{blocker.get('candidate_current_location') or 'unknown'}."
            if blocker.get("feasibility_type") == "current_residence"
            else f"Resolve feasibility before classification: {blocker.get('reason')}."
        )
        candidate_items.append({
            "priority_reason": blocker["code"],
            "blocking": True,
            "requirement_row_id": blocker.get("requirement_row_id"),
            "requirement_id": blocker.get("requirement_id"),
            "source_text": blocker.get("source_text"),
            "maximum_score_impact": 0.0,
            "classification_impact": "non_compensating_feasibility_gate",
            "question": question,
        })

    blocker_requirement_ids = {
        item.get("requirement_row_id") for item in blockers
        if item.get("requirement_row_id") is not None
    }
    for item in unresolved:
        requirement = requirements[item["requirement_row_id"]]
        if requirement.requirement_row_id in blocker_requirement_ids:
            continue
        if blockers:
            continue
        if requirement.review_status == "reviewed":
            continue
        reason: str | None = None
        if item["protected_scope"]:
            reason = "protected_scope_unresolved"
        elif requirement.assessment == "contradicted":
            reason = "contradicted_requirement"
        elif (
            requirement.assessment == "unknown"
            and requirement.requirement_status != "preferred"
        ):
            reason = "unknown_requirement"
        elif item["semantic_mapping_issue"]:
            reason = "low_confidence_semantic_mapping"
        elif item["high_weight_material_impact"]:
            reason = "high_weight_dimension_impact"
        elif classification_can_change and item["maximum_score_impact"] > 0:
            reason = "classification_range_impact"
        if reason:
            candidate_items.append({
                "priority_reason": reason,
                "blocking": False,
                "requirement_row_id": requirement.requirement_row_id,
                "requirement_id": requirement.requirement_id,
                "source_text": requirement.source_text,
                "dimension": item["dimension"],
                "dimension_weight": item["dimension_weight"],
                "current_assessment": requirement.assessment,
                "maximum_score_impact": item["maximum_score_impact"],
                "classification_impact": (
                    "may_change_classification" if classification_can_change
                    else "material_dimension_review"
                ),
                "supporting_claim_ids": item["supporting_claim_ids"],
                "verified_leaf_claim_ids": item["verified_leaf_claim_ids"],
                "unsupported_gap_claim_ids": item[
                    "unsupported_gap_claim_ids"
                ],
                "question": _question(
                    requirement, reason, item["maximum_score_impact"]
                ),
            })

    has_protected_or_semantic = any(
        item["priority_reason"] in {
            "protected_scope_unresolved",
            "contradicted_requirement",
            "unknown_requirement",
            "low_confidence_semantic_mapping",
            "invalid_semantic_mapping_dependency",
        }
        for item in candidate_items
    )
    if blockers or (
        current >= float(config.classification["apply_minimum"])
        and candidate_items
    ):
        priority = "blocking_review"
    elif classification_can_change or has_protected_or_semantic:
        priority = "targeted_review"
    elif classification_range == ["B"]:
        priority = "optional_review"
    else:
        priority = "no_review_needed"

    if priority == "no_review_needed":
        prioritized: list[dict[str, Any]] = []
    else:
        candidate_items.sort(
            key=lambda item: (
                not item["blocking"],
                -float(item["maximum_score_impact"]),
                -float(item.get("dimension_weight") or 0),
                int(item.get("requirement_row_id") or 0),
            )
        )
        prioritized = candidate_items[
            : int(config.review_planning.get("max_items", 5))
        ]

    apply_minimum = float(config.classification["apply_minimum"])
    investigate_minimum = float(config.classification["investigate_minimum"])
    next_threshold = (
        investigate_minimum if current < investigate_minimum
        else apply_minimum if current < apply_minimum else current
    )
    score_needed = round(max(0.0, next_threshold - current), 2)
    return {
        "planning_version": str(
            config.review_planning.get("version", "score-review-plan-v1")
        ),
        "current_score": round(current, 2),
        "conservative_lower_bound": lower,
        "plausible_upper_bound": upper,
        "classification_range": classification_range,
        "classification_stability": stability,
        "score_needed_next_band": score_needed,
        "next_band_points_cannot_override_blocker": bool(blockers),
        "review_priority": priority,
        "feasibility_results": calculated["hard_constraints"],
        "blockers": blockers,
        "unresolved_requirements": unresolved,
        "prioritized_review_items": prioritized,
        "requirements_before_prioritization": sum(
            item.review_status == "pending" for item in score_input.requirements
        ),
        "requirements_after_prioritization": len(prioritized),
    }


def persist_score_review_plan(
    conn: sqlite3.Connection,
    score_id: int,
    score_input: ScoreInput,
    calculated: dict[str, Any],
    config: ScoringConfig,
) -> tuple[dict[str, Any], bool, int]:
    plan = build_score_review_plan(score_input, calculated, config)
    existing = conn.execute(
        """
        SELECT id FROM opportunity_score_review_plans
        WHERE score_id=? AND planning_version=? AND scoring_config_checksum=?
        """,
        (score_id, plan["planning_version"], config.checksum),
    ).fetchone()
    if existing:
        return plan, False, existing["id"]
    with conn:
        cursor = conn.execute(
            """
            INSERT INTO opportunity_score_review_plans(
              score_id, planning_version, scoring_config_checksum,
              current_score, conservative_lower_bound, plausible_upper_bound,
              classification_range_json, classification_stability,
              score_needed_next_band, review_priority, feasibility_results_json,
              blockers_json, unresolved_manifest_json,
              prioritized_review_items_json,
              requirements_before_prioritization,
              requirements_after_prioritization, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                score_id, plan["planning_version"], config.checksum,
                plan["current_score"], plan["conservative_lower_bound"],
                plan["plausible_upper_bound"],
                json.dumps(plan["classification_range"]),
                plan["classification_stability"],
                plan["score_needed_next_band"], plan["review_priority"],
                json.dumps(plan["feasibility_results"], ensure_ascii=False),
                json.dumps(plan["blockers"], ensure_ascii=False),
                json.dumps(plan["unresolved_requirements"], ensure_ascii=False),
                json.dumps(plan["prioritized_review_items"], ensure_ascii=False),
                plan["requirements_before_prioritization"],
                plan["requirements_after_prioritization"], _now(),
            ),
        )
    return plan, True, int(cursor.lastrowid)
