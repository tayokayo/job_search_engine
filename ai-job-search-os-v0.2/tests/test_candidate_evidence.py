from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import pytest
import yaml

from job_os.candidate_evidence import (
    DEFAULT_CANDIDATE_EVIDENCE_PATH,
    CandidateEvidenceError,
    CandidateEvidenceValidationError,
    build_candidate_evidence_index,
    candidate_evidence_checksum,
    claim_counts_by_status,
    evidence_counts_by_category,
    load_candidate_evidence,
    validate_candidate_evidence,
    validate_claim_transformation,
)
from job_os.cli import main

CANONICAL = Path(__file__).parents[1] / "config" / "candidate_evidence.yaml"
INVALID_CLAIM = (
    Path(__file__).parent
    / "fixtures"
    / "candidate_evidence_invalid_unknown_upstream.yaml"
)


def canonical_data() -> dict:
    return yaml.safe_load(CANONICAL.read_text())


def write_artifact(tmp_path: Path, data: dict, *, sort_keys: bool = False) -> Path:
    path = tmp_path / "candidate_evidence.yaml"
    path.write_text(yaml.safe_dump(data, sort_keys=sort_keys, allow_unicode=True))
    return path


def mutated_artifact(tmp_path: Path, mutate) -> object:
    data = copy.deepcopy(canonical_data())
    mutate(data)
    return load_candidate_evidence(write_artifact(tmp_path, data))


def issue_codes(artifact) -> set[str]:
    return {issue.code for issue in validate_candidate_evidence(artifact).issues}


def test_canonical_artifact_loads_validates_and_indexes_every_id():
    artifact = load_candidate_evidence(CANONICAL)
    report = validate_candidate_evidence(artifact)
    index = build_candidate_evidence_index(artifact)
    assert report.valid
    assert artifact.schema_version == "1.1"
    assert len(index.evidence_by_id) == 7
    assert len(index.claim_by_id) == 58
    assert evidence_counts_by_category(artifact) == {
        "experience": 5,
        "project": 1,
        "education": 1,
    }
    assert claim_counts_by_status(artifact) == {
        "verified": 36,
        "derived": 10,
        "unsupported": 12,
    }
    assert len(index.upstream_claim_ids_by_claim_id) == 58
    assert sum(map(len, index.upstream_claim_ids_by_claim_id.values())) == 26
    with pytest.raises(TypeError):
        index.claim_by_id["new"] = None


def test_required_verified_leaf_resolutions():
    index = build_candidate_evidence_index(load_candidate_evidence(CANONICAL))
    assert index.resolve_verified_leaves("amazon_scope_02") == (
        "amazon_achievement_05",
        "amazon_scope_01",
    )
    assert index.resolve_verified_leaves("positioning_summary_01") == (
        "acom_scope_01",
        "amazon_achievement_06",
        "amazon_scope_01",
        "castlery_scope_01",
        "lazada_scope_01",
        "tencent_scope_01",
    )
    assert index.resolve_verified_leaves("years_experience_01") == (
        "career_start_01",
    )
    years = index.claim_by_id["years_experience_01"]
    assert years.derivation_type == "calculation"


def test_claim_index_retains_context_metrics_and_reverse_dependencies():
    index = build_candidate_evidence_index(load_candidate_evidence(CANONICAL))
    metric = index.claim_by_id["amazon_achievement_03"]
    assert metric.text == "Reduced First Mile forecast WAPE by more than 20 percentage points."
    assert metric.parent_evidence_id == "amazon_japan_program_manager"
    assert metric.category == "experience_achievement"
    assert ("company", "Amazon") in metric.context
    assert "amazon_scope_02" in index.downstream_claim_ids_by_claim_id["amazon_scope_01"]
    assert set(index.claims_by_status["unsupported"]) == {
        claim_id for claim_id in index.claim_by_id if claim_id.startswith("gap_")
    }


def test_duplicate_evidence_and_claim_ids_are_rejected(tmp_path):
    duplicate_claim = mutated_artifact(
        tmp_path,
        lambda data: data["positioning"]["claims"].append(
            copy.deepcopy(data["career_facts"]["claims"][0])
        ),
    )
    assert "duplicate_claim_id" in issue_codes(duplicate_claim)
    with pytest.raises(CandidateEvidenceValidationError):
        build_candidate_evidence_index(duplicate_claim)

    duplicate_evidence = mutated_artifact(
        tmp_path,
        lambda data: data["projects"][0].update(
            evidence_id=data["experience"][0]["evidence_id"]
        ),
    )
    assert "duplicate_evidence_id" in issue_codes(duplicate_evidence)


@pytest.mark.parametrize(
    ("upstream", "expected"),
    [
        ([], "missing_upstream"),
        (["nonexistent_claim"], "unknown_upstream"),
        (["amazon_scope_01", "amazon_scope_01"], "duplicate_upstream"),
        (["amazon_scope_02"], "self_reference"),
        (["amazon_japan_program_manager"], "unknown_upstream"),
    ],
)
def test_invalid_upstream_variants_fail_closed(tmp_path, upstream, expected):
    artifact = mutated_artifact(
        tmp_path,
        lambda data: data["experience"][0]["scope_claims"][1].update(
            derived_from_claim_ids=upstream
        ),
    )
    assert expected in issue_codes(artifact)


