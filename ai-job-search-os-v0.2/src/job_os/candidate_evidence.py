from __future__ import annotations

import hashlib
import json
import os
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from types import MappingProxyType
from typing import Any, Iterable, Literal, Mapping

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

SUPPORTED_SCHEMA_VERSION = "1.1"
PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CANDIDATE_EVIDENCE_PATH = Path(
    os.getenv(
        "CANDIDATE_EVIDENCE_PATH",
        str(PROJECT_ROOT / "config" / "candidate_evidence.yaml"),
    )
)
CLAIM_ID_PATTERN = r"^[a-z][a-z0-9_]*$"
LANGUAGE_PROFICIENCIES = {
    "Native",
    "Fluent",
    "Business",
    "Conversational",
    "Functional",
    "Basic",
}
EMAIL_RE = re.compile(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}")
PHONE_RE = re.compile(r"\+\d[\d\s().-]{7,}\d")
CALCULATION_RE = re.compile(
    r"^completed_years_between\((\d{4}-\d{2}-\d{2}),\s*(\d{4}-\d{2}-\d{2})\)$"
)

ClaimStatus = Literal["verified", "derived", "unsupported"]
DerivationType = Literal["paraphrase", "synthesis", "calculation"]
IssueCategory = Literal["schema", "provenance", "policy"]


class FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class AtomicClaim(FrozenModel):
    claim_id: str = Field(pattern=CLAIM_ID_PATTERN)
    text: str = Field(min_length=1)
    status: ClaimStatus
    derivation_type: DerivationType | None = None
    derived_from_claim_ids: tuple[str, ...] = ()
    calculation_rule: str | None = None
    as_of_date: str | None = None


class StructuredFact(FrozenModel):
    value: str | int | tuple[str, ...]
    status: ClaimStatus


class EvidencePolicy(FrozenModel):
    purpose: str
    default_claim_status: ClaimStatus
    permitted_claim_statuses: tuple[ClaimStatus, ...]
    generation_rules: tuple[str, ...]
    provenance_rules: tuple[str, ...]


class Identity(FrozenModel):
    full_name: StructuredFact
    location: StructuredFact
    linkedin: StructuredFact
    regional_experience: StructuredFact
    email: StructuredFact | None = None
    phone: StructuredFact | None = None


class PrivacyPolicy(FrozenModel):
    version_control_policy: str
    private_contact_path: str
    commit_private_contact_file: bool


class ClaimCollection(FrozenModel):
    claims: tuple[AtomicClaim, ...]


class ExperienceEvidence(FrozenModel):
    evidence_id: str = Field(pattern=CLAIM_ID_PATTERN)
    company: str
    location: str
    title: str
    start_date: str
    end_date: str
    status: ClaimStatus
    business_or_product: str | None = None
    scope_claims: tuple[AtomicClaim, ...]
    achievement_claims: tuple[AtomicClaim, ...]


class ProjectEvidence(FrozenModel):
    evidence_id: str = Field(pattern=CLAIM_ID_PATTERN)
    name: str
    role: str
    project_type: str
    start_date: str
    end_date: str
    status: ClaimStatus
    claims: tuple[AtomicClaim, ...]


class EducationEvidence(FrozenModel):
    evidence_id: str = Field(pattern=CLAIM_ID_PATTERN)
    institution: str
    country: str
    qualification: str
    focus: str
    minor: str | None = None
    status: ClaimStatus


class LanguageEvidence(FrozenModel):
    language: str
    proficiency: str
    status: ClaimStatus


class SkillEvidence(FrozenModel):
    status: ClaimStatus
    values: tuple[str, ...]


class Maintenance(FrozenModel):
    update_method: str
    required_for_new_claim: tuple[str, ...]
    freshness_notes: tuple[str, ...]


