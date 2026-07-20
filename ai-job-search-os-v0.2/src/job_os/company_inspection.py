from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .company_fit import (
    DEFAULT_SCORING_CONFIG_PATH,
    _checksum,
    _current_facts,
    _current_tier,
    _parse_time,
    combined_decision_view,
    load_company_fit_config,
)


def _json(value: str) -> Any:
    return json.loads(value)


def show_company_fit(
    conn: sqlite3.Connection,
    company_id: str,
    *,
    scoring_config_path: str | Path = DEFAULT_SCORING_CONFIG_PATH,
) -> dict[str, Any]:
    company = conn.execute("SELECT * FROM companies WHERE id=?", (company_id,)).fetchone()
    if not company:
        raise KeyError(f"unknown company id: {company_id}")
    aliases = [dict(row) for row in conn.execute(
        "SELECT alias, alias_kind, evidence_url FROM company_aliases WHERE company_id=? ORDER BY alias",
        (company_id,),
    )]
    domains = [dict(row) for row in conn.execute(
        "SELECT domain, domain_kind, verified, evidence_url FROM company_domains WHERE company_id=? ORDER BY domain",
        (company_id,),
    )]
    markets = [row["market"] for row in conn.execute(
        "SELECT market FROM company_target_markets WHERE company_id=? ORDER BY market", (company_id,)
    )]
    facts = [dict(row) for row in _current_facts(conn, company_id)]
    for fact in facts:
        fact["stale"] = (
            _parse_time(fact["retrieved_at"]) + timedelta(days=fact["freshness_days"])
            < datetime.now(timezone.utc)
        )
        fact["expires_at"] = (
            _parse_time(fact["retrieved_at"]) + timedelta(days=fact["freshness_days"])
        ).isoformat()
    related = [dict(row) for row in conn.execute(
        """SELECT jobs.id job_id, jobs.title, jobs.company alert_company, jobs.location,
                  resolution.relationship, resolution.underlying_company_unknown
           FROM job_company_resolutions resolution JOIN jobs ON jobs.id=resolution.job_id
           WHERE resolution.named_company_id=? OR resolution.underlying_company_id=?
           ORDER BY jobs.id""",
        (company_id, company_id),
    )]
    history = [dict(row) for row in conn.execute(
        """SELECT id, previous_state, new_state, event_type, trigger_type, reason,
                  related_job_ids_json, actor, created_at
           FROM company_watch_history WHERE company_id=? ORDER BY id""",
        (company_id,),
    )]
    for event in history:
        event["related_job_ids"] = _json(event.pop("related_job_ids_json"))
    tier_history = [dict(row) for row in conn.execute(
        """SELECT desired_tier, reason, actor, created_at
           FROM company_desired_tier_history WHERE company_id=? ORDER BY id""",
        (company_id,),
    )]
    score = conn.execute(
        "SELECT * FROM company_fit_scores WHERE company_id=? ORDER BY id DESC LIMIT 1",
        (company_id,),
    ).fetchone()
    score_payload = None
    freshness = {"stale": None, "reasons": ["not_scored"]}
    if score:
        config = load_company_fit_config(scoring_config_path)
        current_facts = _current_facts(conn, company_id)
        current_facts_checksum = _checksum([
            {"id": row["fact_id"], "version": row["version"], "checksum": row["fact_checksum"]}
            for row in current_facts
        ])
        _, tier_checksum = _current_tier(conn, company_id)
        reasons = []
        if score["scoring_config_checksum"] != config.checksum:
            reasons.append("scoring_config_changed")
        if score["identity_checksum"] != company["identity_checksum"]:
            reasons.append("company_identity_changed")
        if score["facts_checksum"] != current_facts_checksum:
            reasons.append("company_facts_changed")
        if score["desired_tier_checksum"] != tier_checksum:
            reasons.append("desired_company_tier_changed")
        stored_stale = set(_json(score["stale_facts_json"]))
        now_stale = {fact["fact_id"] for fact in facts if fact["stale"]}
        if now_stale != stored_stale:
            reasons.append("fact_freshness_changed")
        score_payload = {
            "score_id": score["id"],
            "company_fit_score": score["company_fit_score"],
            "company_confidence_score": score["company_confidence_score"],
            "watch_recommendation": score["watch_recommendation"],
            "dimensions": _json(score["dimension_breakdown_json"]),
            "evidence_manifest": _json(score["evidence_manifest_json"]),
            "missing_research": _json(score["missing_research_json"]),
            "stale_facts": _json(score["stale_facts_json"]),
            "conflicts": _json(score["conflict_facts_json"]),
            "scored_at": score["scored_at"],
            "provenance": {
                "scoring_version": score["scoring_version"],
                "scoring_config_checksum": score["scoring_config_checksum"],
                "identity_checksum": score["identity_checksum"],
                "facts_checksum": score["facts_checksum"],
                "desired_tier_checksum": score["desired_tier_checksum"],
            },
        }
        freshness = {"stale": bool(reasons), "reasons": reasons}
    parent = None
    if company["parent_company_id"]:
        row = conn.execute(
            "SELECT id, canonical_name FROM companies WHERE id=?", (company["parent_company_id"],)
        ).fetchone()
        parent = dict(row) if row else None
    return {
        "company": {
            "id": company["id"],
            "canonical_name": company["canonical_name"],
            "legal_name": company["legal_name"],
            "parent_company": parent,
            "identity_confidence": company["identity_confidence"],
            "identity_evidence": _json(company["identity_evidence_json"]),
            "aliases": aliases,
            "domains": domains,
            "target_markets": markets,
        },
        "score": score_payload,
        "freshness": freshness,
        "facts": facts,
        "related_opportunities": related,
        "desired_tier_history": tier_history,
        "watch_history": history,
    }


