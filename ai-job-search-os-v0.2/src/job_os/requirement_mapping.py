from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .candidate_evidence import (
    DEFAULT_CANDIDATE_EVIDENCE_PATH,
    CandidateEvidenceDocument,
    build_candidate_evidence_index,
    candidate_evidence_checksum,
    load_candidate_evidence,
)

EXTRACTION_VERSION = "deterministic-explicit-v4"
MAPPING_VERSION = "candidate-evidence-v5"
ELIGIBLE_DECISIONS = {"eligible", "conditionally_eligible"}

class MappingBlockedError(ValueError):
    pass


@dataclass(frozen=True)
class Requirement:
    requirement_id: str
    sequence_number: int
    source_text: str
    source_span_start: int
    source_span_end: int
    normalized_requirement: str
    category: str
    importance: str
    requirement_status: str
    extraction_confidence: float


@dataclass(frozen=True)
class _SourceUnit:
    text: str
    start: int
    end: int


@dataclass(frozen=True)
class EvidenceFact:
    fact_id: str
    text: str
    status: str
    verified_leaf_ids: tuple[str, ...]
    category: str


@dataclass(frozen=True)
class MappingDecision:
    assessment: str
    supporting: tuple[EvidenceFact, ...]
    verified_leaves: tuple[EvidenceFact, ...]
    gaps: tuple[EvidenceFact, ...]
    explanation: str
    confidence: float
    human_review: bool
    hard_constraint_failed: bool = False
    hard_constraint_reason: str | None = None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")


def _evidence_facts(artifact: CandidateEvidenceDocument) -> dict[str, EvidenceFact]:
    index = build_candidate_evidence_index(artifact)
    facts = {
        claim_id: EvidenceFact(
            claim_id,
            claim.text,
            claim.status,
            claim.verified_leaf_claim_ids if claim.status != "unsupported" else (),
            claim.category,
        )
        for claim_id, claim in index.claim_by_id.items()
    }
    for language in artifact.languages:
        fact_id = f"language_{_slug(language.language)}"
        facts[fact_id] = EvidenceFact(
            fact_id,
            f"{language.language} proficiency: {language.proficiency}.",
            language.status,
            (fact_id,) if language.status == "verified" else (),
            "language",
        )
    for group, skill_group in artifact.skills.items():
        for value in skill_group.values:
            fact_id = f"skill_{_slug(value)}"
            facts[fact_id] = EvidenceFact(
                fact_id,
                f"Verified skill: {value}.",
                skill_group.status,
                (fact_id,) if skill_group.status == "verified" else (),
                f"skills.{group}",
            )
    for education in artifact.education:
        facts[education.evidence_id] = EvidenceFact(
            education.evidence_id,
            f"{education.qualification}, {education.institution} ({education.focus}).",
            education.status,
            (education.evidence_id,) if education.status == "verified" else (),
            "education",
        )
    structured = {
        "identity_location": artifact.identity.location,
        "identity_regional_experience": artifact.identity.regional_experience,
    }
    for fact_id, value in structured.items():
        rendered = value.value if isinstance(value.value, str) else ", ".join(value.value)
        facts[fact_id] = EvidenceFact(
            fact_id,
            str(rendered),
            value.status,
            (fact_id,) if value.status == "verified" else (),
            "identity",
        )
    return facts