class CandidateEvidenceDocument(FrozenModel):
    schema_version: str
    artifact_type: Literal["candidate_evidence"]
    candidate_id: str = Field(pattern=CLAIM_ID_PATTERN)
    candidate_name: str
    canonical: bool
    evidence_policy: EvidencePolicy
    identity: Identity
    privacy: PrivacyPolicy
    career_facts: ClaimCollection
    positioning: ClaimCollection
    experience: tuple[ExperienceEvidence, ...]
    projects: tuple[ProjectEvidence, ...]
    education: tuple[EducationEvidence, ...]
    languages: tuple[LanguageEvidence, ...]
    skills: Mapping[str, SkillEvidence]
    explicit_evidence_gaps: tuple[AtomicClaim, ...]
    prohibited_inferences: tuple[str, ...]
    maintenance: Maintenance


@dataclass(frozen=True)
class EvidenceIssue:
    category: IssueCategory
    code: str
    path: str
    message: str


@dataclass(frozen=True)
class ValidationReport:
    valid: bool
    issues: tuple[EvidenceIssue, ...]

    def count(self, code: str) -> int:
        return sum(issue.code == code for issue in self.issues)


class CandidateEvidenceError(ValueError):
    def __init__(self, issue: EvidenceIssue):
        super().__init__(f"{issue.path}: {issue.message}")
        self.issue = issue


class CandidateEvidenceValidationError(CandidateEvidenceError):
    def __init__(self, issues: Iterable[EvidenceIssue]):
        self.issues = tuple(issues)
        issue = self.issues[0] if self.issues else EvidenceIssue(
            "schema", "invalid_artifact", "root", "candidate evidence is invalid"
        )
        super().__init__(issue)


@dataclass(frozen=True)
class FlattenedClaim:
    claim_id: str
    text: str
    status: ClaimStatus
    derivation_type: DerivationType | None
    direct_upstream_claim_ids: tuple[str, ...]
    verified_leaf_claim_ids: tuple[str, ...]
    parent_evidence_id: str | None
    category: str
    context: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class CandidateEvidenceIndex:
    evidence_by_id: Mapping[str, Any]
    claim_by_id: Mapping[str, FlattenedClaim]
    upstream_claim_ids_by_claim_id: Mapping[str, tuple[str, ...]]
    downstream_claim_ids_by_claim_id: Mapping[str, tuple[str, ...]]
    claims_by_status: Mapping[str, tuple[str, ...]]
    claims_by_evidence_id: Mapping[str, tuple[str, ...]]

    def resolve_verified_leaves(self, claim_id: str) -> tuple[str, ...]:
        try:
            return self.claim_by_id[claim_id].verified_leaf_claim_ids
        except KeyError as exc:
            raise KeyError(f"unknown claim_id: {claim_id}") from exc


@dataclass(frozen=True)
class TransformationIssue:
    code: str
    message: str


@dataclass(frozen=True)
class _ClaimRecord:
    claim: AtomicClaim
    path: str
    parent_evidence_id: str | None
    category: str
    context: tuple[tuple[str, str], ...]


class _UniqueKeyLoader(yaml.SafeLoader):
    pass


def _construct_unique_mapping(
    loader: _UniqueKeyLoader, node: yaml.MappingNode, deep: bool = False
) -> dict[Any, Any]:
    mapping: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in mapping:
            raise yaml.constructor.ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                f"found duplicate key {key!r}",
                key_node.start_mark,
            )
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)


def _redacted_message(message: str) -> str:
    message = EMAIL_RE.sub("[REDACTED_EMAIL]", message)
    return PHONE_RE.sub("[REDACTED_PHONE]", message)


def _load_yaml(path: Path) -> Any:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise CandidateEvidenceError(
            EvidenceIssue("schema", "unreadable_file", str(path), "artifact is missing or unreadable")
        ) from exc
    try:
        return yaml.load(raw, Loader=_UniqueKeyLoader)
    except yaml.YAMLError as exc:
        raise CandidateEvidenceError(
            EvidenceIssue("schema", "invalid_yaml", str(path), "artifact contains invalid YAML")
        ) from exc


