from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from job_os.candidate_evidence import DEFAULT_CANDIDATE_EVIDENCE_PATH
from job_os.cli import main
from job_os.evidence_map_inspection import show_evidence_map
from job_os.mapping_calibration import (
    AIProposal,
    CapturedAIProvider,
    NoAIProvider,
    calibrate_jobs,
    review_requirement,
    show_review_queue,
    validate_ai_proposal,
)
from job_os.requirement_mapping import MappingBlockedError, extract_requirements, map_job, map_jobs
from job_os.store import connect


DESCRIPTION = """Responsibilities
Lead cross-functional product discovery and roadmap delivery.
Requirements
Python experience is required.
Marketplace platform experience is required.
Direct ownership of paid acquisition is required.
Business-level Japanese is required.
Lead and grow a team of Product Managers.
Reduced forecast error by more than 20% is required.
Quantum computing certification is required.
Bachelor's degree is required.
"""


def _add_job(
    conn: sqlite3.Connection,
    *,
    description: str = DESCRIPTION,
    eligibility: str = "eligible",
    complete: bool = True,
    verification: str = "verified_official",
) -> int:
    cursor = conn.execute(
        """
        INSERT INTO jobs(source, source_id, gmail_message_id, source_url,
                         canonical_job_url, title, company, location,
                         alert_timestamp, dedupe_key)
        VALUES ('test', '123', 'message-1', 'https://example.test/jobs/123',
                'https://example.test/jobs/123', 'Product Lead', 'Example Co',
                'Tokyo, Japan', '2026-07-19T00:00:00+00:00', ?)
        """,
        (f"test-{hashlib.sha256(description.encode()).hexdigest()[:12]}",),
    )
    job_id = cursor.lastrowid
    checksum = hashlib.sha256(description.encode()).hexdigest()
    snapshot = conn.execute(
        """
        INSERT INTO job_source_snapshots(
          job_id, source_url, source_type, retrieved_at, http_status,
          retrieval_status, verification_status, content_checksum, content_text,
          extracted_json, failure_reason
        ) VALUES (?, 'https://example.test/jobs/123', 'official_company',
                  '2026-07-19T00:00:00+00:00', 200, 'success', ?, ?, ?, '{}', NULL)
        """,
        (job_id, verification, checksum, description),
    )
    snapshot_id = snapshot.lastrowid
    conn.execute(
        """
        INSERT INTO job_current_fields(job_id, field_name, value_json,
                                       source_snapshot_id, selected_at)
        VALUES (?, 'job_description', ?, ?, '2026-07-19T00:00:00+00:00')
        """,
        (job_id, json.dumps(description), snapshot_id),
    )
    conn.execute(
        """
        INSERT INTO job_eligibility_decisions(job_id, decision, reason,
                                              verification_status,
                                              complete_description, decided_at)
        VALUES (?, ?, 'test decision', ?, ?, '2026-07-19T00:00:00+00:00')
        """,
        (job_id, eligibility, verification, int(complete)),
    )
    conn.commit()
    return job_id


@pytest.fixture
def mapped(tmp_path: Path):
    database = tmp_path / "mapping.sqlite"
    conn = connect(database)
    job_id = _add_job(conn)
    result = map_job(conn, job_id)
    yield conn, database, job_id, result
    conn.close()


def _by_text(conn: sqlite3.Connection, run_id: int) -> dict[str, sqlite3.Row]:
    rows = conn.execute(
        """
        SELECT requirements.source_text, requirements.category,
               mappings.*
        FROM job_requirements requirements
        JOIN job_requirement_mappings mappings
          ON mappings.requirement_row_id = requirements.id
        WHERE requirements.run_id = ?
        """,
        (run_id,),
    ).fetchall()
    return {row["source_text"]: row for row in rows}