RESPONSIBILITY_HEADINGS = re.compile(
    r"^(?:job description|(?:key\s+)?responsibilit(?:y|ies)|what you(?:'|’)ll do|the role|role responsibilities)$",
    re.I,
)
REQUIREMENT_HEADINGS = re.compile(
    r"^(?:key\s+)?(?:requirements?|qualifications?|what you(?:'|’)ll bring|about you|who you are)$",
    re.I,
)
PREFERRED_HEADINGS = re.compile(r"^(?:preferred|nice[- ]to[- ]have|desirable)(?: qualifications?)?:?$", re.I)
MANDATORY_HEADINGS = re.compile(r"^(?:must[- ]have|minimum qualifications?)(?: requirements?)?:?$", re.I)
STOP_HEADINGS = re.compile(
    r"^(?:why join|benefits|about (?:the company|us)|to apply|how to apply|for more information|equal opportunity|show more|show less|apply now|privacy)",
    re.I,
)
BOILERPLATE = re.compile(
    r"(?:equal opportunity|all qualified applicants|privacy policy|cookie|subscribe|unsubscribe|@|apply now|click here)",
    re.I,
)
RESPONSIBILITY_VERBS = re.compile(
    r"^(?:you will|working closely|lead|own|develop|define|drive|manage|build|partner|collaborate|conduct|translate|establish|oversee|analyze|analyse|create|deliver|support|work|identify|ensure|shape|set|mentor|guide|monitor|optimi[sz]e|coordinate|communicate|present|facilitate|perform|maintain|validate|proactively debug|document|contribute|evaluate)\b",
    re.I,
)
def _category(text: str, section: str) -> str:
    lowered = text.lower()
    if re.search(r"\b(?:english|thai|japanese|mandarin|chinese|language|bilingual|fluency|fluent)\b", lowered):
        return "language"
    if re.search(r"\b\d+\s*(?:\+|plus)?\s*(?:-|–|to)?\s*\d*\s*years?\b", lowered):
        return "years_of_experience"
    if re.search(r"\b(?:bachelor|master|mba|degree|education)\b", lowered):
        return "education"
    if re.search(r"\b(?:based in|location|singapore|bangkok|thailand|japan|tokyo|asia|sea|southeast asia|travel|geograph)\b", lowered):
        return "geography"
    if re.search(r"\b(?:p&l|profit and loss|budget|revenue|commercial ownership|operational ownership)\b", lowered):
        return "commercial_or_operational_ownership"
    if re.search(r"\b(?:sql|python|tableau|power bi|excel|analytics tools?|data tools?)\b", lowered):
        return "technical_skills"
    if re.search(r"\b(?:lead(?:er|ership)?|manage|mentor|direct reports?|grow a team|people management|head of)\b", lowered):
        return "leadership_scope"
    if re.search(r"\b(?:senior|executive|c-suite|director|head|strategic leadership)\b", lowered):
        return "seniority"
    if re.search(r"\b(?:marketplace|e-?commerce|fintech|healthcare|regulated|food delivery|trading platform|logistics|consumer)\b", lowered):
        return "domain_experience"
    if re.search(r"\b(?:product management|product strategy|discovery|roadmap|mvp|experimentation|analytics|stakeholder|cross-functional|communication|mece)\b", lowered):
        return "functional_experience"
    if section == "responsibilities":
        return "responsibilities"
    return "other_explicit_constraints"


def _source_units(description: str) -> Iterable[_SourceUnit]:
    lines = list(re.finditer(r"[^\r\n]+", description))
    index = 0
    while index < len(lines):
        line = lines[index]
        start, end = line.start(), line.end()
        raw = description[start:end]
        while index + 1 < len(lines):
            next_line = lines[index + 1]
            next_text = next_line.group(0).strip()
            continuation = bool(
                re.search(r"\b(?:building|with|and|or|to|of|for|across|including)\s*$", raw.strip(), re.I)
                or next_text.startswith("(")
            )
            if not continuation:
                break
            index += 1
            end = next_line.end()
            raw = description[start:end]
        cursor = 0
        separators = list(re.finditer(r";|(?<=[.!?])\s+(?=[A-Z])", raw))
        for separator in separators:
            boundary = separator.end() if separator.group(0) == ";" else separator.start()
            if raw[cursor:boundary].strip():
                yield _SourceUnit(raw[cursor:boundary], line.start() + cursor, line.start() + boundary)
            cursor = separator.end()
        if raw[cursor:].strip():
            yield _SourceUnit(raw[cursor:], start + cursor, end)
        index += 1