def load_candidate_evidence(
    path: str | Path = DEFAULT_CANDIDATE_EVIDENCE_PATH,
) -> CandidateEvidenceDocument:
    artifact_path = Path(path)
    data = _load_yaml(artifact_path)
    if not isinstance(data, dict):
        raise CandidateEvidenceError(
            EvidenceIssue("schema", "invalid_document", "root", "artifact must be a YAML mapping")
        )
    version = data.get("schema_version")
    if version != SUPPORTED_SCHEMA_VERSION:
        raise CandidateEvidenceError(
            EvidenceIssue(
                "schema",
                "unsupported_schema_version",
                "schema_version",
                f"expected schema version {SUPPORTED_SCHEMA_VERSION}",
            )
        )
    try:
        return CandidateEvidenceDocument.model_validate(data)
    except ValidationError as exc:
        error = exc.errors(include_url=False)[0]
        path_text = ".".join(str(part) for part in error["loc"]) or "root"
        raise CandidateEvidenceError(
            EvidenceIssue(
                "schema",
                "schema_validation",
                path_text,
                _redacted_message(error["msg"]),
            )
        ) from exc


def _claim_records(artifact: CandidateEvidenceDocument) -> tuple[_ClaimRecord, ...]:
    records: list[_ClaimRecord] = []

    def add(
        claims: Iterable[AtomicClaim],
        path: str,
        category: str,
        parent: str | None = None,
        context: Mapping[str, str] | None = None,
    ) -> None:
        context_tuple = tuple(sorted((context or {}).items()))
        for index, claim in enumerate(claims):
            records.append(
                _ClaimRecord(claim, f"{path}[{index}]", parent, category, context_tuple)
            )

    add(artifact.career_facts.claims, "career_facts.claims", "career_fact")
    add(artifact.positioning.claims, "positioning.claims", "positioning")
    for index, experience in enumerate(artifact.experience):
        context = {"company": experience.company, "title": experience.title}
        add(
            experience.scope_claims,
            f"experience[{index}].scope_claims",
            "experience_scope",
            experience.evidence_id,
            context,
        )
        add(
            experience.achievement_claims,
            f"experience[{index}].achievement_claims",
            "experience_achievement",
            experience.evidence_id,
            context,
        )
    for index, project in enumerate(artifact.projects):
        add(
            project.claims,
            f"projects[{index}].claims",
            "project",
            project.evidence_id,
            {"project": project.name},
        )
    add(
        artifact.explicit_evidence_gaps,
        "explicit_evidence_gaps",
        "evidence_gap",
    )
    return tuple(records)


def _evidence_records(
    artifact: CandidateEvidenceDocument,
) -> tuple[tuple[str, str, Any, str], ...]:
    records: list[tuple[str, str, Any, str]] = []
    for index, item in enumerate(artifact.experience):
        records.append((item.evidence_id, "experience", item, f"experience[{index}]"))
    for index, item in enumerate(artifact.projects):
        records.append((item.evidence_id, "project", item, f"projects[{index}]"))
    for index, item in enumerate(artifact.education):
        records.append((item.evidence_id, "education", item, f"education[{index}]"))
    return tuple(records)


def _partial_date(value: str, path: str, issues: list[EvidenceIssue]) -> date | None:
    if value == "present":
        return None
    try:
        if re.fullmatch(r"\d{4}-\d{2}", value):
            return date.fromisoformat(value + "-01")
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            return date.fromisoformat(value)
    except ValueError:
        pass
    issues.append(EvidenceIssue("policy", "malformed_date", path, "date must be YYYY-MM, YYYY-MM-DD, or present"))
    return None


def _validate_dates(
    artifact: CandidateEvidenceDocument, issues: list[EvidenceIssue]
) -> None:
    dated = [
        (f"experience[{index}]", item.start_date, item.end_date)
        for index, item in enumerate(artifact.experience)
    ] + [
        (f"projects[{index}]", item.start_date, item.end_date)
        for index, item in enumerate(artifact.projects)
    ]
    for path, start_raw, end_raw in dated:
        start = _partial_date(start_raw, f"{path}.start_date", issues)
        end = _partial_date(end_raw, f"{path}.end_date", issues)
        if start and end and start > end:
            issues.append(EvidenceIssue("policy", "date_order", path, "start_date must not follow end_date"))