def test_direct_partial_unsupported_language_and_missing_evidence(mapped):
    conn, _, _, result = mapped
    rows = _by_text(conn, result["run_id"])

    direct = rows["Python experience is required."]
    assert direct["assessment"] == "confirmed"
    assert json.loads(direct["supporting_claim_ids_json"]) == ["skill_python"]

    partial = rows["Lead and grow a team of Product Managers."]
    assert partial["assessment"] == "partial"
    assert "castlery_scope_02" in json.loads(partial["supporting_claim_ids_json"])
    assert "gap_direct_reports_01" in json.loads(partial["unsupported_gap_claim_ids_json"])

    unsupported = rows["Direct ownership of paid acquisition is required."]
    assert unsupported["assessment"] == "unsupported"
    assert json.loads(unsupported["supporting_claim_ids_json"]) == []
    assert json.loads(unsupported["unsupported_gap_claim_ids_json"]) == ["gap_paid_acquisition_01"]

    language = rows["Business-level Japanese is required."]
    assert language["assessment"] == "contradicted"
    assert json.loads(language["supporting_claim_ids_json"]) == ["language_japanese"]
    assert "Japanese: Functional" in language["explanation"]
    assert "gap_japanese_business_01" in json.loads(language["unsupported_gap_claim_ids_json"])

    missing = rows["Quantum computing certification is required."]
    assert missing["assessment"] == "unsupported"
    assert json.loads(missing["supporting_claim_ids_json"]) == []


def test_percentage_is_not_percentage_point_match(mapped):
    conn, _, _, result = mapped
    row = _by_text(conn, result["run_id"])["Reduced forecast error by more than 20% is required."]
    assert row["assessment"] == "unsupported"
    assert "percentage-point" in row["explanation"]
    assert json.loads(row["supporting_claim_ids_json"]) == []


def test_subsection_labels_are_not_requirements_and_preference_is_retained():
    description = """Job Description
Build recurring reports.
Requirements
Requirement: Must-Have
SQL expertise is required.
Requirement: Nice-to-Have
Python experience is a plus.
Show more
Required Skills
Navigation metadata
"""
    requirements = extract_requirements(description, "checksum")
    assert [item.source_text for item in requirements] == [
        "Build recurring reports.",
        "SQL expertise is required.",
        "Python experience is a plus.",
    ]
    assert requirements[-1].requirement_status == "preferred"
    for requirement in requirements:
        assert description[requirement.source_span_start:requirement.source_span_end] == requirement.source_text


def test_wrapped_requirement_is_one_bounded_source_span():
    description = """Requirements
Strong experience building
digital trading platforms
(e.g., retail trading apps and brokerage technology)
"""
    requirements = extract_requirements(description, "checksum")
    assert len(requirements) == 1
    requirement = requirements[0]
    assert requirement.normalized_requirement == (
        "Strong experience building digital trading platforms "
        "(e.g., retail trading apps and brokerage technology)"
    )
    assert description[requirement.source_span_start:requirement.source_span_end] == requirement.source_text


def test_postgraduate_requirement_is_not_satisfied_by_bachelors(mapped):
    conn, _, _, _ = mapped
    requirement = extract_requirements("Requirements\nMaster's degree or MBA is preferred.\n", "checksum")[0]
    from job_os.candidate_evidence import build_candidate_evidence_index, load_candidate_evidence
    from job_os.requirement_mapping import _evidence_facts, map_requirement

    artifact = load_candidate_evidence()
    assert build_candidate_evidence_index(artifact)
    decision = map_requirement(requirement, _evidence_facts(artifact))
    assert decision.assessment == "unsupported"
    assert not decision.supporting


def test_derived_claim_resolves_verified_leaves(mapped):
    conn, _, _, result = mapped
    row = _by_text(conn, result["run_id"])["Marketplace platform experience is required."]
    assert row["assessment"] == "confirmed"
    assert json.loads(row["supporting_claim_ids_json"]) == ["positioning_domain_marketplace_01"]
    leaves = json.loads(row["verified_leaf_claim_ids_json"])
    assert leaves
    assert "positioning_domain_marketplace_01" not in leaves


