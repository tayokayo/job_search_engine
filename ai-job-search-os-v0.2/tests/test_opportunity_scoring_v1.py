from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest
import yaml

from job_os.cli import main
from job_os.mapping_calibration import NoAIProvider, calibrate_jobs, review_requirement
from job_os.opportunity_score_inspection import show_opportunity_score
from job_os.opportunity_scoring import (
    DEFAULT_SCORING_CONFIG_PATH,
    RequirementInput,
    ScoreInput,
    ScoringBlockedError,
    calculate_opportunity_score,
    load_scoring_config,
    prepare_score_input,
    score_opportunity,
)
from job_os.requirement_mapping import map_job
from job_os.score_review_planning import build_score_review_plan
from job_os.store import connect


DESCRIPTION = """Responsibilities
Python experience is required.
Marketplace platform experience is required.
Lead and grow a team of Product Managers.
Quantum computing certification is required.
"""


def _requirement(
    row_id: int,
    assessment: str,
    *,
    text: str | None = None,
    category: str = "functional_experience",
    status: str = "mandatory",
    confidence: float = 1.0,
    hard: bool = False,
    review_status: str = "reviewed",
    supporting_claim_ids: tuple[str, ...] | None = None,
    verified_leaf_claim_ids: tuple[str, ...] | None = None,
    gap_claim_ids: tuple[str, ...] | None = None,
) -> RequirementInput:
    source = text or f"Unique explicit requirement {row_id} for {assessment}."
    return RequirementInput(
        requirement_row_id=row_id,
        requirement_id=f"req-{row_id}",
        sequence_number=row_id,
        source_text=source,
        normalized_requirement=source,
        category=category,
        requirement_status=status,
        assessment=assessment,
        supporting_claim_ids=(
            supporting_claim_ids if supporting_claim_ids is not None
            else (f"claim-{row_id}",) if assessment in {"confirmed", "partial"} else ()
        ),
        verified_leaf_claim_ids=(
            verified_leaf_claim_ids if verified_leaf_claim_ids is not None
            else (f"leaf-{row_id}",) if assessment in {"confirmed", "partial"} else ()
        ),
        unsupported_gap_claim_ids=(
            gap_claim_ids if gap_claim_ids is not None
            else (f"gap-{row_id}",) if assessment in {"unsupported", "contradicted"} else ()
        ),
        confidence=confidence,
        calibration_id=row_id,
        calibration_version="calibration-v2",
        deterministic_assessment=assessment,
        ai_proposed_assessment=None,
        ai_validation_status=None,
        ai_validation_errors=(),
        review_status=review_status,
        human_review_id=row_id if review_status == "reviewed" else None,
        reviewer="test-reviewer" if review_status == "reviewed" else None,
        reviewed_at="2026-07-20T00:00:00+00:00" if review_status == "reviewed" else None,
        hard_constraint_failed=hard,
        hard_constraint_reason="validated hard constraint" if hard else None,
    )


def _input(*requirements: RequirementInput, location: str = "Tokyo, Japan") -> ScoreInput:
    return ScoreInput(
        job={
            "id": 1,
            "location": location,
            "candidate_current_location": "Tokyo, Japan",
            "candidate_current_location_status": "verified",
            "candidate_work_authorization_status": "unknown",
            "candidate_relocation_willingness_status": "unknown",
        },
        mapping_run={
            "id": 1,
            "job_content_checksum": "job-checksum",
            "candidate_evidence_checksum": "candidate-checksum",
            "mapping_version": "mapping-v5",
        },
        requirements=tuple(requirements),
        assessment_manifest_checksum="manifest-checksum",
        calibration_versions=("calibration-v2",),
    )