def _validate_calculation(
    record: _ClaimRecord,
    claims: Mapping[str, _ClaimRecord],
    issues: list[EvidenceIssue],
) -> None:
    claim = record.claim
    if claim.derivation_type != "calculation":
        if claim.calculation_rule or claim.as_of_date:
            issues.append(EvidenceIssue("policy", "unexpected_calculation_metadata", record.path, "only calculated claims may carry calculation metadata"))
        return
    if not claim.calculation_rule or not claim.as_of_date:
        issues.append(EvidenceIssue("policy", "missing_calculation_metadata", record.path, "calculated claim requires calculation_rule and as_of_date"))
        return
    match = CALCULATION_RE.fullmatch(claim.calculation_rule)
    if not match:
        issues.append(EvidenceIssue("policy", "invalid_calculation_rule", record.path, "unsupported deterministic calculation rule"))
        return
    try:
        start = date.fromisoformat(match.group(1))
        end = date.fromisoformat(match.group(2))
        as_of = date.fromisoformat(claim.as_of_date)
    except ValueError:
        issues.append(EvidenceIssue("policy", "invalid_calculation_date", record.path, "calculation dates must be valid ISO dates"))
        return
    if end != as_of or start > end:
        issues.append(EvidenceIssue("policy", "calculation_date_mismatch", record.path, "calculation end date must equal as_of_date and follow start date"))
        return
    years = end.year - start.year - ((end.month, end.day) < (start.month, start.day))
    if not re.search(rf"\b{years}\b", claim.text):
        issues.append(EvidenceIssue("policy", "calculation_value_mismatch", record.path, "claim text does not contain the calculated completed-year value"))
    natural_start = f"{start.day} {start.strftime('%B')} {start.year}"
    upstream_text = " ".join(
        claims[claim_id].claim.text
        for claim_id in claim.derived_from_claim_ids
        if claim_id in claims
    )
    if natural_start not in upstream_text:
        issues.append(EvidenceIssue("policy", "calculation_source_mismatch", record.path, "calculation start date is not established by its upstream claims"))


def _contains_contact(value: Any) -> bool:
    if isinstance(value, str):
        return bool(EMAIL_RE.search(value) or PHONE_RE.search(value))
    if isinstance(value, Mapping):
        return any(_contains_contact(item) for item in value.values())
    if isinstance(value, (tuple, list)):
        return any(_contains_contact(item) for item in value)
    return False