@pytest.mark.parametrize(
    ("eligibility", "complete"),
    [("eligible", False), ("manual_review", True), ("ineligible", True)],
)
def test_incomplete_or_ineligible_job_is_blocked(tmp_path, eligibility, complete):
    conn = connect(tmp_path / f"{eligibility}-{complete}.sqlite")
    job_id = _add_job(conn, eligibility=eligibility, complete=complete)
    with pytest.raises(MappingBlockedError, match="explicit human override required"):
        map_job(conn, job_id)
    assert conn.execute("SELECT COUNT(*) FROM job_evidence_mapping_runs").fetchone()[0] == 0
    conn.close()


def test_explicit_human_override_is_audited(tmp_path):
    conn = connect(tmp_path / "override.sqlite")
    job_id = _add_job(conn, eligibility="manual_review", complete=False, verification="partial")
    result = map_job(
        conn,
        job_id,
        human_override=True,
        override_reason="Reviewer confirmed the bounded description is sufficient.",
        override_reviewer="reviewer-1",
    )
    row = conn.execute("SELECT * FROM job_evidence_mapping_runs WHERE id = ?", (result["run_id"],)).fetchone()
    assert row["human_override"] == 1
    assert row["override_reason"].startswith("Reviewer confirmed")
    assert row["human_review_status"] == "pending"
    conn.close()


def test_repeated_mapping_is_idempotent(mapped):
    conn, _, job_id, first = mapped
    before = {
        table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in ("job_evidence_mapping_runs", "job_requirements", "job_requirement_mappings")
    }
    second = map_job(conn, job_id)
    after = {
        table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in before
    }
    assert second == {**first, "created": False}
    assert before == after


def test_mapping_stale_after_job_checksum_change(mapped):
    conn, _, job_id, _ = mapped
    changed = DESCRIPTION + "\nFive years of healthcare experience is required.\n"
    checksum = hashlib.sha256(changed.encode()).hexdigest()
    snapshot = conn.execute(
        """
        INSERT INTO job_source_snapshots(
          job_id, source_url, source_type, retrieved_at, http_status,
          retrieval_status, verification_status, content_checksum, content_text,
          extracted_json, failure_reason
        ) VALUES (?, 'https://example.test/jobs/123', 'official_company',
                  '2026-07-20T00:00:00+00:00', 200, 'success',
                  'verified_official', ?, ?, '{}', NULL)
        """,
        (job_id, checksum, changed),
    )
    conn.execute(
        "UPDATE job_current_fields SET value_json = ?, source_snapshot_id = ? WHERE job_id = ? AND field_name = 'job_description'",
        (json.dumps(changed), snapshot.lastrowid, job_id),
    )
    conn.commit()
    inspected = show_evidence_map(conn, job_id)
    assert inspected["freshness"] == {
        **inspected["freshness"], "stale": True, "reasons": ["job_content_changed"]
    }


def test_mapping_stale_after_candidate_checksum_change(mapped, tmp_path):
    conn, _, job_id, _ = mapped
    changed_path = tmp_path / "candidate_evidence.yaml"
    original = Path(DEFAULT_CANDIDATE_EVIDENCE_PATH).read_text()
    changed_path.write_text(
        original.replace("candidate_name: Tayo Kayode", "candidate_name: Tayo Kayode Test", 1)
    )
    inspected = show_evidence_map(conn, job_id, changed_path)
    assert inspected["freshness"]["stale"] is True
    assert inspected["freshness"]["reasons"] == ["candidate_evidence_changed"]


def test_read_only_inspection_command_does_not_change_database(mapped, capsys):
    conn, database, job_id, _ = mapped
    conn.close()
    before = database.read_bytes()
    assert main(["show-evidence-map", "--job-id", str(job_id), "--db", str(database)]) is None
    output = json.loads(capsys.readouterr().out)
    assert output["job"]["id"] == job_id
    assert output["mapping"]["requirements"]
    assert database.read_bytes() == before


