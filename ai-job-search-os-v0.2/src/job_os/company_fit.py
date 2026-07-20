from __future__ import annotations

import csv
import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

import yaml


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SCORING_CONFIG_PATH = PROJECT_ROOT / "config" / "scoring.yaml"
DEFAULT_WATCHLIST_PATH = PROJECT_ROOT / "config" / "company_watchlist.csv"
COMPANY_SCORING_VERSION = "company-fit-v1"
CORE_DIMENSIONS = (
    "candidate_background_fit",
    "operating_complexity",
    "product_operations_intersection",
    "ai_transformation_relevance",
    "geographic_fit",
    "international_environment",
    "future_role_likelihood",
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _checksum(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")


def _normalized_alias(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()


def _parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


@dataclass(frozen=True)
class CompanyFitConfig:
    version: str
    dimensions: Mapping[str, float]
    desired_values: Mapping[str, float]
    recommendations: Mapping[str, float]
    confidence_weights: Mapping[str, float]
    source_authority: Mapping[str, float]
    freshness_days: Mapping[str, int]
    minimum_core_dimensions: int
    weak_core_fit_threshold: float
    multiple_b_count: int
    multiple_b_period_days: int
    checksum: str


def load_company_fit_config(
    path: str | Path = DEFAULT_SCORING_CONFIG_PATH,
) -> CompanyFitConfig:
    payload = yaml.safe_load(Path(path).read_text())
    data = payload.get("company_fit_v1") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        raise ValueError("scoring config must contain company_fit_v1")
    dimensions = {k: float(v) for k, v in (data.get("dimensions") or {}).items()}
    required = set(CORE_DIMENSIONS) | {"desired_company_status"}
    if set(dimensions) != required or abs(sum(dimensions.values()) - 100) > 1e-9:
        raise ValueError("company dimensions must contain the eight v1 dimensions and sum to 100")
    confidence_weights = {
        k: float(v) for k, v in (data.get("confidence_weights") or {}).items()
    }
    if abs(sum(confidence_weights.values()) - 1) > 1e-9:
        raise ValueError("company confidence weights must sum to 1")
    dynamic = data.get("dynamic_watchlist") or {}
    canonical = json.loads(json.dumps(data, sort_keys=True))
    return CompanyFitConfig(
        version=str(data.get("version") or COMPANY_SCORING_VERSION),
        dimensions=dimensions,
        desired_values={k: float(v) for k, v in (data.get("desired_company_values") or {}).items()},
        recommendations={k: float(v) for k, v in (data.get("recommendations") or {}).items()},
        confidence_weights=confidence_weights,
        source_authority={k: float(v) for k, v in (data.get("source_authority") or {}).items()},
        freshness_days={k: int(v) for k, v in (data.get("freshness_days") or {}).items()},
        minimum_core_dimensions=int(data.get("minimum_core_dimensions_for_score", 2)),
        weak_core_fit_threshold=float(data.get("weak_core_fit_threshold", 0.35)),
        multiple_b_count=int(dynamic.get("multiple_b_count", 2)),
        multiple_b_period_days=int(dynamic.get("multiple_b_period_days", 90)),
        checksum=_checksum(canonical),
    )


def _identity_checksum(
    canonical_name: str,
    legal_name: str | None,
    parent_company_id: str | None,
    confidence: float,
    evidence: list[Any],
) -> str:
    return _checksum(
        {
            "canonical_name": canonical_name,
            "legal_name": legal_name,
            "parent_company_id": parent_company_id,
            "identity_confidence": confidence,
            "identity_evidence": evidence,
        }
    )


def upsert_company(
    conn: sqlite3.Connection,
    *,
    company_id: str,
    canonical_name: str,
    legal_name: str | None = None,
    parent_company_id: str | None = None,
    identity_confidence: float = 0.5,
    identity_evidence: list[Any] | None = None,
    timestamp: str | None = None,
) -> None:
    evidence = identity_evidence or []
    timestamp = timestamp or _now()
    checksum = _identity_checksum(
        canonical_name, legal_name, parent_company_id, identity_confidence, evidence
    )
    conn.execute(
        """
        INSERT INTO companies(
          id, canonical_name, legal_name, parent_company_id, identity_confidence,
          identity_evidence_json, identity_checksum, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
          canonical_name=excluded.canonical_name,
          legal_name=COALESCE(excluded.legal_name, companies.legal_name),
          parent_company_id=COALESCE(excluded.parent_company_id, companies.parent_company_id),
          identity_confidence=MAX(excluded.identity_confidence, companies.identity_confidence),
          identity_evidence_json=CASE
            WHEN excluded.identity_confidence >= companies.identity_confidence
            THEN excluded.identity_evidence_json ELSE companies.identity_evidence_json END,
          identity_checksum=CASE
            WHEN excluded.identity_confidence >= companies.identity_confidence
            THEN excluded.identity_checksum ELSE companies.identity_checksum END,
          updated_at=excluded.updated_at
        """,
        (
            company_id,
            canonical_name,
            legal_name,
            parent_company_id,
            identity_confidence,
            json.dumps(evidence, ensure_ascii=False, sort_keys=True),
            checksum,
            timestamp,
            timestamp,
        ),
    )
    aliases = [(canonical_name, "alias")]
    if legal_name:
        aliases.append((legal_name, "legal_name"))
    for alias, kind in aliases:
        conn.execute(
            """INSERT OR IGNORE INTO company_aliases(
                 company_id, alias, alias_kind, normalized_alias, evidence_url
               ) VALUES (?, ?, ?, ?, NULL)""",
            (company_id, alias, kind, _normalized_alias(alias)),
        )


def _append_tier_event(
    conn: sqlite3.Connection,
    company_id: str,
    tier: str,
    reason: str,
    actor: str,
    *,
    seed_import_id: int | None = None,
    timestamp: str | None = None,
) -> bool:
    timestamp = timestamp or _now()
    event_checksum = _checksum(
        {"company_id": company_id, "tier": tier, "reason": reason, "actor": actor, "seed": seed_import_id}
    )
    result = conn.execute(
        """INSERT OR IGNORE INTO company_desired_tier_history(
             company_id, desired_tier, reason, actor, source_seed_import_id,
             event_checksum, created_at
           ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (company_id, tier, reason, actor, seed_import_id, event_checksum, timestamp),
    )
    return result.rowcount == 1


def _append_watch_event(
    conn: sqlite3.Connection,
    company_id: str,
    new_state: str,
    event_type: str,
    trigger_type: str,
    reason: str,
    *,
    related_job_ids: list[int] | None = None,
    actor: str = "system",
    timestamp: str | None = None,
) -> bool:
    current = conn.execute(
        "SELECT new_state FROM company_watch_history WHERE company_id=? ORDER BY id DESC LIMIT 1",
        (company_id,),
    ).fetchone()
    previous = current["new_state"] if current else None
    if previous == new_state and event_type != "manual":
        return False
    event_checksum = _checksum(
        {
            "company_id": company_id,
            "previous": previous,
            "new": new_state,
            "type": event_type,
            "trigger": trigger_type,
            "reason": reason,
            "jobs": sorted(related_job_ids or []),
            "actor": actor,
        }
    )
    result = conn.execute(
        """INSERT OR IGNORE INTO company_watch_history(
             company_id, previous_state, new_state, event_type, trigger_type,
             reason, related_job_ids_json, actor, event_checksum, created_at
           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            company_id,
            previous,
            new_state,
            event_type,
            trigger_type,
            reason,
            json.dumps(sorted(related_job_ids or [])),
            actor,
            event_checksum,
            timestamp or _now(),
        ),
    )
    return result.rowcount == 1


def import_seed_watchlist(
    conn: sqlite3.Connection,
    path: str | Path = DEFAULT_WATCHLIST_PATH,
    *,
    timestamp: str | None = None,
) -> dict[str, int]:
    path = Path(path)
    timestamp = timestamp or _now()
    rows_imported = companies_linked = tier_events = watch_events = 0
    with path.open(newline="", encoding="utf-8-sig") as handle:
        for row_number, row in enumerate(csv.DictReader(handle), 2):
            label = (row.get("company") or "").strip()
            market = (row.get("market") or "").strip()
            tier = (row.get("seed_tier") or "").strip()
            if not label or not market or tier not in {"tier_1", "tier_2"}:
                raise ValueError(f"invalid watchlist row {row_number}")
            insert = conn.execute(
                """INSERT OR IGNORE INTO company_seed_imports(
                     source_path, source_row, source_company, market, seed_tier, imported_at
                   ) VALUES (?, ?, ?, ?, ?, ?)""",
                (str(path), row_number, label, market, tier, timestamp),
            )
            if insert.rowcount:
                rows_imported += 1
            seed = conn.execute(
                "SELECT id FROM company_seed_imports WHERE source_path=? AND source_row=?",
                (str(path), row_number),
            ).fetchone()
            identities = (
                [("shopee", "Shopee", "seed label names an operating brand"),
                 ("sea-limited", "Sea Limited", "seed label names Shopee's parent")]
                if _normalized_alias(label) == "shopee sea"
                else [(_slug(label), label, "one canonical company from seed label")]
            )
            for company_id, canonical_name, resolution in identities:
                parent = "sea-limited" if company_id == "shopee" else None
                if parent:
                    upsert_company(
                        conn,
                        company_id="sea-limited",
                        canonical_name="Sea Limited",
                        legal_name="Sea Limited",
                        identity_confidence=0.7,
                        identity_evidence=[{"source": "seed", "label": label}],
                        timestamp=timestamp,
                    )
                upsert_company(
                    conn,
                    company_id=company_id,
                    canonical_name=canonical_name,
                    parent_company_id=parent,
                    identity_confidence=0.7,
                    identity_evidence=[{"source": "seed", "label": label}],
                    timestamp=timestamp,
                )
                conn.execute(
                    """INSERT OR IGNORE INTO company_aliases(
                         company_id, alias, alias_kind, normalized_alias, evidence_url
                       ) VALUES (?, ?, 'seed_label', ?, NULL)""",
                    (company_id, label, _normalized_alias(label)),
                )
                conn.execute(
                    "INSERT OR IGNORE INTO company_target_markets(company_id, market, source) VALUES (?, ?, 'seed_watchlist')",
                    (company_id, market),
                )
                link = conn.execute(
                    """INSERT OR IGNORE INTO company_seed_import_links(
                         seed_import_id, company_id, resolution_reason
                       ) VALUES (?, ?, ?)""",
                    (seed["id"], company_id, resolution),
                )
                companies_linked += int(bool(link.rowcount))
                tier_events += int(
                    _append_tier_event(
                        conn,
                        company_id,
                        tier,
                        f"Imported from seed row {row_number}: {label}",
                        "seed_import",
                        seed_import_id=seed["id"],
                        timestamp=timestamp,
                    )
                )
                watch_events += int(
                    _append_watch_event(
                        conn,
                        company_id,
                        "needs_research",
                        "seeded",
                        "seed_import",
                        f"Seeded as {tier}; company evidence has not yet been assessed",
                        timestamp=timestamp,
                    )
                )
    conn.commit()
    return {
        "seed_rows_imported": rows_imported,
        "seed_rows_total": conn.execute("SELECT COUNT(*) FROM company_seed_imports").fetchone()[0],
        "company_links_created": companies_linked,
        "tier_events_created": tier_events,
        "watch_events_created": watch_events,
    }


def set_desired_company_tier(
    conn: sqlite3.Connection,
    company_id: str,
    tier: str,
    *,
    reason: str,
    reviewer: str,
) -> dict[str, Any]:
    if tier not in {"tier_1", "tier_2", "dynamic", "none"}:
        raise ValueError(f"invalid desired-company tier: {tier}")
    if not conn.execute("SELECT 1 FROM companies WHERE id=?", (company_id,)).fetchone():
        raise KeyError(f"unknown company id: {company_id}")
    created = _append_tier_event(
        conn, company_id, tier, reason, reviewer
    )
    conn.commit()
    return {"company_id": company_id, "desired_tier": tier, "history_event_created": created}


def set_company_watch_state(
    conn: sqlite3.Connection,
    company_id: str,
    state: str,
    *,
    reason: str,
    reviewer: str,
) -> dict[str, Any]:
    allowed = {
        "priority_watch", "active_watch", "monitor", "do_not_watch",
        "needs_research", "identity_unresolved",
    }
    if state not in allowed:
        raise ValueError(f"invalid watch state: {state}")
    if not conn.execute("SELECT 1 FROM companies WHERE id=?", (company_id,)).fetchone():
        raise KeyError(f"unknown company id: {company_id}")
    created = _append_watch_event(
        conn, company_id, state, "manual", "manual", reason, actor=reviewer
    )
    conn.commit()
    return {"company_id": company_id, "watch_state": state, "history_event_created": created}


def import_company_research(
    conn: sqlite3.Connection,
    path: str | Path,
    *,
    timestamp: str | None = None,
) -> dict[str, int]:
    payload = json.loads(Path(path).read_text())
    if not isinstance(payload, dict) or not isinstance(payload.get("companies"), list):
        raise ValueError("captured company research must contain a companies list")
    timestamp = timestamp or _now()
    companies = payload["companies"]
    for item in companies:
        upsert_company(
            conn,
            company_id=item["company_id"],
            canonical_name=item["canonical_name"],
            legal_name=item.get("legal_name"),
            identity_confidence=float(item.get("identity_confidence", 0.5)),
            identity_evidence=list(item.get("identity_evidence") or []),
            timestamp=timestamp,
        )
    for item in companies:
        if parent := item.get("parent_company_id"):
            if not conn.execute("SELECT 1 FROM companies WHERE id=?", (parent,)).fetchone():
                raise ValueError(f"unknown parent company: {parent}")
            conn.execute(
                "UPDATE companies SET parent_company_id=?, identity_checksum=?, updated_at=? WHERE id=?",
                (
                    parent,
                    _identity_checksum(
                        item["canonical_name"], item.get("legal_name"), parent,
                        float(item.get("identity_confidence", 0.5)),
                        list(item.get("identity_evidence") or []),
                    ),
                    timestamp,
                    item["company_id"],
                ),
            )
        for alias in item.get("aliases") or []:
            value = alias["value"] if isinstance(alias, dict) else str(alias)
            kind = alias.get("kind", "alias") if isinstance(alias, dict) else "alias"
            evidence_url = alias.get("evidence_url") if isinstance(alias, dict) else None
            conn.execute(
                """INSERT OR IGNORE INTO company_aliases(
                     company_id, alias, alias_kind, normalized_alias, evidence_url
                   ) VALUES (?, ?, ?, ?, ?)""",
                (item["company_id"], value, kind, _normalized_alias(value), evidence_url),
            )
        for domain in item.get("domains") or []:
            conn.execute(
                """INSERT OR REPLACE INTO company_domains(
                     company_id, domain, domain_kind, verified, evidence_url
                   ) VALUES (?, ?, ?, ?, ?)""",
                (
                    item["company_id"], domain["domain"].lower(),
                    domain.get("kind", "corporate"), int(domain.get("verified", True)),
                    domain.get("evidence_url"),
                ),
            )
        for market in item.get("target_markets") or []:
            conn.execute(
                "INSERT OR IGNORE INTO company_target_markets(company_id, market, source) VALUES (?, ?, 'captured_research')",
                (item["company_id"], market),
            )

    facts_created = 0
    for fact in payload.get("facts") or []:
        canonical = {
            key: fact.get(key)
            for key in (
                "fact_id", "company_id", "version", "dimension", "statement",
                "source_url", "source_type", "published_date", "retrieved_at",
                "freshness_policy", "freshness_days", "confidence", "status",
                "rejection_reason", "fit_value",
            )
        }
        freshness_policy = fact.get("freshness_policy", "business_model")
        result = conn.execute(
            """INSERT OR IGNORE INTO company_facts(
                 fact_id, company_id, version, dimension, statement, source_url,
                 source_type, published_date, retrieved_at, freshness_policy,
                 freshness_days, confidence, status, rejection_reason, fit_value,
                 fact_checksum
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                fact["fact_id"], fact["company_id"], int(fact.get("version", 1)),
                fact["dimension"], fact["statement"], fact["source_url"],
                fact["source_type"], fact.get("published_date"),
                fact.get("retrieved_at") or timestamp, freshness_policy,
                int(fact.get("freshness_days") or 365), float(fact.get("confidence", 0.8)),
                fact.get("status", "accepted"), fact.get("rejection_reason"),
                fact.get("fit_value"), _checksum(canonical),
            ),
        )
        facts_created += int(bool(result.rowcount))

    resolutions_created = 0
    for resolution in payload.get("job_relationships") or []:
        evidence = list(resolution.get("identity_evidence") or [])
        canonical = {
            "job_id": int(resolution["job_id"]),
            "named_company_id": resolution.get("named_company_id"),
            "underlying_company_id": resolution.get("underlying_company_id"),
            "relationship": resolution["relationship"],
            "underlying_company_unknown": bool(resolution.get("underlying_company_unknown")),
            "identity_confidence": float(resolution.get("identity_confidence", 0.5)),
            "identity_evidence": evidence,
        }
        result = conn.execute(
            """INSERT INTO job_company_resolutions(
                 job_id, named_company_id, underlying_company_id, relationship,
                 underlying_company_unknown, identity_confidence,
                 identity_evidence_json, resolution_checksum, resolved_at
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(job_id) DO UPDATE SET
                 named_company_id=excluded.named_company_id,
                 underlying_company_id=excluded.underlying_company_id,
                 relationship=excluded.relationship,
                 underlying_company_unknown=excluded.underlying_company_unknown,
                 identity_confidence=excluded.identity_confidence,
                 identity_evidence_json=excluded.identity_evidence_json,
                 resolution_checksum=excluded.resolution_checksum,
                 resolved_at=excluded.resolved_at
               WHERE job_company_resolutions.resolution_checksum != excluded.resolution_checksum""",
            (
                canonical["job_id"], canonical["named_company_id"],
                canonical["underlying_company_id"], canonical["relationship"],
                int(canonical["underlying_company_unknown"]), canonical["identity_confidence"],
                json.dumps(evidence, sort_keys=True), _checksum(canonical), timestamp,
            ),
        )
        resolutions_created += int(bool(result.rowcount))
    conn.commit()
    return {
        "companies_processed": len(companies),
        "facts_created": facts_created,
        "job_resolutions_changed": resolutions_created,
    }


def _current_tier(conn: sqlite3.Connection, company_id: str) -> tuple[str, str]:
    row = conn.execute(
        "SELECT desired_tier, event_checksum FROM company_desired_tier_history WHERE company_id=? ORDER BY id DESC LIMIT 1",
        (company_id,),
    ).fetchone()
    return (row["desired_tier"], row["event_checksum"]) if row else ("none", _checksum({"company_id": company_id, "tier": "none"}))


def _current_facts(conn: sqlite3.Connection, company_id: str) -> list[sqlite3.Row]:
    return conn.execute(
        """
        WITH latest AS (
          SELECT fact_id, MAX(version) version FROM company_facts
          WHERE company_id=? GROUP BY fact_id
        )
        SELECT facts.* FROM company_facts facts JOIN latest
          ON latest.fact_id=facts.fact_id AND latest.version=facts.version
        WHERE facts.company_id=? ORDER BY facts.dimension, facts.fact_id
        """,
        (company_id, company_id),
    ).fetchall()


def calculate_company_fit(
    company: Mapping[str, Any],
    facts: list[Mapping[str, Any]],
    desired_tier: str,
    config: CompanyFitConfig,
    *,
    as_of: datetime | None = None,
    underlying_employer_known: bool = True,
) -> dict[str, Any]:
    as_of = as_of or datetime.now(timezone.utc)
    accepted: list[Mapping[str, Any]] = []
    stale: list[str] = []
    rejected: list[dict[str, Any]] = []
    for fact in facts:
        if fact["status"] == "rejected":
            rejected.append({"fact_id": fact["fact_id"], "reason": fact["rejection_reason"]})
            continue
        expires = _parse_time(fact["retrieved_at"]) + timedelta(days=int(fact["freshness_days"]))
        if expires < as_of:
            stale.append(str(fact["fact_id"]))
            continue
        if fact["dimension"] in CORE_DIMENSIONS and fact["fit_value"] is not None:
            accepted.append(fact)

    by_dimension: dict[str, list[Mapping[str, Any]]] = {d: [] for d in CORE_DIMENSIONS}
    for fact in accepted:
        by_dimension[fact["dimension"]].append(fact)
    missing = [dimension for dimension, values in by_dimension.items() if not values]
    conflicts: list[dict[str, Any]] = []
    breakdown: dict[str, Any] = {}
    evidence_manifest: dict[str, list[str]] = {}
    covered_weight = weighted_value = 0.0
    authority_values: list[float] = []
    fact_freshness_values: list[float] = []
    for dimension, values in by_dimension.items():
        if not values:
            breakdown[dimension] = {"status": "missing", "weight": config.dimensions[dimension], "value": None, "evidence_fact_ids": []}
            continue
        fit_values = [float(item["fit_value"]) for item in values]
        if max(fit_values) - min(fit_values) > 0.35:
            conflicts.append({"dimension": dimension, "fact_ids": [item["fact_id"] for item in values], "values": fit_values})
        evidence_weights = [
            max(0.01, float(item["confidence"]) * config.source_authority.get(item["source_type"], 0.5))
            for item in values
        ]
        value = sum(v * w for v, w in zip(fit_values, evidence_weights)) / sum(evidence_weights)
        weight = config.dimensions[dimension]
        covered_weight += weight
        weighted_value += value * weight
        authority_values.extend(
            config.source_authority.get(item["source_type"], 0.5) * float(item["confidence"])
            for item in values
        )
        fact_freshness_values.extend(
            max(
                0.0,
                min(
                    1.0,
                    ((_parse_time(item["retrieved_at"]) + timedelta(days=int(item["freshness_days"]))) - as_of).total_seconds()
                    / (int(item["freshness_days"]) * 86400),
                ),
            )
            for item in values
        )
        evidence_manifest[dimension] = [str(item["fact_id"]) for item in values]
        breakdown[dimension] = {
            "status": "conflicting" if any(c["dimension"] == dimension for c in conflicts) else "supported",
            "weight": weight,
            "value": round(value, 4),
            "evidence_fact_ids": evidence_manifest[dimension],
        }

    desired_value = config.desired_values.get(desired_tier, 0.0)
    desired_points = desired_value * config.dimensions["desired_company_status"]
    breakdown["desired_company_status"] = {
        "status": "configured",
        "weight": config.dimensions["desired_company_status"],
        "value": desired_value,
        "points": desired_points,
        "desired_tier": desired_tier,
        "evidence_fact_ids": [],
    }
    score: float | None = None
    covered_dimensions = len(CORE_DIMENSIONS) - len(missing)
    if covered_dimensions >= config.minimum_core_dimensions and covered_weight:
        score = round((weighted_value / covered_weight) * 90 + desired_points, 2)

    coverage = covered_weight / sum(config.dimensions[d] for d in CORE_DIMENSIONS)
    confidence_components = {
        "identity_certainty": float(company["identity_confidence"]),
        "source_authority": sum(authority_values) / len(authority_values) if authority_values else 0.0,
        "dimension_coverage": coverage,
        "fact_freshness": sum(fact_freshness_values) / len(fact_freshness_values) if fact_freshness_values else 0.0,
        "conflict_quality": max(0.0, 1.0 - len(conflicts) / max(1, covered_dimensions)),
        "underlying_employer_known": 1.0 if underlying_employer_known else 0.0,
    }
    confidence = round(
        100 * sum(confidence_components[name] * weight for name, weight in config.confidence_weights.items()),
        2,
    )
    adequate = config.recommendations["adequate_confidence_minimum"]
    if not underlying_employer_known:
        recommendation = "identity_unresolved"
    elif score is None or confidence < adequate or conflicts:
        recommendation = "needs_research"
    elif score >= config.recommendations["priority_watch_minimum"]:
        recommendation = "priority_watch"
    elif score >= config.recommendations["active_watch_minimum"]:
        recommendation = "active_watch"
    elif score >= config.recommendations["monitor_minimum"]:
        recommendation = "monitor"
    else:
        recommendation = "do_not_watch"
    weak_core = [
        dimension
        for dimension in ("candidate_background_fit", "operating_complexity")
        if breakdown[dimension]["value"] is not None
        and breakdown[dimension]["value"] <= config.weak_core_fit_threshold
    ]
    if weak_core and recommendation in {"priority_watch", "active_watch"}:
        recommendation = "monitor" if score and score >= 50 else "do_not_watch"
    return {
        "company_fit_score": score,
        "company_confidence_score": confidence,
        "watch_recommendation": recommendation,
        "dimension_breakdown": breakdown,
        "evidence_manifest": evidence_manifest,
        "missing_research": missing,
        "stale_facts": stale,
        "conflict_facts": conflicts,
        "rejected_facts": rejected,
        "confidence_components": confidence_components,
        "weak_core_dimensions": weak_core,
    }


def score_company(
    conn: sqlite3.Connection,
    company_id: str,
    *,
    config: CompanyFitConfig | None = None,
    as_of: datetime | None = None,
) -> dict[str, Any]:
    config = config or load_company_fit_config()
    company = conn.execute("SELECT * FROM companies WHERE id=?", (company_id,)).fetchone()
    if not company:
        raise KeyError(f"unknown company id: {company_id}")
    facts = _current_facts(conn, company_id)
    desired_tier, tier_checksum = _current_tier(conn, company_id)
    result = calculate_company_fit(dict(company), [dict(row) for row in facts], desired_tier, config, as_of=as_of)
    facts_checksum = _checksum(
        [{"id": row["fact_id"], "version": row["version"], "checksum": row["fact_checksum"]} for row in facts]
    )
    scored_at = (as_of or datetime.now(timezone.utc)).isoformat()
    insert = conn.execute(
        """INSERT OR IGNORE INTO company_fit_scores(
             company_id, scoring_version, scoring_config_checksum, identity_checksum,
             facts_checksum, desired_tier_checksum, company_fit_score,
             company_confidence_score, watch_recommendation, dimension_breakdown_json,
             evidence_manifest_json, missing_research_json, stale_facts_json,
             conflict_facts_json, scored_at
           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            company_id, config.version, config.checksum, company["identity_checksum"],
            facts_checksum, tier_checksum, result["company_fit_score"],
            result["company_confidence_score"], result["watch_recommendation"],
            json.dumps(result["dimension_breakdown"], sort_keys=True),
            json.dumps(result["evidence_manifest"], sort_keys=True),
            json.dumps(result["missing_research"]), json.dumps(result["stale_facts"]),
            json.dumps(result["conflict_facts"], sort_keys=True), scored_at,
        ),
    )
    score_row = conn.execute(
        """SELECT id FROM company_fit_scores WHERE company_id=? AND scoring_version=?
           AND scoring_config_checksum=? AND identity_checksum=? AND facts_checksum=?
           AND desired_tier_checksum=?""",
        (company_id, config.version, config.checksum, company["identity_checksum"], facts_checksum, tier_checksum),
    ).fetchone()
    result.update({"company_id": company_id, "score_id": score_row["id"], "created": bool(insert.rowcount), "desired_tier": desired_tier})
    return result


def _dynamic_watch_update(
    conn: sqlite3.Connection,
    company_id: str,
    score: Mapping[str, Any],
    config: CompanyFitConfig,
) -> bool:
    relations = conn.execute(
        "SELECT job_id FROM job_company_resolutions WHERE underlying_company_id=? AND relationship='direct_employer'",
        (company_id,),
    ).fetchall()
    job_ids = [row["job_id"] for row in relations]
    if not job_ids:
        return False
    placeholders = ",".join("?" for _ in job_ids)
    cutoff = (datetime.now(timezone.utc) - timedelta(days=config.multiple_b_period_days)).isoformat()
    opportunity_rows = conn.execute(
        f"""WITH latest AS (
               SELECT job_id, MAX(id) id FROM opportunity_fit_scores
               WHERE job_id IN ({placeholders}) GROUP BY job_id
             )
             SELECT scores.job_id, scores.provisional_classification, scores.scored_at
             FROM opportunity_fit_scores scores JOIN latest ON latest.id=scores.id""",
        job_ids,
    ).fetchall()
    a_jobs = [row["job_id"] for row in opportunity_rows if row["provisional_classification"] == "A"]
    b_jobs = [row["job_id"] for row in opportunity_rows if row["provisional_classification"] == "B" and row["scored_at"] >= cutoff]
    tier, _ = _current_tier(conn, company_id)
    trigger = reason = None
    related: list[int] = []
    if a_jobs:
        trigger, related = "a_opportunity", a_jobs
        reason = "Dynamically qualified by an A opportunity"
    elif len(b_jobs) >= config.multiple_b_count:
        trigger, related = "multiple_b_opportunities", b_jobs
        reason = f"Dynamically qualified by {len(b_jobs)} B opportunities within {config.multiple_b_period_days} days"
    elif score["company_fit_score"] is not None and score["company_fit_score"] >= config.recommendations["active_watch_minimum"]:
        trigger, related = "company_fit_threshold", job_ids
        reason = "Dynamically qualified at the active-watch Company Fit threshold"
    if not trigger:
        return False
    if tier == "none":
        _append_tier_event(conn, company_id, "dynamic", reason, "dynamic_watchlist")
    return _append_watch_event(
        conn, company_id, score["watch_recommendation"], "dynamic_added",
        trigger, reason, related_job_ids=related,
    )


def score_companies(
    conn: sqlite3.Connection,
    *,
    company_ids: list[str] | None = None,
    scoring_config_path: str | Path = DEFAULT_SCORING_CONFIG_PATH,
    as_of: datetime | None = None,
) -> dict[str, Any]:
    config = load_company_fit_config(scoring_config_path)
    if company_ids is None:
        company_ids = [row["id"] for row in conn.execute("SELECT id FROM companies ORDER BY id")]
    counts: dict[str, int] = {}
    created = reused = watch_events = 0
    results = []
    for company_id in company_ids:
        result = score_company(conn, company_id, config=config, as_of=as_of)
        created += int(result["created"])
        reused += int(not result["created"])
        counts[result["watch_recommendation"]] = counts.get(result["watch_recommendation"], 0) + 1
        if result["watch_recommendation"] != "needs_research":
            current = conn.execute(
                "SELECT new_state FROM company_watch_history WHERE company_id=? ORDER BY id DESC LIMIT 1",
                (company_id,),
            ).fetchone()
            if current:
                event_type = "promoted" if result["watch_recommendation"] in {"priority_watch", "active_watch"} else "demoted"
                watch_events += int(_append_watch_event(
                    conn, company_id, result["watch_recommendation"], event_type,
                    "score_recommendation", "Company Fit recommendation changed after evidence-based scoring",
                ))
        watch_events += int(_dynamic_watch_update(conn, company_id, result, config))
        results.append(result)
    conn.commit()
    return {"companies_scored": len(results), "score_records_created": created, "score_records_reused": reused, "watch_events_created": watch_events, "recommendations": counts, "results": results}


def combined_decision_view(conn: sqlite3.Connection, job_id: int) -> dict[str, Any]:
    job = conn.execute("SELECT id, title, company, location FROM jobs WHERE id=?", (job_id,)).fetchone()
    if not job:
        raise KeyError(f"unknown job id: {job_id}")
    opportunity = conn.execute(
        "SELECT * FROM opportunity_fit_scores WHERE job_id=? ORDER BY id DESC LIMIT 1", (job_id,)
    ).fetchone()
    resolution = conn.execute(
        """SELECT resolution.*, named.canonical_name named_company_name,
                  underlying.canonical_name underlying_company_name
           FROM job_company_resolutions resolution
           LEFT JOIN companies named ON named.id=resolution.named_company_id
           LEFT JOIN companies underlying ON underlying.id=resolution.underlying_company_id
           WHERE resolution.job_id=?""",
        (job_id,),
    ).fetchone()
    company_score = None
    if resolution and resolution["underlying_company_id"] and not resolution["underlying_company_unknown"]:
        company_score = conn.execute(
            "SELECT * FROM company_fit_scores WHERE company_id=? ORDER BY id DESC LIMIT 1",
            (resolution["underlying_company_id"],),
        ).fetchone()
    classification = opportunity["provisional_classification"] if opportunity else None
    if classification == "A":
        lead = "Apply to this role"
    elif classification == "B":
        lead = "Investigate this role"
    elif classification == "C":
        lead = "Ignore this role"
    else:
        lead = "Opportunity Fit is unavailable"
    if not resolution or resolution["underlying_company_unknown"]:
        suffix = "the underlying employer is unresolved, so Company Fit is not assigned"
    elif company_score and company_score["watch_recommendation"] in {"priority_watch", "active_watch", "monitor"}:
        suffix = f"continue watching {resolution['underlying_company_name']} ({company_score['watch_recommendation']})"
    elif company_score and company_score["watch_recommendation"] == "needs_research":
        suffix = f"research {resolution['underlying_company_name']} before a watch decision"
    else:
        suffix = f"do not watch {resolution['underlying_company_name']}" if resolution else "company identity is unavailable"
    return {
        "job": dict(job),
        "opportunity_fit": None if not opportunity else {
            "score": opportunity["opportunity_fit_score"],
            "confidence": opportunity["evidence_confidence_score"],
            "classification": classification,
        },
        "company_identity": None if not resolution else {
            "named_company_id": resolution["named_company_id"],
            "named_company": resolution["named_company_name"],
            "underlying_company_id": resolution["underlying_company_id"],
            "underlying_company": resolution["underlying_company_name"],
            "relationship": resolution["relationship"],
            "underlying_company_unknown": bool(resolution["underlying_company_unknown"]),
        },
        "company_fit": None if not company_score else {
            "score": company_score["company_fit_score"],
            "confidence": company_score["company_confidence_score"],
            "watch_recommendation": company_score["watch_recommendation"],
        },
        "decision": f"{lead}; {suffix}.",
        "combined_score": None,
    }