def extract_requirements(description: str, job_checksum: str) -> tuple[Requirement, ...]:
    requirements: list[Requirement] = []
    section = ""
    section_status = "unspecified"
    for unit in _source_units(description):
        raw = unit.text
        leading = raw.lstrip()
        whitespace_count = len(raw) - len(leading)
        bullet = re.match(r"(?:[•*\-–—]\s+|\d+[.)]\s+)", leading)
        prefix_count = whitespace_count + (bullet.end() if bullet else 0)
        stripped = raw[prefix_count:].strip()
        if not stripped:
            continue
        heading = stripped.rstrip(":").strip()
        requirement_control = re.match(
            r"^(?:requirement:\s*)?(must[- ]have|nice[- ]to[- ]have|required|preferred)$",
            heading,
            re.I,
        )
        if requirement_control:
            section = "requirements"
            section_status = "preferred" if "nice" in requirement_control.group(1).lower() or "preferred" in requirement_control.group(1).lower() else "mandatory"
            continue
        if RESPONSIBILITY_HEADINGS.match(heading):
            section, section_status = "responsibilities", "mandatory"
            continue
        if REQUIREMENT_HEADINGS.match(heading):
            section, section_status = "requirements", "mandatory"
            continue
        if PREFERRED_HEADINGS.match(heading):
            section, section_status = "requirements", "preferred"
            continue
        if MANDATORY_HEADINGS.match(heading):
            section, section_status = "requirements", "mandatory"
            continue
        if STOP_HEADINGS.match(heading):
            break
        if re.match(r"^(?:education|experience and skills|other|required skills|preferred skills|where required to be managed locally)$", heading, re.I):
            if heading.lower().startswith("preferred"):
                section_status = "preferred"
            elif heading.lower().startswith("required"):
                section_status = "mandatory"
            continue
        if BOILERPLATE.search(stripped) or len(stripped) < 5:
            continue
        explicit = section == "requirements" or (
            section == "responsibilities" and (RESPONSIBILITY_VERBS.match(stripped) or ":" in stripped)
        ) or (
            not section
            and re.search(r"\b(?:candidates?|applicants?)\s+(?:must|should|required)|\b(?:is|are) required\b", stripped, re.I)
        )
        if not explicit:
            continue
        status = section_status
        if re.search(r"\b(?:preferred|nice[- ]to[- ]have|desirable|a plus)\b", stripped, re.I):
            status = "preferred"
        elif re.search(r"\b(?:required|must|minimum|need to)\b", stripped, re.I):
            status = "mandatory"
        category = _category(stripped, section)
        normalized = re.sub(r"\s+", " ", stripped)
        start = unit.start + prefix_count
        end = start + len(stripped)
        sequence = len(requirements) + 1
        digest = hashlib.sha256(
            f"{job_checksum}|{start}|{normalized}".encode("utf-8")
        ).hexdigest()[:10]
        requirements.append(
            Requirement(
                f"req_{sequence:03d}_{digest}", sequence, stripped, start, end,
                normalized, category,
                "high" if status == "mandatory" else "medium",
                status,
                0.98 if re.search(r"\b(?:required|must|preferred)\b", stripped, re.I) else 0.9,
            )
        )
    return tuple(requirements)


def _selected(facts: dict[str, EvidenceFact], ids: Iterable[str]) -> tuple[EvidenceFact, ...]:
    return tuple(facts[item] for item in ids if item in facts)


def _decision(
    assessment: str,
    facts: dict[str, EvidenceFact],
    supporting_ids: Iterable[str] = (),
    gap_ids: Iterable[str] = (),
    explanation: str = "",
    confidence: float = 0.5,
    hard_constraint_failed: bool = False,
    hard_constraint_reason: str | None = None,
) -> MappingDecision:
    supporting = _selected(facts, supporting_ids)
    gaps = _selected(facts, gap_ids)
    leaf_ids = tuple(dict.fromkeys(
        leaf_id for fact in supporting for leaf_id in fact.verified_leaf_ids
    ))
    leaves = _selected(facts, leaf_ids)
    return MappingDecision(
        assessment, supporting, leaves, gaps, explanation, confidence,
        assessment != "confirmed",
        hard_constraint_failed,
        hard_constraint_reason,
    )


LANGUAGE_RANK = {"Basic": 1, "Functional": 2, "Conversational": 3, "Business": 4, "Fluent": 5, "Native": 6}