def validate_candidate_evidence(
    artifact: CandidateEvidenceDocument,
) -> ValidationReport:
    issues: list[EvidenceIssue] = []
    claim_records = _claim_records(artifact)
    evidence_records = _evidence_records(artifact)
    claims_by_id: dict[str, _ClaimRecord] = {}
    evidence_ids: dict[str, str] = {}

    for evidence_id, _, _, path in evidence_records:
        if evidence_id in evidence_ids:
            issues.append(EvidenceIssue("schema", "duplicate_evidence_id", path, "duplicate evidence_id"))
        else:
            evidence_ids[evidence_id] = path
    for record in claim_records:
        claim_id = record.claim.claim_id
        if claim_id in claims_by_id:
            issues.append(EvidenceIssue("schema", "duplicate_claim_id", record.path, "duplicate claim_id"))
        else:
            claims_by_id[claim_id] = record

    adjacency: dict[str, tuple[str, ...]] = {}
    for record in claim_records:
        claim = record.claim
        upstream = claim.derived_from_claim_ids
        if claim.status == "derived":
            if not upstream:
                issues.append(EvidenceIssue("provenance", "missing_upstream", record.path, "derived claim requires upstream claim IDs"))
            if claim.derivation_type is None:
                issues.append(EvidenceIssue("provenance", "missing_derivation_type", record.path, "derived claim requires derivation_type"))
            if len(upstream) != len(set(upstream)):
                issues.append(EvidenceIssue("provenance", "duplicate_upstream", record.path, "derived claim repeats an upstream claim ID"))
            if claim.claim_id in upstream:
                issues.append(EvidenceIssue("provenance", "self_reference", record.path, "claim cannot cite itself"))
            for upstream_id in upstream:
                if upstream_id not in claims_by_id:
                    issues.append(EvidenceIssue("provenance", "unknown_upstream", record.path, "derived claim references an unknown claim ID"))
            adjacency[claim.claim_id] = upstream
        else:
            if upstream or claim.derivation_type is not None:
                issues.append(EvidenceIssue("provenance", "unexpected_provenance", record.path, "verified and unsupported claims cannot carry derived provenance"))
            adjacency[claim.claim_id] = ()
        _validate_calculation(record, claims_by_id, issues)

    visiting: set[str] = set()
    visited: set[str] = set()
    cycle_nodes: set[str] = set()

    def visit(claim_id: str, trail: tuple[str, ...]) -> None:
        if claim_id in visiting:
            cycle_nodes.update(trail[trail.index(claim_id):] if claim_id in trail else (claim_id,))
            return
        if claim_id in visited:
            return
        visiting.add(claim_id)
        for upstream_id in adjacency.get(claim_id, ()):
            if upstream_id in claims_by_id:
                visit(upstream_id, trail + (claim_id,))
        visiting.remove(claim_id)
        visited.add(claim_id)

    for claim_id in claims_by_id:
        visit(claim_id, ())
    for claim_id in sorted(cycle_nodes):
        issues.append(EvidenceIssue("provenance", "provenance_cycle", claims_by_id[claim_id].path, "claim participates in a provenance cycle"))

    def leaves(claim_id: str, trail: frozenset[str] = frozenset()) -> set[str]:
        if claim_id in trail or claim_id not in claims_by_id:
            return set()
        upstream = adjacency.get(claim_id, ())
        if not upstream:
            return {claim_id}
        return set().union(
            *(leaves(item, trail | {claim_id}) for item in upstream)
        )

    for claim_id, upstream in adjacency.items():
        if not upstream or claim_id in cycle_nodes:
            continue
        non_verified = [
            leaf
            for leaf in leaves(claim_id)
            if claims_by_id[leaf].claim.status != "verified"
        ]
        if non_verified:
            issues.append(EvidenceIssue("provenance", "non_verified_leaf", claims_by_id[claim_id].path, "derived provenance must terminate only in verified claims"))

    for index, gap in enumerate(artifact.explicit_evidence_gaps):
        if gap.status != "unsupported":
            issues.append(EvidenceIssue("policy", "invalid_gap_status", f"explicit_evidence_gaps[{index}]", "evidence gaps must have unsupported status"))
    for index, language in enumerate(artifact.languages):
        if language.proficiency not in LANGUAGE_PROFICIENCIES:
            issues.append(EvidenceIssue("policy", "invalid_language_proficiency", f"languages[{index}].proficiency", "language proficiency is outside the accepted vocabulary"))
    if _contains_contact(artifact.model_dump(mode="json")):
        issues.append(EvidenceIssue("policy", "personal_contact", "root", "public artifact contains a personal email address or phone number"))
    _validate_dates(artifact, issues)
    return ValidationReport(not issues, tuple(issues))