def test_batch_maps_only_complete_eligible_jobs(tmp_path):
    conn = connect(tmp_path / "batch.sqlite")
    eligible = _add_job(conn, description=DESCRIPTION + "\nA", complete=True)
    _add_job(conn, description=DESCRIPTION + "\nB", complete=False)
    _add_job(conn, description=DESCRIPTION + "\nC", eligibility="manual_review", complete=True)
    result = map_jobs(conn)
    assert result["eligible_jobs"] == [eligible]
    assert result["mapped"] == 1
    conn.close()


@pytest.mark.parametrize(
    ("language", "level", "gap_id"),
    [
        ("Japanese", "Business", "gap_japanese_business_01"),
        ("Japanese", "Native", "gap_japanese_business_01"),
        ("Mandarin", "Business", "gap_mandarin_business_01"),
        ("Mandarin", "Native", "gap_mandarin_business_01"),
    ],
)
def test_business_and_native_language_requirements_are_hard_failures(language, level, gap_id):
    from job_os.candidate_evidence import load_candidate_evidence
    from job_os.requirement_mapping import _evidence_facts, map_requirement

    requirement = extract_requirements(
        f"Requirements\n{level}-level {language} is required.\n", "checksum"
    )[0]
    decision = map_requirement(requirement, _evidence_facts(load_candidate_evidence()))
    assert decision.assessment == "contradicted"
    assert decision.hard_constraint_failed is True
    assert gap_id in [fact.fact_id for fact in decision.gaps]


def test_language_without_level_is_conservative_and_reviewable():
    from job_os.candidate_evidence import load_candidate_evidence
    from job_os.requirement_mapping import _evidence_facts, map_requirement

    requirement = extract_requirements(
        "Requirements\nJapanese communication skills are required.\n", "checksum"
    )[0]
    decision = map_requirement(requirement, _evidence_facts(load_candidate_evidence()))
    assert decision.assessment == "partial"
    assert decision.hard_constraint_failed is False
    assert decision.human_review is True
    assert "no standardized proficiency level" in decision.explanation


def _proposal(assessment, supports=(), gaps=(), explanation="Semantic comparison.", confidence=0.8):
    raw = {
        "assessment": assessment,
        "supporting_claim_ids": list(supports),
        "unsupported_gap_claim_ids": list(gaps),
        "explanation": explanation,
        "confidence": confidence,
    }
    return AIProposal(assessment, tuple(supports), tuple(gaps), explanation, confidence, raw)


def test_ai_hard_constraint_override_is_rejected_and_stored_separately(mapped):
    conn, _, job_id, result = mapped
    rows = _by_text(conn, result["run_id"])
    language_row = rows["Business-level Japanese is required."]
    requirement_id = conn.execute(
        "SELECT requirement_id FROM job_requirements WHERE id=?", (language_row["requirement_row_id"],)
    ).fetchone()[0]
    provider = CapturedAIProvider({
        "provider": "test-ai",
        "model": "semantic-test",
        "mapper_version": "v1",
        "proposals": {
            f"{job_id}:{requirement_id}": {
                "assessment": "confirmed",
                "supporting_claim_ids": ["language_japanese"],
                "unsupported_gap_claim_ids": [],
                "explanation": "Proposes an impermissible language upgrade.",
                "confidence": 0.9,
            }
        },
    })
    calibrated = calibrate_jobs(conn, provider=provider, job_ids=[job_id])
    assert calibrated["invalid_ai_proposals"] == 1
    stored = conn.execute(
        "SELECT * FROM job_requirement_ai_proposals WHERE requirement_row_id=?",
        (language_row["requirement_row_id"],),
    ).fetchone()
    assert stored["validation_status"] == "rejected"
    assert "hard_constraint_override" in json.loads(stored["validation_errors_json"])
    final = conn.execute(
        "SELECT * FROM job_requirement_calibrations WHERE requirement_row_id=?",
        (language_row["requirement_row_id"],),
    ).fetchone()
    assert final["deterministic_assessment"] == "contradicted"
    assert final["ai_proposed_assessment"] == "confirmed"
    assert final["final_assessment"] == "contradicted"
    assert final["hard_constraint_failed"] == 1