def test_prose_basis_is_rejected_as_an_unknown_schema_field(tmp_path):
    data = canonical_data()
    claim = data["experience"][0]["scope_claims"][1]
    claim.pop("derived_from_claim_ids")
    claim["basis"] = "Synthesized from the parent evidence record."
    with pytest.raises(CandidateEvidenceError) as exc:
        load_candidate_evidence(write_artifact(tmp_path, data))
    assert exc.value.issue.code == "schema_validation"
    assert "basis" in exc.value.issue.path


def test_deliberately_invalid_fixture_rejects_unknown_reference(tmp_path):
    invalid = yaml.safe_load(INVALID_CLAIM.read_text())
    expected = invalid.pop("expected_error")
    artifact = mutated_artifact(
        tmp_path,
        lambda data: data["positioning"]["claims"].append(invalid),
    )
    report = validate_candidate_evidence(artifact)
    assert not report.valid
    assert expected in {issue.code for issue in report.issues}


def test_direct_and_indirect_cycles_are_rejected(tmp_path):
    direct = mutated_artifact(
        tmp_path,
        lambda data: data["positioning"]["claims"].extend(
            [
                {
                    "claim_id": "cycle_a",
                    "text": "Cycle A",
                    "status": "derived",
                    "derivation_type": "synthesis",
                    "derived_from_claim_ids": ["cycle_b"],
                },
                {
                    "claim_id": "cycle_b",
                    "text": "Cycle B",
                    "status": "derived",
                    "derivation_type": "synthesis",
                    "derived_from_claim_ids": ["cycle_a"],
                },
            ]
        ),
    )
    assert "provenance_cycle" in issue_codes(direct)

    indirect = mutated_artifact(
        tmp_path,
        lambda data: data["positioning"]["claims"].extend(
            [
                {
                    "claim_id": "indirect_a",
                    "text": "Indirect A",
                    "status": "derived",
                    "derivation_type": "synthesis",
                    "derived_from_claim_ids": ["indirect_b"],
                },
                {
                    "claim_id": "indirect_b",
                    "text": "Indirect B",
                    "status": "derived",
                    "derivation_type": "synthesis",
                    "derived_from_claim_ids": ["indirect_c"],
                },
                {
                    "claim_id": "indirect_c",
                    "text": "Indirect C",
                    "status": "derived",
                    "derivation_type": "synthesis",
                    "derived_from_claim_ids": ["indirect_a"],
                },
            ]
        ),
    )
    assert "provenance_cycle" in issue_codes(indirect)


def test_derived_chain_must_not_end_in_unsupported_claim(tmp_path):
    artifact = mutated_artifact(
        tmp_path,
        lambda data: data["positioning"]["claims"][0].update(
            derived_from_claim_ids=["gap_pnl_ownership_01"]
        ),
    )
    assert "non_verified_leaf" in issue_codes(artifact)


@pytest.mark.parametrize(
    ("field", "value"),
    [("status", "certain"), ("derivation_type", "creative")],
)
def test_invalid_status_and_derivation_type_are_schema_errors(tmp_path, field, value):
    data = canonical_data()
    data["positioning"]["claims"][0][field] = value
    with pytest.raises(CandidateEvidenceError) as exc:
        load_candidate_evidence(write_artifact(tmp_path, data))
    assert exc.value.issue.category == "schema"


def test_verified_claim_cannot_carry_derived_provenance(tmp_path):
    artifact = mutated_artifact(
        tmp_path,
        lambda data: data["experience"][0]["achievement_claims"][0].update(
            derivation_type="paraphrase",
            derived_from_claim_ids=["amazon_scope_01"],
        ),
    )
    assert "unexpected_provenance" in issue_codes(artifact)


@pytest.mark.parametrize("missing_field", ["calculation_rule", "as_of_date"])
def test_calculated_claim_requires_rule_and_as_of_date(tmp_path, missing_field):
    artifact = mutated_artifact(
        tmp_path,
        lambda data: data["career_facts"]["claims"][1].pop(missing_field),
    )
    assert "missing_calculation_metadata" in issue_codes(artifact)


def test_calculated_claim_rule_value_and_source_are_validated(tmp_path):
    wrong_value = mutated_artifact(
        tmp_path,
        lambda data: data["career_facts"]["claims"][1].update(
            text="Has 12 completed years of professional experience."
        ),
    )
    assert "calculation_value_mismatch" in issue_codes(wrong_value)
    wrong_source = mutated_artifact(
        tmp_path,
        lambda data: data["career_facts"]["claims"][0].update(
            text="The career began sometime in July 2013."
        ),
    )
    assert "calculation_source_mismatch" in issue_codes(wrong_source)