def map_requirement(requirement: Requirement, facts: dict[str, EvidenceFact]) -> MappingDecision:
    text = requirement.normalized_requirement
    lowered = text.lower()

    gap_rules = (
        (r"paid acquisition", "gap_paid_acquisition_01"),
        (r"\bp&l\b|profit and loss", "gap_pnl_ownership_01"),
        (r"\bbudget", "gap_budget_01"),
        (r"engineering management|manage engineers|engineering team", "gap_software_engineering_management_01"),
        (r"ml engineering leadership|machine learning engineering leadership", "gap_ml_engineering_leadership_01"),
        (r"board reporting", "gap_board_reporting_01"),
        (r"work authorization|right to work|visa", "gap_work_authorization_01"),
    )
    for pattern, gap_id in gap_rules:
        if re.search(pattern, lowered):
            return _decision(
                "unsupported", facts, gap_ids=(gap_id,),
                explanation="The validated evidence index explicitly records this scope as unsupported; it is a gap, not affirmative evidence.",
                confidence=0.99,
            )

    named_languages = tuple(language for language in ("English", "Thai", "Japanese", "Mandarin") if language.lower() in lowered)
    if named_languages:
        requested = next((level for level in LANGUAGE_RANK if re.search(rf"\b{level}(?:[- ]level)?\b", text, re.I)), None)
        if requested is None:
            language_ids = tuple(f"language_{language.lower()}" for language in named_languages)
            present = tuple(item for item in language_ids if item in facts)
            return _decision(
                "partial", facts, present,
                explanation="The posting names a language but no standardized proficiency level. Exact validated levels are retained without assuming that they satisfy the unstated threshold.",
                confidence=0.9,
            )
        language_ids = tuple(f"language_{language.lower()}" for language in named_languages)
        actual_levels: dict[str, str] = {}
        missing: list[str] = []
        failed: list[str] = []
        gap_ids: list[str] = []
        for language, fact_id in zip(named_languages, language_ids):
            fact = facts.get(fact_id)
            if not fact:
                missing.append(language)
                continue
            actual_match = re.search(r"proficiency: ([A-Za-z]+)", fact.text)
            actual = actual_match.group(1) if actual_match else ""
            actual_levels[language] = actual
            if LANGUAGE_RANK.get(actual, 0) < LANGUAGE_RANK[requested]:
                failed.append(language)
                if language in {"Japanese", "Mandarin"} and requested in {"Business", "Native"}:
                    gap_id = f"gap_{language.lower()}_business_01"
                    if gap_id in facts:
                        gap_ids.append(gap_id)
        if failed:
            rendered = ", ".join(f"{language}: {actual_levels.get(language, 'no evidence')}" for language in failed)
            return _decision(
                "contradicted", facts, language_ids, gap_ids,
                f"The requirement explicitly asks for {requested} proficiency; validated evidence is lower ({rendered}). Lower proficiency is not upgraded or treated as partial.",
                0.99,
                hard_constraint_failed=True,
                hard_constraint_reason=f"required_{requested.lower()}_language_level_not_met",
            )
        if missing:
            return _decision(
                "unsupported", facts, language_ids,
                explanation=f"The explicit {requested} requirement is clear, but no validated proficiency fact exists for: {', '.join(missing)}.",
                confidence=0.98,
                hard_constraint_failed=True,
                hard_constraint_reason=f"required_{requested.lower()}_language_evidence_absent",
            )
        rendered = ", ".join(f"{language}: {actual_levels[language]}" for language in named_languages)
        return _decision(
            "confirmed", facts, language_ids,
            explanation=f"Validated evidence meets the explicit {requested} threshold ({rendered}).",
            confidence=0.98,
        )

    skill_aliases = {
        "python": "skill_python", "sql": "skill_sql", "tableau": "skill_tableau",
        "power bi": "skill_power_bi", "excel": "skill_microsoft_excel",
        "cte": "skill_sql", "ctes": "skill_sql", "window functions": "skill_sql", "multi-table joins": "skill_sql",
    }
    matched_skills = tuple(dict.fromkeys(fact_id for token, fact_id in skill_aliases.items() if re.search(rf"\b{re.escape(token)}\b", lowered)))
    if matched_skills:
        present = tuple(fact_id for fact_id in matched_skills if fact_id in facts)
        if len(present) == len(matched_skills):
            return _decision("confirmed", facts, present, explanation="Each named technical skill is a verified structured fact in the candidate-evidence index.", confidence=0.98)
        if not present:
            return _decision("unsupported", facts, explanation="The technical requirement is clear, but none of its named skills has a verified candidate-evidence fact.", confidence=0.9)
        return _decision("partial", facts, present, explanation="Only some named technical skills have verified evidence.", confidence=0.85)

    if re.search(r"\b(?:master|mba|doctorate|phd)\b", lowered):
        return _decision("unsupported", facts, explanation="The postgraduate requirement is clear, and the validated education index does not contain that qualification.", confidence=0.97)
    if re.search(r"\b(?:bachelor|degree)\b", lowered):
        named_fields = re.findall(
            r"\b(?:business administration|business|finance|operations|economics|analytics|engineering|statistics)\b",
            lowered,
        )
        exact_field = "economics" in named_fields
        assessment = "confirmed" if not named_fields or exact_field else "partial"
        explanation = (
            "The validated education record is a BA (Hons) in Economics, which exactly matches a listed degree field."
            if exact_field
            else "The validated education record contains a bachelor's degree, but its Economics field is not an exact match to the posting's named fields."
            if named_fields
            else "The validated education record contains a bachelor's degree."
        )
        return _decision(assessment, facts, ("education_essex",), explanation=explanation, confidence=0.96)

    if re.search(r"\b\d+\s*(?:\+|plus)?\s*(?:-|–|to)?\s*\d*\s*years?\b", lowered):
        numbers = [int(value) for value in re.findall(r"\b(\d+)\b", lowered)]
        minimum = min(numbers) if numbers else 0
        combined_leadership_duration = len(re.findall(r"years?", lowered)) > 1 or "leadership" in lowered
        assessment = "confirmed" if minimum <= 13 and not combined_leadership_duration else "partial"
        supporting_ids = ("years_experience_01", "castlery_scope_02") if combined_leadership_duration else ("years_experience_01",)
        return _decision(
            assessment, facts, supporting_ids,
            explanation=f"A validated derived claim records 13 completed years and resolves to its verified career-start leaf; leadership scope is supported but its duration is not inferred. The lowest stated threshold is {minimum} years.",
            confidence=0.96,
        )

    if re.search(r"already be based in singapore|must be based in singapore", lowered):
        return _decision(
            "contradicted", facts, ("identity_location",),
            explanation="The posting requires the candidate already to be based in Singapore, while the validated current-location fact is Tokyo, Japan.",
            confidence=0.99,
        )

    if re.search(r"direct reports?|people authority|hire and fire", lowered):
        return _decision("unsupported", facts, gap_ids=("gap_direct_reports_01", "gap_people_authority_01"), explanation="Direct-report count and formal people authority are explicit evidence gaps.", confidence=0.99)
    if re.search(r"\b(?:lead|manage|mentor)\b|grow a team|develop product managers", lowered) and re.search(r"team|product managers?|people", lowered):
        return _decision(
            "partial", facts, ("castlery_scope_02",), ("gap_direct_reports_01", "gap_people_authority_01"),
            "Validated evidence supports team-building and coaching, but direct reports and formal people authority remain explicit gaps.", 0.94,
        )

    if re.search(r"\b\d+(?:\.\d+)?\s*%", lowered):
        return _decision(
            "unsupported", facts,
            explanation="No exact metric match was found. Percentage and percentage-point evidence are not treated as interchangeable.",
            confidence=0.97,
        )

    keyword_claims = (
        (r"marketplace", "positioning_domain_marketplace_01"),
        (r"e-?commerce", "positioning_domain_ecommerce_01"),
        (r"logistics|supply chain", "positioning_domain_logistics_01"),
        (r"planning|forecast|s&op", "positioning_domain_planning_01"),
        (r"consumer|user research", "positioning_domain_consumer_01"),
        (r"\bux\b|omnichannel", "positioning_domain_ux_01"),
        (r"\bai\b|llm|nlp", "positioning_domain_ai_01"),
        (r"cross-functional", "tencent_achievement_03"),
        (r"product management|product strategy|discovery|roadmap|mvp|product lifecycle", "castlery_scope_01"),
        (r"analytics|data-driven|data insights", "amazon_scope_02"),
        (r"southeast asia|\bsea\b|regional|multi-country", "identity_regional_experience"),
    )
    claim_ids = tuple(dict.fromkeys(claim_id for pattern, claim_id in keyword_claims if re.search(pattern, lowered)))
    if claim_ids:
        assessment = (
            "partial"
            if requirement.category in {"responsibilities", "leadership_scope", "seniority", "other_explicit_constraints"}
            or RESPONSIBILITY_VERBS.match(text)
            else "confirmed"
        )
        return _decision(
            assessment, facts, claim_ids,
            explanation="Validated evidence contains directly relevant claims; every derived claim is resolved to its verified leaf claims.",
            confidence=0.88,
        )

    if requirement.category == "language" or len(text.split()) < 3:
        return _decision(
            "unknown", facts,
            explanation="The source requirement is too ambiguous to identify a specific proficiency threshold or evidence target.",
            confidence=0.7,
        )
    return _decision(
        "unsupported", facts,
        explanation="The requirement is clear, but no validated candidate fact directly supports it; absence of evidence is recorded as unsupported, not as a match.",
        confidence=0.82,
    )