def build_candidate_evidence_index(
    artifact: CandidateEvidenceDocument,
) -> CandidateEvidenceIndex:
    report = validate_candidate_evidence(artifact)
    if not report.valid:
        raise CandidateEvidenceValidationError(report.issues)
    records = _claim_records(artifact)
    record_by_id = {record.claim.claim_id: record for record in records}
    upstream = {
        claim_id: record.claim.derived_from_claim_ids
        for claim_id, record in record_by_id.items()
    }

    def resolve(claim_id: str) -> tuple[str, ...]:
        sources = upstream[claim_id]
        if not sources:
            return (claim_id,)
        return tuple(sorted({leaf for source in sources for leaf in resolve(source)}))

    downstream_work: dict[str, set[str]] = defaultdict(set)
    for claim_id, sources in upstream.items():
        for source in sources:
            downstream_work[source].add(claim_id)
    claims_by_status_work: dict[str, list[str]] = defaultdict(list)
    claims_by_evidence_work: dict[str, list[str]] = defaultdict(list)
    flattened: dict[str, FlattenedClaim] = {}
    for claim_id, record in record_by_id.items():
        claim = record.claim
        claims_by_status_work[claim.status].append(claim_id)
        if record.parent_evidence_id:
            claims_by_evidence_work[record.parent_evidence_id].append(claim_id)
        flattened[claim_id] = FlattenedClaim(
            claim_id=claim_id,
            text=claim.text,
            status=claim.status,
            derivation_type=claim.derivation_type,
            direct_upstream_claim_ids=claim.derived_from_claim_ids,
            verified_leaf_claim_ids=resolve(claim_id),
            parent_evidence_id=record.parent_evidence_id,
            category=record.category,
            context=record.context,
        )
    evidence = {item[0]: item[2] for item in _evidence_records(artifact)}
    return CandidateEvidenceIndex(
        evidence_by_id=MappingProxyType(evidence),
        claim_by_id=MappingProxyType(flattened),
        upstream_claim_ids_by_claim_id=MappingProxyType(upstream),
        downstream_claim_ids_by_claim_id=MappingProxyType(
            {key: tuple(sorted(downstream_work.get(key, set()))) for key in flattened}
        ),
        claims_by_status=MappingProxyType(
            {key: tuple(sorted(values)) for key, values in claims_by_status_work.items()}
        ),
        claims_by_evidence_id=MappingProxyType(
            {key: tuple(sorted(values)) for key, values in claims_by_evidence_work.items()}
        ),
    )


def candidate_evidence_checksum(artifact: CandidateEvidenceDocument) -> str:
    serialized = json.dumps(
        artifact.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(serialized).hexdigest()


def validate_claim_transformation(
    source_text: str, rendered_text: str
) -> tuple[TransformationIssue, ...]:
    issues: list[TransformationIssue] = []
    metric_re = re.compile(
        r"(?:(?:more than|over|within|plus or minus)\s+)?\d[\d,]*(?:\.\d+)?(?:\s*[–-]\s*\d[\d,]*(?:\.\d+)?)?\s*(?:%|percentage points?|million|working hours?|packages?)?",
        re.I,
    )

    def metrics(value: str) -> tuple[str, ...]:
        return tuple(
            re.sub(r"\s+", " ", match.group(0).strip().lower())
            for match in metric_re.finditer(value)
            if match.group(0).strip()
        )

    if metrics(source_text) != metrics(rendered_text):
        issues.append(TransformationIssue("metric_mismatch", "numeric tokens, qualifiers, or units changed"))
    bounded_verbs = ("contributed to", "supported", "advised", "collaborated", "managed delivery")
    ownership_verbs = ("owned", "drove", "delivered", "achieved", "generated", "led company-wide")
    source_lower = source_text.lower()
    rendered_lower = rendered_text.lower()
    if any(verb in source_lower for verb in bounded_verbs) and any(
        verb in rendered_lower for verb in ownership_verbs
    ):
        issues.append(TransformationIssue("attribution_upgrade", "bounded attribution was upgraded to ownership"))
    return tuple(issues)


def evidence_counts_by_category(
    artifact: CandidateEvidenceDocument,
) -> dict[str, int]:
    return dict(Counter(category for _, category, _, _ in _evidence_records(artifact)))


def claim_counts_by_status(
    artifact: CandidateEvidenceDocument,
) -> dict[str, int]:
    return dict(Counter(record.claim.status for record in _claim_records(artifact)))