def _add_mapped_calibrated_job(
    conn: sqlite3.Connection,
    *,
    location: str = "Tokyo, Japan",
    description: str = DESCRIPTION,
) -> tuple[int, int]:
    digest = hashlib.sha256((location + description).encode()).hexdigest()
    job = conn.execute(
        """
        INSERT INTO jobs(source, source_id, gmail_message_id, source_url,
                         canonical_job_url, title, company, location,
                         alert_timestamp, dedupe_key)
        VALUES ('test', ?, 'message-score', 'https://example.test/jobs/score',
                'https://example.test/jobs/score', 'Product Lead', 'Example Co', ?,
                '2026-07-20T00:00:00+00:00', ?)
        """,
        (digest[:12], location, f"score-{digest}"),
    )
    job_id = job.lastrowid
    checksum = hashlib.sha256(description.encode()).hexdigest()
    snapshot = conn.execute(
        """
        INSERT INTO job_source_snapshots(
          job_id, source_url, source_type, retrieved_at, http_status,
          retrieval_status, verification_status, content_checksum, content_text,
          extracted_json, failure_reason
        ) VALUES (?, 'https://example.test/jobs/score', 'official_company',
                  '2026-07-20T00:00:00+00:00', 200, 'success',
                  'verified_official', ?, ?, '{}', NULL)
        """,
        (job_id, checksum, description),
    )
    conn.execute(
        """
        INSERT INTO job_current_fields(job_id, field_name, value_json,
                                       source_snapshot_id, selected_at)
        VALUES (?, 'job_description', ?, ?, '2026-07-20T00:00:00+00:00')
        """,
        (job_id, json.dumps(description), snapshot.lastrowid),
    )
    conn.execute(
        """
        INSERT INTO job_eligibility_decisions(
          job_id, decision, reason, verification_status,
          complete_description, decided_at
        ) VALUES (?, 'eligible', 'complete official description',
                  'verified_official', 1, '2026-07-20T00:00:00+00:00')
        """,
        (job_id,),
    )
    conn.execute(
        """
        INSERT INTO job_enrichments(
          job_id, verification_status, complete_description,
          conflict_fields_json, last_attempted_at, updated_at
        ) VALUES (?, 'verified_official', 1, '[]',
                  '2026-07-20T00:00:00+00:00', '2026-07-20T00:00:00+00:00')
        """,
        (job_id,),
    )
    conn.commit()
    mapped = map_job(conn, job_id)
    calibrated = calibrate_jobs(conn, provider=NoAIProvider(), job_ids=[job_id])
    assert calibrated["requirements"] > 0
    return job_id, mapped["run_id"]


def test_all_assessment_contributions_and_unknown_confidence_penalty():
    config = load_scoring_config()
    requirements = tuple(
        _requirement(index, assessment)
        for index, assessment in enumerate(
            ("confirmed", "partial", "unsupported", "contradicted", "unknown"), 1
        )
    )
    scored = calculate_opportunity_score(_input(*requirements), config)
    values = {
        item["assessment"]: item["fit_value"]
        for item in scored["requirement_contributions"]
    }
    assert values == {
        "confirmed": 1.0,
        "partial": 0.5,
        "unsupported": 0.0,
        "contradicted": 0.0,
        "unknown": 0.0,
    }
    assert scored["confidence_components"]["unknown_penalty"] > 0


def test_mandatory_and_preferred_multipliers_are_applied():
    scored = calculate_opportunity_score(
        _input(
            _requirement(1, "partial", status="mandatory"),
            _requirement(2, "partial", status="preferred"),
        ),
        load_scoring_config(),
    )
    manifest = scored["requirement_contributions"]
    assert manifest[0]["importance_multiplier"] == 1.0
    assert manifest[1]["importance_multiplier"] == 0.35


def test_specific_categories_are_not_displaced_by_generic_keywords():
    scored = calculate_opportunity_score(
        _input(
            _requirement(
                1,
                "confirmed",
                text="Bachelor's degree in Business or Operations is required.",
                category="education",
            ),
            _requirement(
                2,
                "partial",
                text="Lead Product Managers across business operations.",
                category="leadership_scope",
            ),
        ),
        load_scoring_config(),
    )
    manifest = scored["requirement_contributions"]
    assert manifest[0]["dimension"] == "seniority_and_scope"
    assert manifest[1]["dimension"] == "leadership_scope"


@pytest.mark.parametrize(
    "text",
    [
        "Business-level Japanese is required.",
        "Native Mandarin is required.",
        "Chinese at business level is required.",
    ],
)
def test_hard_language_failure_forces_zero_and_c(text):
    language = _requirement(1, "contradicted", text=text, category="language", hard=True)
    scored = calculate_opportunity_score(_input(language), load_scoring_config())
    assert scored["hard_constraint_failed"] is True
    assert scored["opportunity_fit_score"] == 0
    assert scored["provisional_classification"] == "C"


def test_outside_geography_is_a_visible_hard_failure():
    scored = calculate_opportunity_score(
        _input(_requirement(1, "confirmed"), location="London, United Kingdom"),
        load_scoring_config(),
    )
    gate = next(item for item in scored["hard_constraints"] if item["code"] == "outside_target_geography")
    assert gate["failed"] is True
    assert scored["opportunity_fit_score"] == 0
    assert scored["provisional_classification"] == "C"