def _job_input(conn: sqlite3.Connection, job_id: int) -> sqlite3.Row:
    row = conn.execute(
        """
        SELECT jobs.id, jobs.title, jobs.company,
               eligibility.decision AS eligibility,
               eligibility.verification_status,
               eligibility.complete_description,
               current.value_json AS description_json,
               snapshots.id AS source_snapshot_id,
               snapshots.source_url,
               snapshots.content_checksum
        FROM jobs
        LEFT JOIN job_eligibility_decisions AS eligibility ON eligibility.job_id = jobs.id
        LEFT JOIN job_current_fields AS current
          ON current.job_id = jobs.id AND current.field_name = 'job_description'
        LEFT JOIN job_source_snapshots AS snapshots ON snapshots.id = current.source_snapshot_id
        WHERE jobs.id = ?
        """,
        (job_id,),
    ).fetchone()
    if not row:
        raise KeyError(f"unknown job id: {job_id}")
    return row


def map_job(
    conn: sqlite3.Connection,
    job_id: int,
    *,
    candidate_evidence_path: str | Path = DEFAULT_CANDIDATE_EVIDENCE_PATH,
    human_override: bool = False,
    override_reason: str | None = None,
    override_reviewer: str | None = None,
) -> dict[str, Any]:
    row = _job_input(conn, job_id)
    allowed = row["eligibility"] in ELIGIBLE_DECISIONS and bool(row["complete_description"])
    if not allowed and not human_override:
        raise MappingBlockedError(
            f"job {job_id} blocked: eligibility={row['eligibility']!r}, complete_description={bool(row['complete_description'])}; explicit human override required"
        )
    if human_override and not (override_reason or "").strip():
        raise MappingBlockedError("human override requires a non-empty reason")
    if not row["description_json"] or not row["source_snapshot_id"]:
        raise MappingBlockedError(f"job {job_id} has no selected job description to map")
    description = json.loads(row["description_json"])
    if not isinstance(description, str) or not description.strip():
        raise MappingBlockedError(f"job {job_id} has an empty selected job description")

    artifact = load_candidate_evidence(candidate_evidence_path)
    evidence_checksum = candidate_evidence_checksum(artifact)
    facts = _evidence_facts(artifact)
    requirements = extract_requirements(description, row["content_checksum"])
    if not requirements:
        raise MappingBlockedError(f"job {job_id} produced no explicit requirements")

    existing = conn.execute(
        """
        SELECT id FROM job_evidence_mapping_runs
        WHERE job_id = ? AND job_content_checksum = ? AND candidate_evidence_checksum = ?
          AND extraction_version = ? AND mapping_version = ?
        """,
        (job_id, row["content_checksum"], evidence_checksum, EXTRACTION_VERSION, MAPPING_VERSION),
    ).fetchone()
    if existing:
        count = conn.execute("SELECT COUNT(*) FROM job_requirements WHERE run_id = ?", (existing["id"],)).fetchone()[0]
        return {"job_id": job_id, "run_id": existing["id"], "created": False, "requirements": count}

    decisions = tuple(map_requirement(requirement, facts) for requirement in requirements)
    created_at = _now()
    with conn:
        cursor = conn.execute(
            """
            INSERT INTO job_evidence_mapping_runs(
              job_id, source_snapshot_id, job_content_checksum, candidate_evidence_checksum,
              extraction_version, mapping_version, extraction_provider, extraction_model,
              mapping_provider, mapping_model, created_at, human_override, override_reason,
              override_reviewer, human_review_status
            ) VALUES (?, ?, ?, ?, ?, ?, 'deterministic', NULL, 'deterministic', NULL, ?, ?, ?, ?, ?)
            """,
            (
                job_id, row["source_snapshot_id"], row["content_checksum"], evidence_checksum,
                EXTRACTION_VERSION, MAPPING_VERSION, created_at, int(human_override),
                override_reason, override_reviewer,
                "pending" if human_override or any(item.human_review for item in decisions) else "not_required",
            ),
        )
        run_id = cursor.lastrowid
        for requirement, decision in zip(requirements, decisions):
            requirement_row = conn.execute(
                """
                INSERT INTO job_requirements(
                  run_id, requirement_id, sequence_number, source_text, source_span_start,
                  source_span_end, normalized_requirement, category, importance,
                  requirement_status, explicitness, source_url, source_snapshot_id,
                  job_content_checksum, extraction_confidence
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'explicit', ?, ?, ?, ?)
                """,
                (
                    run_id, requirement.requirement_id, requirement.sequence_number,
                    requirement.source_text, requirement.source_span_start,
                    requirement.source_span_end, requirement.normalized_requirement,
                    requirement.category, requirement.importance, requirement.requirement_status,
                    row["source_url"], row["source_snapshot_id"], row["content_checksum"],
                    requirement.extraction_confidence,
                ),
            )
            conn.execute(
                """
                INSERT INTO job_requirement_mappings(
                  requirement_row_id, assessment, supporting_claim_ids_json,
                  supporting_claims_json, verified_leaf_claim_ids_json,
                  verified_leaf_claims_json, unsupported_gap_claim_ids_json,
                  unsupported_gap_claims_json, explanation, mapping_confidence,
                  human_review_flag
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    requirement_row.lastrowid, decision.assessment,
                    _ids_json(decision.supporting), _facts_json(decision.supporting),
                    _ids_json(decision.verified_leaves), _facts_json(decision.verified_leaves),
                    _ids_json(decision.gaps), _facts_json(decision.gaps),
                    decision.explanation, decision.confidence, int(decision.human_review),
                ),
            )
    return {"job_id": job_id, "run_id": run_id, "created": True, "requirements": len(requirements)}


def _ids_json(facts: Iterable[EvidenceFact]) -> str:
    return json.dumps([fact.fact_id for fact in facts], ensure_ascii=False)


def _facts_json(facts: Iterable[EvidenceFact]) -> str:
    return json.dumps(
        [{"claim_id": fact.fact_id, "text": fact.text, "status": fact.status, "category": fact.category} for fact in facts],
        ensure_ascii=False,
    )


def map_jobs(
    conn: sqlite3.Connection,
    *,
    job_ids: list[int] | None = None,
    candidate_evidence_path: str | Path = DEFAULT_CANDIDATE_EVIDENCE_PATH,
    human_override: bool = False,
    override_reason: str | None = None,
    override_reviewer: str | None = None,
) -> dict[str, Any]:
    if job_ids is None:
        job_ids = [
            row[0] for row in conn.execute(
                """
                SELECT jobs.id FROM jobs
                JOIN job_eligibility_decisions eligibility ON eligibility.job_id = jobs.id
                WHERE eligibility.decision IN ('eligible', 'conditionally_eligible')
                  AND eligibility.complete_description = 1
                ORDER BY jobs.id
                """
            )
        ]
    results = []
    blocked = []
    for job_id in job_ids:
        try:
            results.append(map_job(
                conn, job_id, candidate_evidence_path=candidate_evidence_path,
                human_override=human_override, override_reason=override_reason,
                override_reviewer=override_reviewer,
            ))
        except MappingBlockedError as exc:
            blocked.append({"job_id": job_id, "reason": str(exc)})
    run_ids = [item["run_id"] for item in results]
    category_counts: Counter[str] = Counter()
    assessment_counts: Counter[str] = Counter()
    review_flags = 0
    if run_ids:
        placeholders = ",".join("?" for _ in run_ids)
        category_counts.update(dict(conn.execute(
            f"SELECT category, COUNT(*) FROM job_requirements WHERE run_id IN ({placeholders}) GROUP BY category", run_ids
        ).fetchall()))
        rows = conn.execute(
            f"""SELECT mappings.assessment, COUNT(*), SUM(mappings.human_review_flag)
                 FROM job_requirement_mappings mappings
                 JOIN job_requirements requirements ON requirements.id = mappings.requirement_row_id
                 WHERE requirements.run_id IN ({placeholders}) GROUP BY mappings.assessment""", run_ids
        ).fetchall()
        for assessment, count, flags in rows:
            assessment_counts[assessment] = count
            review_flags += flags or 0
    return {
        "eligible_jobs": job_ids if not human_override else [],
        "attempted": len(job_ids),
        "mapped": len(results),
        "created_runs": sum(item["created"] for item in results),
        "reused_runs": sum(not item["created"] for item in results),
        "blocked": blocked,
        "requirements_by_category": dict(sorted(category_counts.items())),
        "assessments": dict(sorted(assessment_counts.items())),
        "human_review_flags": review_flags,
        "results": results,
    }