def test_valid_ai_proposal_is_accepted_and_retained_separately(mapped):
    conn, _, job_id, result = mapped
    row = _by_text(conn, result["run_id"])["Python experience is required."]
    requirement_id = conn.execute(
        "SELECT requirement_id FROM job_requirements WHERE id=?", (row["requirement_row_id"],)
    ).fetchone()[0]
    provider = CapturedAIProvider({
        "provider": "test-ai",
        "model": "semantic-test",
        "mapper_version": "v1",
        "proposals": {
            f"{job_id}:{requirement_id}": {
                "assessment": "confirmed",
                "supporting_claim_ids": ["skill_python"],
                "unsupported_gap_claim_ids": [],
                "explanation": "The exact named skill is present as a verified structured fact.",
                "confidence": 0.95,
            }
        },
    })
    calibrate_jobs(conn, provider=provider, job_ids=[job_id])
    proposal = conn.execute(
        "SELECT * FROM job_requirement_ai_proposals WHERE requirement_row_id=?",
        (row["requirement_row_id"],),
    ).fetchone()
    calibration = conn.execute(
        "SELECT * FROM job_requirement_calibrations WHERE requirement_row_id=?",
        (row["requirement_row_id"],),
    ).fetchone()
    assert proposal["validation_status"] == "accepted"
    assert calibration["deterministic_assessment"] == "confirmed"
    assert calibration["ai_proposed_assessment"] == "confirmed"
    assert calibration["final_assessment"] == "confirmed"


def test_ai_validator_rejects_unknown_ids_gaps_as_support_and_metric_changes(mapped):
    from job_os.candidate_evidence import load_candidate_evidence
    from job_os.requirement_mapping import _evidence_facts, map_requirement

    conn, _, _, result = mapped
    facts = _evidence_facts(load_candidate_evidence())
    from job_os.mapping_calibration import _requirement_from_row

    requirements = {
        row["source_text"]: _requirement_from_row(row)
        for row in conn.execute("SELECT * FROM job_requirements WHERE run_id=?", (result["run_id"],))
    }
    paid = requirements["Direct ownership of paid acquisition is required."]
    deterministic_paid = map_requirement(paid, facts)
    errors = validate_ai_proposal(
        paid,
        deterministic_paid,
        _proposal("confirmed", ("made_up_claim", "gap_paid_acquisition_01")),
        facts,
    )
    assert any(error.startswith("unknown_supporting_claim_ids") for error in errors)
    assert "unsupported_claim_used_as_affirmative_evidence" in errors
    assert "protected_scope_upgrade" in errors

    metric = requirements["Reduced forecast error by more than 20% is required."]
    metric_errors = validate_ai_proposal(
        metric,
        map_requirement(metric, facts),
        _proposal("confirmed", ("amazon_achievement_03",)),
        facts,
    )
    assert "metric_mismatch:amazon_achievement_03" in metric_errors

    unknown_errors = validate_ai_proposal(
        requirements["Quantum computing certification is required."],
        map_requirement(requirements["Quantum computing certification is required."], facts),
        _proposal("unknown"),
        facts,
    )
    assert "unknown_for_clear_requirement" in unknown_errors

    attribution_errors = validate_ai_proposal(
        requirements["Quantum computing certification is required."],
        map_requirement(requirements["Quantum computing certification is required."], facts),
        _proposal(
            "confirmed",
            ("castlery_achievement_01",),
            explanation="The candidate owned and delivered 30% year-on-year revenue growth.",
        ),
        facts,
    )
    assert "attribution_upgrade:castlery_achievement_01" in attribution_errors


def test_clear_missing_evidence_is_unsupported_not_unknown(mapped):
    conn, _, _, result = mapped
    row = _by_text(conn, result["run_id"])["Quantum computing certification is required."]
    assert row["assessment"] == "unsupported"