def test_verbose_description_normalization_and_duplicate_protection():
    base = (
        _requirement(1, "confirmed", text="Own the product roadmap."),
        _requirement(2, "unsupported", text="Build quantum computing systems."),
    )
    duplicated = base + (
        _requirement(3, "unsupported", text="Build quantum computing systems."),
        _requirement(4, "unsupported", text="Build quantum computing systems."),
    )
    config = load_scoring_config()
    base_score = calculate_opportunity_score(_input(*base), config)
    verbose_score = calculate_opportunity_score(_input(*duplicated), config)
    assert verbose_score["opportunity_fit_score"] == base_score["opportunity_fit_score"]
    assert [item["reason"] for item in verbose_score["excluded_requirements"]].count("material_duplicate") == 2


def test_fit_and_confidence_are_separate_and_high_fit_low_confidence_is_b():
    certain = _requirement(1, "confirmed", confidence=1.0)
    uncertain = replace(certain, confidence=0.1, review_status="pending", human_review_id=None)
    config = load_scoring_config()
    high_confidence = calculate_opportunity_score(_input(certain), config)
    low_confidence = calculate_opportunity_score(_input(uncertain), config)
    assert low_confidence["opportunity_fit_score"] == high_confidence["opportunity_fit_score"] == 100
    assert low_confidence["evidence_confidence_score"] < high_confidence["evidence_confidence_score"]
    assert low_confidence["provisional_classification"] == "B"


def test_stale_mapping_blocks_scoring(tmp_path: Path):
    conn = connect(tmp_path / "stale.sqlite")
    job_id, _ = _add_mapped_calibrated_job(conn)
    changed = DESCRIPTION + "Five years of healthcare experience is required.\n"
    checksum = hashlib.sha256(changed.encode()).hexdigest()
    snapshot = conn.execute(
        """
        INSERT INTO job_source_snapshots(
          job_id, source_url, source_type, retrieved_at, http_status,
          retrieval_status, verification_status, content_checksum, content_text,
          extracted_json, failure_reason
        ) VALUES (?, 'https://example.test/jobs/score?v=2', 'official_company',
                  '2026-07-20T01:00:00+00:00', 200, 'success',
                  'verified_official', ?, ?, '{}', NULL)
        """,
        (job_id, checksum, changed),
    )
    conn.execute(
        "UPDATE job_current_fields SET value_json=?, source_snapshot_id=? WHERE job_id=? AND field_name='job_description'",
        (json.dumps(changed), snapshot.lastrowid, job_id),
    )
    conn.commit()
    with pytest.raises(ScoringBlockedError) as caught:
        prepare_score_input(conn, job_id)
    assert caught.value.code == "mapping_stale_job_content"
    assert conn.execute("SELECT COUNT(*) FROM opportunity_fit_scores").fetchone()[0] == 0
    conn.close()


def test_repeated_scoring_is_idempotent(tmp_path: Path):
    conn = connect(tmp_path / "repeat.sqlite")
    job_id, _ = _add_mapped_calibrated_job(conn)
    first = score_opportunity(conn, job_id)
    second = score_opportunity(conn, job_id)
    assert first["created"] is True
    assert second["created"] is False
    assert second["score_id"] == first["score_id"]
    assert conn.execute("SELECT COUNT(*) FROM opportunity_fit_scores").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM opportunity_score_review_plans").fetchone()[0] == 1
    conn.close()


def test_config_and_human_review_changes_make_score_stale(tmp_path: Path):
    conn = connect(tmp_path / "freshness.sqlite")
    job_id, run_id = _add_mapped_calibrated_job(conn)
    score_opportunity(conn, job_id)
    baseline = show_opportunity_score(conn, job_id)
    assert baseline["freshness"]["stale"] is False

    payload = yaml.safe_load(Path(DEFAULT_SCORING_CONFIG_PATH).read_text())
    payload["opportunity_fit_v1"]["classification"]["apply_minimum"] = 81
    changed_config = tmp_path / "scoring.yaml"
    changed_config.write_text(yaml.safe_dump(payload, sort_keys=False))
    changed = show_opportunity_score(conn, job_id, scoring_config_path=changed_config)
    assert "scoring_config_changed" in changed["freshness"]["reasons"]

    row = conn.execute(
        """
        SELECT requirements.id, calibrations.final_assessment
        FROM job_requirements requirements
        JOIN job_requirement_calibrations calibrations
          ON calibrations.requirement_row_id=requirements.id
        WHERE requirements.run_id=? AND calibrations.hard_constraint_failed=0
        ORDER BY requirements.id LIMIT 1
        """,
        (run_id,),
    ).fetchone()
    review_requirement(
        conn,
        requirement_row_id=row["id"],
        final_assessment=row["final_assessment"],
        reviewer="test-reviewer",
        review_reason="Confirmed no validated evidence supports the requirement.",
    )
    reviewed = show_opportunity_score(conn, job_id)
    assert "reviewed_assessment_changed" in reviewed["freshness"]["reasons"]
    replacement = score_opportunity(conn, job_id)
    assert replacement["created"] is True
    assert conn.execute("SELECT COUNT(*) FROM opportunity_fit_scores").fetchone()[0] == 2
    conn.close()