def show_watchlist(conn: sqlite3.Connection) -> dict[str, Any]:
    companies = conn.execute(
        """
        WITH latest_score AS (
          SELECT company_id, MAX(id) id FROM company_fit_scores GROUP BY company_id
        ), latest_watch AS (
          SELECT company_id, MAX(id) id FROM company_watch_history GROUP BY company_id
        ), latest_tier AS (
          SELECT company_id, MAX(id) id FROM company_desired_tier_history GROUP BY company_id
        )
        SELECT companies.id company_id, companies.canonical_name,
               tiers.desired_tier, scores.company_fit_score,
               scores.company_confidence_score, scores.watch_recommendation,
               watch.new_state current_watch_state, watch.reason watch_reason,
               watch.created_at watch_updated_at,
               (SELECT COUNT(*) FROM company_seed_import_links links
                WHERE links.company_id=companies.id) seed_links
        FROM companies
        LEFT JOIN latest_score ls ON ls.company_id=companies.id
        LEFT JOIN company_fit_scores scores ON scores.id=ls.id
        LEFT JOIN latest_watch lw ON lw.company_id=companies.id
        LEFT JOIN company_watch_history watch ON watch.id=lw.id
        LEFT JOIN latest_tier lt ON lt.company_id=companies.id
        LEFT JOIN company_desired_tier_history tiers ON tiers.id=lt.id
        ORDER BY CASE COALESCE(watch.new_state, scores.watch_recommendation, 'needs_research')
          WHEN 'priority_watch' THEN 1 WHEN 'active_watch' THEN 2
          WHEN 'monitor' THEN 3 WHEN 'needs_research' THEN 4
          WHEN 'identity_unresolved' THEN 5 ELSE 6 END,
          scores.company_fit_score DESC, companies.canonical_name
        """
    ).fetchall()
    rows = []
    counts: dict[str, int] = {}
    for row in companies:
        item = dict(row)
        item["watch_state"] = item.pop("current_watch_state") or item["watch_recommendation"] or "needs_research"
        counts[item["watch_state"]] = counts.get(item["watch_state"], 0) + 1
        rows.append(item)
    unresolved = [dict(row) for row in conn.execute(
        """SELECT jobs.id job_id, jobs.title, jobs.company named_company,
                  jobs.location, resolution.relationship
           FROM job_company_resolutions resolution JOIN jobs ON jobs.id=resolution.job_id
           WHERE resolution.underlying_company_unknown=1 ORDER BY jobs.id"""
    )]
    return {
        "counts_by_watch_state": counts,
        "seed_rows_imported": conn.execute("SELECT COUNT(*) FROM company_seed_imports").fetchone()[0],
        "canonical_companies": len(rows),
        "companies": rows,
        "underlying_employer_unresolved": unresolved,
    }


def show_combined_decision(conn: sqlite3.Connection, job_id: int) -> dict[str, Any]:
    return combined_decision_view(conn, job_id)