def test_human_review_is_append_only_and_does_not_overwrite_machine_results(mapped):
    conn, _, job_id, result = mapped
    calibrate_jobs(conn, provider=NoAIProvider(), job_ids=[job_id])
    row = _by_text(conn, result["run_id"])["Quantum computing certification is required."]
    requirement_row_id = row["requirement_row_id"]
    before = conn.execute(
        "SELECT deterministic_assessment, ai_proposed_assessment, final_assessment FROM job_requirement_calibrations WHERE requirement_row_id=?",
        (requirement_row_id,),
    ).fetchone()
    reviewed = review_requirement(
        conn,
        requirement_row_id=requirement_row_id,
        final_assessment="unsupported",
        reviewer="local-reviewer",
        review_reason="No candidate evidence supports this clear requirement.",
    )
    after = conn.execute(
        "SELECT deterministic_assessment, ai_proposed_assessment, final_assessment FROM job_requirement_calibrations WHERE requirement_row_id=?",
        (requirement_row_id,),
    ).fetchone()
    assert tuple(before) == tuple(after)
    assert reviewed["review_status"] == "reviewed"
    assert conn.execute(
        "SELECT COUNT(*) FROM job_requirement_human_reviews WHERE requirement_row_id=?",
        (requirement_row_id,),
    ).fetchone()[0] == 1
    queue_ids = {item["requirement_row_id"] for item in show_review_queue(conn, job_ids=[job_id])["requirements"]}
    assert requirement_row_id not in queue_ids


def test_human_review_cannot_override_hard_language_failure(mapped):
    conn, _, job_id, result = mapped
    calibrate_jobs(conn, provider=NoAIProvider(), job_ids=[job_id])
    row = _by_text(conn, result["run_id"])["Business-level Japanese is required."]
    with pytest.raises(ValueError, match="cannot override"):
        review_requirement(
            conn,
            requirement_row_id=row["requirement_row_id"],
            final_assessment="partial",
            reviewer="local-reviewer",
            review_reason="Attempted override.",
        )
    with pytest.raises(ValueError, match="cannot omit"):
        review_requirement(
            conn,
            requirement_row_id=row["requirement_row_id"],
            final_assessment="contradicted",
            unsupported_gap_claim_ids=(),
            reviewer="local-reviewer",
            review_reason="Attempted gap removal.",
        )


def test_human_review_rejects_out_of_range_confidence(mapped):
    conn, _, job_id, result = mapped
    calibrate_jobs(conn, provider=NoAIProvider(), job_ids=[job_id])
    row = _by_text(conn, result["run_id"])["Quantum computing certification is required."]
    with pytest.raises(ValueError, match="between 0 and 1"):
        review_requirement(
            conn,
            requirement_row_id=row["requirement_row_id"],
            final_assessment="unsupported",
            reviewer="local-reviewer",
            review_reason="Invalid confidence.",
            confidence=1.1,
        )


def test_review_queue_cli_is_read_only(mapped, capsys):
    conn, database, job_id, _ = mapped
    calibrate_jobs(conn, provider=NoAIProvider(), job_ids=[job_id])
    conn.close()
    before = database.read_bytes()
    assert main([
        "show-evidence-review-queue", "--job-id", str(job_id), "--db", str(database)
    ]) is None
    output = json.loads(capsys.readouterr().out)
    assert output["count"] > 0
    assert database.read_bytes() == before


def test_explicit_local_review_cli_appends_decision(mapped, capsys):
    conn, database, job_id, result = mapped
    calibrate_jobs(conn, provider=NoAIProvider(), job_ids=[job_id])
    row = _by_text(conn, result["run_id"])["Quantum computing certification is required."]
    requirement_row_id = row["requirement_row_id"]
    conn.close()
    assert main([
        "review-evidence-map",
        "--requirement-row-id", str(requirement_row_id),
        "--assessment", "unsupported",
        "--reason", "No validated candidate evidence supports this requirement.",
        "--reviewer", "local-reviewer",
        "--db", str(database),
    ]) is None
    output = json.loads(capsys.readouterr().out)
    assert output["review_status"] == "reviewed"
    verify = sqlite3.connect(database)
    assert verify.execute(
        "SELECT COUNT(*) FROM job_requirement_human_reviews WHERE requirement_row_id=?",
        (requirement_row_id,),
    ).fetchone()[0] == 1
    verify.close()