def test_read_only_score_inspection_command(tmp_path: Path, capsys):
    database = tmp_path / "inspection.sqlite"
    conn = connect(database)
    job_id, _ = _add_mapped_calibrated_job(conn)
    score_opportunity(conn, job_id)
    conn.close()
    before = database.read_bytes()
    assert main([
        "show-opportunity-score", "--job-id", str(job_id), "--db", str(database)
    ]) is None
    output = json.loads(capsys.readouterr().out)
    assert output["score"]["requirement_contributions"]
    assert output["score"]["provenance"]["scoring_config_checksum"]
    assert database.read_bytes() == before


@pytest.mark.parametrize(
    ("eligibility", "verification", "complete", "code"),
    [
        ("manual_review", "partial", True, "eligibility_blocked"),
        ("eligible", "closed", True, "closed"),
        ("eligible", "unavailable", True, "unavailable"),
        ("eligible", "verified_official", False, "description_incomplete"),
    ],
)
def test_ineligible_closed_unavailable_and_incomplete_are_blocked(
    tmp_path: Path, eligibility: str, verification: str, complete: bool, code: str
):
    conn = connect(tmp_path / f"blocked-{code}.sqlite")
    job_id, _ = _add_mapped_calibrated_job(conn)
    conn.execute(
        "UPDATE job_eligibility_decisions SET decision=?, verification_status=?, complete_description=? WHERE job_id=?",
        (eligibility, verification, int(complete), job_id),
    )
    conn.execute(
        "UPDATE job_enrichments SET verification_status=?, complete_description=? WHERE job_id=?",
        (verification, int(complete), job_id),
    )
    conn.commit()
    with pytest.raises(ScoringBlockedError) as caught:
        prepare_score_input(conn, job_id)
    assert caught.value.code == code
    conn.close()


def _review_plan(score_input: ScoreInput) -> dict:
    config = load_scoring_config()
    calculated = calculate_opportunity_score(score_input, config)
    return build_score_review_plan(score_input, calculated, config)


def test_target_geography_does_not_satisfy_current_residency_requirement():
    residency = _requirement(
        1,
        "contradicted",
        text="Candidates should already be based in Singapore.",
        category="geography",
        review_status="pending",
        supporting_claim_ids=("identity_location",),
        verified_leaf_claim_ids=("identity_location",),
        gap_claim_ids=(),
    )
    score_input = _input(residency, location="Singapore")
    calculated = calculate_opportunity_score(score_input, load_scoring_config())
    target = next(
        item for item in calculated["hard_constraints"]
        if item["feasibility_type"] == "target_geography"
    )
    residence = next(
        item for item in calculated["hard_constraints"]
        if item["feasibility_type"] == "current_residence"
    )
    assert target["status"] == "satisfied"
    assert residence["status"] == "hard_failure"
    assert residence["candidate_current_location"] == "Tokyo, Japan"
    assert calculated["opportunity_fit_score"] == 0
    assert calculated["provisional_classification"] == "C"
    plan = build_score_review_plan(score_input, calculated, load_scoring_config())
    assert plan["classification_range"] == ["C"]
    assert plan["review_priority"] == "blocking_review"
    assert plan["requirements_after_prioritization"] == 1


@pytest.mark.parametrize(
    ("text", "expected_code"),
    [
        (
            "Must have the right to work in Singapore.",
            "work_authorization_unknown",
        ),
        (
            "Must have the right to work in Singapore without sponsorship.",
            "no_sponsorship_authorization_unknown",
        ),
        (
            "Visa sponsorship is not available for this role.",
            "no_sponsorship_authorization_unknown",
        ),
        (
            "Candidates must relocate to Singapore.",
            "willingness_to_relocate_unknown",
        ),
    ],
)
def test_unknown_authorization_sponsorship_and_relocation_are_manual_blockers(
    text, expected_code
):
    requirement = _requirement(
        1,
        "unsupported",
        text=text,
        category="geography",
        review_status="pending",
        supporting_claim_ids=(),
        verified_leaf_claim_ids=(),
        gap_claim_ids=("gap_work_authorization_01",),
    )
    score_input = _input(requirement, location="Singapore")
    calculated = calculate_opportunity_score(score_input, load_scoring_config())
    blocker = next(
        item for item in calculated["hard_constraints"]
        if item["code"] == expected_code
    )
    assert blocker["status"] == "manual_blocker"
    assert blocker["failed"] is False
    assert calculated["feasibility_blocked"] is True
    assert _review_plan(score_input)["review_priority"] == "blocking_review"