def test_date_order_and_language_vocabulary_are_validated(tmp_path):
    bad_dates = mutated_artifact(
        tmp_path,
        lambda data: data["experience"][1].update(
            start_date="2025-01", end_date="2024-01"
        ),
    )
    assert "date_order" in issue_codes(bad_dates)
    malformed = mutated_artifact(
        tmp_path,
        lambda data: data["experience"][1].update(start_date="November 2021"),
    )
    assert "malformed_date" in issue_codes(malformed)
    bad_language = mutated_artifact(
        tmp_path,
        lambda data: data["languages"][3].update(proficiency="Professional-ish"),
    )
    assert "invalid_language_proficiency" in issue_codes(bad_language)


def test_checksum_ignores_yaml_formatting_and_mapping_order(tmp_path):
    data = canonical_data()
    first = load_candidate_evidence(write_artifact(tmp_path, data, sort_keys=False))
    second_path = tmp_path / "reordered.yaml"
    second_path.write_text(yaml.safe_dump(data, sort_keys=True, allow_unicode=True))
    second = load_candidate_evidence(second_path)
    assert candidate_evidence_checksum(first) == candidate_evidence_checksum(second)


def test_checksum_changes_after_semantic_change(tmp_path):
    original = load_candidate_evidence(CANONICAL)
    changed = mutated_artifact(
        tmp_path,
        lambda data: data["experience"][0]["achievement_claims"][0].update(
            text="Reduced First Mile cost per package by over 71%."
        ),
    )
    assert candidate_evidence_checksum(original) != candidate_evidence_checksum(changed)


def test_missing_malformed_and_unsupported_artifacts_fail_closed(tmp_path):
    with pytest.raises(CandidateEvidenceError) as missing:
        load_candidate_evidence(tmp_path / "missing.yaml")
    assert missing.value.issue.code == "unreadable_file"

    malformed_path = tmp_path / "malformed.yaml"
    malformed_path.write_text("schema_version: [")
    with pytest.raises(CandidateEvidenceError) as malformed:
        load_candidate_evidence(malformed_path)
    assert malformed.value.issue.code == "invalid_yaml"

    data = canonical_data()
    data["schema_version"] = "2.0"
    with pytest.raises(CandidateEvidenceError) as version:
        load_candidate_evidence(write_artifact(tmp_path, data))
    assert version.value.issue.code == "unsupported_schema_version"


def test_duplicate_yaml_keys_fail_closed(tmp_path):
    duplicate_key = tmp_path / "duplicate-key.yaml"
    duplicate_key.write_text(
        'schema_version: "1.1"\nschema_version: "1.1"\n',
        encoding="utf-8",
    )
    with pytest.raises(CandidateEvidenceError) as exc:
        load_candidate_evidence(duplicate_key)
    assert exc.value.issue.code == "invalid_yaml"


def test_contact_details_are_rejected_and_cli_redacts_them(tmp_path, capsys):
    data = canonical_data()
    data["identity"]["email"] = {
        "value": "private.person@example.test",
        "status": "verified",
    }
    data["identity"]["phone"] = {
        "value": "+81 90 1111 2222",
        "status": "verified",
    }
    path = write_artifact(tmp_path, data)
    artifact = load_candidate_evidence(path)
    assert "personal_contact" in issue_codes(artifact)
    main(["validate-candidate-evidence", "--candidate-evidence-path", str(path)])
    output = capsys.readouterr().out
    assert "private.person" not in output
    assert "1111" not in output
    assert '"valid": false' in output
    assert "personal_contact" in output


def test_metric_and_attribution_transformations_are_rejected():
    metric = validate_claim_transformation(
        "Reduced First Mile forecast WAPE by more than 20 percentage points.",
        "Reduced First Mile forecast WAPE by 20%.",
    )
    assert {issue.code for issue in metric} == {"metric_mismatch"}
    attribution = validate_claim_transformation(
        "Contributed to 30% year-on-year revenue growth.",
        "Drove 30% year-on-year revenue growth.",
    )
    assert {issue.code for issue in attribution} == {"attribution_upgrade"}
    faithful = validate_claim_transformation(
        "Contributed to 30% year-on-year revenue growth.",
        "Contributed to 30% year-on-year revenue growth.",
    )
    assert faithful == ()


def test_validation_cli_is_read_only_and_reports_only_bounded_metadata(
    tmp_path, monkeypatch, capsys
):
    before = hashlib.sha256(CANONICAL.read_bytes()).hexdigest()
    monkeypatch.chdir(tmp_path)
    main(
        [
            "validate-candidate-evidence",
            "--candidate-evidence-path",
            str(CANONICAL),
        ]
    )
    result = json.loads(capsys.readouterr().out)
    after = hashlib.sha256(CANONICAL.read_bytes()).hexdigest()
    assert result["valid"] is True
    assert result["schema_version"] == "1.1"
    assert result["provenance"] == {"nodes": 58, "edges": 26}
    assert result["errors"] == []
    assert before == after
    assert not (tmp_path / "job_os.sqlite").exists()
    assert DEFAULT_CANDIDATE_EVIDENCE_PATH == CANONICAL