def test_stable_low_potential_c_removes_blanket_unsupported_review():
    unsupported = _requirement(
        1,
        "unsupported",
        text="Quantum computing certification is required.",
        review_status="pending",
        supporting_claim_ids=(),
        verified_leaf_claim_ids=(),
        gap_claim_ids=(),
    )
    plan = _review_plan(_input(unsupported))
    assert plan["current_score"] == 0
    assert plan["plausible_upper_bound"] == 0
    assert plan["classification_range"] == ["C"]
    assert plan["review_priority"] == "no_review_needed"
    assert plan["requirements_before_prioritization"] == 1
    assert plan["requirements_after_prioritization"] == 0


def test_partial_upside_requires_cited_verified_evidence_and_no_gap():
    eligible = _requirement(1, "partial", review_status="pending")
    no_evidence = _requirement(
        2,
        "partial",
        review_status="pending",
        supporting_claim_ids=(),
        verified_leaf_claim_ids=(),
        gap_claim_ids=(),
    )
    protected = _requirement(
        3,
        "partial",
        text="Lead a team with direct reports.",
        category="leadership_scope",
        review_status="pending",
        gap_claim_ids=("gap_direct_reports_01",),
    )
    plan = _review_plan(_input(eligible, no_evidence, protected))
    unresolved = {
        item["requirement_row_id"]: item
        for item in plan["unresolved_requirements"]
    }
    assert unresolved[1]["plausible_fit_value"] == 1.0
    assert unresolved[2]["plausible_fit_value"] == 0.5
    assert unresolved[3]["plausible_fit_value"] == 0.5
    assert unresolved[3]["protected_scope"] is True


def test_classification_ranges_and_review_priorities():
    stable_b = _input(
        _requirement(1, "confirmed", review_status="reviewed"),
        _requirement(2, "partial", review_status="reviewed"),
    )
    stable_plan = _review_plan(stable_b)
    assert stable_plan["current_score"] == 75
    assert stable_plan["classification_range"] == ["B"]
    assert stable_plan["review_priority"] == "optional_review"

    crossing = _input(
        _requirement(1, "confirmed", review_status="reviewed"),
        _requirement(2, "partial", review_status="pending"),
    )
    crossing_plan = _review_plan(crossing)
    assert crossing_plan["current_score"] == 75
    assert crossing_plan["plausible_upper_bound"] == 100
    assert crossing_plan["classification_range"] == ["C", "B", "A"]
    assert crossing_plan["review_priority"] == "targeted_review"


def test_review_plan_is_capped_at_five_highest_impact_items():
    texts = (
        "Own the product roadmap.",
        "Lead customer discovery interviews.",
        "Deliver pricing experiments.",
        "Build operational dashboards.",
        "Coordinate compliance launches.",
        "Define retention initiatives.",
        "Improve checkout conversion.",
        "Manage quarterly planning.",
    )
    requirements = tuple(
        _requirement(index, "partial", text=text, review_status="pending")
        for index, text in enumerate(texts, 1)
    )
    plan = _review_plan(_input(*requirements))
    assert plan["review_priority"] == "targeted_review"
    assert len(plan["prioritized_review_items"]) == 5
    impacts = [
        item["maximum_score_impact"]
        for item in plan["prioritized_review_items"]
    ]
    assert impacts == sorted(impacts, reverse=True)


def test_read_only_review_plan_inspection_command(tmp_path: Path, capsys):
    database = tmp_path / "review-plan.sqlite"
    conn = connect(database)
    job_id, _ = _add_mapped_calibrated_job(conn)
    score_opportunity(conn, job_id)
    conn.close()
    before = database.read_bytes()
    assert main([
        "show-score-review-plan", "--job-id", str(job_id), "--db", str(database)
    ]) is None
    output = json.loads(capsys.readouterr().out)
    assert output["review_plan"]["classification_range"]
    assert len(output["review_plan"]["review_questions"]) <= 5
    assert database.read_bytes() == before
