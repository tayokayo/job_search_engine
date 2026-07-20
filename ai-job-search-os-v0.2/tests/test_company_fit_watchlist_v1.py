from __future__ import annotations

import csv
import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from job_os.cli import main
from job_os.company_fit import (
    calculate_company_fit,
    combined_decision_view,
    import_company_research,
    import_seed_watchlist,
    load_company_fit_config,
    score_companies,
    set_company_watch_state,
    set_desired_company_tier,
)
from job_os.company_inspection import show_company_fit, show_watchlist
from job_os.store import connect


FIXTURE = Path(__file__).parent / "fixtures" / "company_research.json"
AS_OF = datetime(2026, 7, 20, 12, tzinfo=timezone.utc)


def _seed_csv(path: Path, company: str = "Alpha Market", tier: str = "tier_1") -> None:
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["company", "market", "seed_tier"])
        writer.writeheader()
        writer.writerow({"company": company, "market": "Singapore", "seed_tier": tier})


def _prepared(tmp_path: Path) -> tuple[sqlite3.Connection, Path]:
    database = tmp_path / "test.sqlite"
    conn = connect(database)
    seed = tmp_path / "watchlist.csv"
    _seed_csv(seed)
    import_seed_watchlist(conn, seed, timestamp="2026-07-20T00:00:00+00:00")
    import_company_research(conn, FIXTURE, timestamp="2026-07-20T00:00:00+00:00")
    return conn, database


def _job(conn: sqlite3.Connection, company: str, suffix: str) -> int:
    row = conn.execute(
        """
        INSERT INTO jobs(source, source_id, gmail_message_id, source_url,
                         canonical_job_url, title, company, location,
                         alert_timestamp, dedupe_key)
        VALUES ('test', ?, ?, 'https://jobs.example/role',
                'https://jobs.example/role', 'Product Lead', ?, 'Singapore',
                '2026-07-20T00:00:00+00:00', ?)
        """,
        (suffix, f"message-{suffix}", company, f"company-test-{suffix}"),
    )
    conn.commit()
    return row.lastrowid


def test_canonical_identity_alias_parent_and_seed_split(tmp_path):
    conn, _ = _prepared(tmp_path)
    try:
        company = conn.execute("SELECT * FROM companies WHERE id='alpha-market'").fetchone()
        assert company["parent_company_id"] == "alpha-holdings"
        aliases = {row["alias"] for row in conn.execute(
            "SELECT alias FROM company_aliases WHERE company_id='alpha-market'"
        )}
        assert {"Alpha Market", "Alpha Marketplace"} <= aliases

        combined = tmp_path / "combined.csv"
        _seed_csv(combined, "Shopee / Sea", "tier_2")
        result = import_seed_watchlist(conn, combined)
        assert result["seed_rows_imported"] == 1
        shopee = conn.execute("SELECT parent_company_id FROM companies WHERE id='shopee'").fetchone()
        assert shopee["parent_company_id"] == "sea-limited"
        assert conn.execute("SELECT COUNT(*) FROM company_seed_import_links WHERE seed_import_id=(SELECT MAX(id) FROM company_seed_imports)").fetchone()[0] == 2
    finally:
        conn.close()


def test_recruiter_and_hidden_employer_are_not_scored_as_employer(tmp_path):
    conn, _ = _prepared(tmp_path)
    try:
        job_id = _job(conn, "Example Recruiting", "hidden")
        conn.execute(
            """INSERT INTO job_company_resolutions(
                 job_id, named_company_id, underlying_company_id, relationship,
                 underlying_company_unknown, identity_confidence,
                 identity_evidence_json, resolution_checksum, resolved_at
               ) VALUES (?, 'example-recruiting', NULL, 'staffing_intermediary',
                         1, .99, '[]', 'hidden-checksum', '2026-07-20T00:00:00+00:00')""",
            (job_id,),
        )
        conn.commit()
        view = combined_decision_view(conn, job_id)
        assert view["company_fit"] is None
        assert view["company_identity"]["underlying_company_unknown"] is True
        assert view["company_identity"]["relationship"] == "staffing_intermediary"
    finally:
        conn.close()


def test_seed_tier_points_and_missing_facts_reduce_confidence_not_fit(tmp_path):
    conn, _ = _prepared(tmp_path)
    try:
        config = load_company_fit_config()
        company = dict(conn.execute("SELECT * FROM companies WHERE id='alpha-market'").fetchone())
        facts = [dict(row) for row in conn.execute(
            "SELECT * FROM company_facts WHERE company_id='alpha-market' AND status='accepted'"
        )]
        tier_one = calculate_company_fit(company, facts, "tier_1", config, as_of=AS_OF)
        dynamic = calculate_company_fit(company, facts, "dynamic", config, as_of=AS_OF)
        assert tier_one["company_fit_score"] - dynamic["company_fit_score"] == 10

        two_supported = [
            {**facts[0], "fit_value": 1.0},
            {**facts[1], "fit_value": 1.0},
        ]
        sparse = calculate_company_fit(company, two_supported, "dynamic", config, as_of=AS_OF)
        full_perfect = calculate_company_fit(
            company, [{**fact, "fit_value": 1.0} for fact in facts], "dynamic", config, as_of=AS_OF
        )
        assert sparse["company_fit_score"] == full_perfect["company_fit_score"] == 90
        assert sparse["company_confidence_score"] < full_perfect["company_confidence_score"]
        assert sparse["missing_research"]
    finally:
        conn.close()


def test_stale_and_conflicting_facts_are_visible_and_block_recommendation(tmp_path):
    conn, _ = _prepared(tmp_path)
    try:
        config = load_company_fit_config()
        company = dict(conn.execute("SELECT * FROM companies WHERE id='alpha-market'").fetchone())
        facts = [dict(row) for row in conn.execute(
            "SELECT * FROM company_facts WHERE company_id='alpha-market' AND status='accepted'"
        )]
        stale = calculate_company_fit(
            company, facts, "tier_1", config,
            as_of=datetime(2031, 1, 1, tzinfo=timezone.utc),
        )
        assert len(stale["stale_facts"]) == len(facts)
        assert stale["company_fit_score"] is None
        assert stale["watch_recommendation"] == "needs_research"

        background = next(
            fact for fact in facts if fact["dimension"] == "candidate_background_fit"
        )
        conflict_fact = {
            **background,
            "fact_id": "alpha-background-conflict",
            "fit_value": 0.0,
        }
        conflicting = calculate_company_fit(company, facts + [conflict_fact], "tier_1", config, as_of=AS_OF)
        assert conflicting["conflict_facts"][0]["dimension"] == "candidate_background_fit"
        assert conflicting["watch_recommendation"] == "needs_research"
    finally:
        conn.close()


def test_watch_promotion_history_and_repeated_runs_are_idempotent(tmp_path):
    conn, _ = _prepared(tmp_path)
    try:
        job_id = _job(conn, "Alpha Market", "direct")
        conn.execute(
            """INSERT INTO job_company_resolutions(
                 job_id, named_company_id, underlying_company_id, relationship,
                 underlying_company_unknown, identity_confidence,
                 identity_evidence_json, resolution_checksum, resolved_at
               ) VALUES (?, 'alpha-market', 'alpha-market', 'direct_employer',
                         0, .99, '[]', 'direct-checksum', '2026-07-20T00:00:00+00:00')""",
            (job_id,),
        )
        before_opportunity = conn.execute("SELECT COUNT(*) FROM opportunity_fit_scores").fetchone()[0]
        first = score_companies(conn, company_ids=["alpha-market"], as_of=AS_OF)
        first_score_count = conn.execute("SELECT COUNT(*) FROM company_fit_scores").fetchone()[0]
        first_history_count = conn.execute("SELECT COUNT(*) FROM company_watch_history").fetchone()[0]
        second = score_companies(conn, company_ids=["alpha-market"], as_of=AS_OF)
        assert first["score_records_created"] == 1
        assert second["score_records_reused"] == 1
        assert conn.execute("SELECT COUNT(*) FROM company_fit_scores").fetchone()[0] == first_score_count
        assert conn.execute("SELECT COUNT(*) FROM company_watch_history").fetchone()[0] == first_history_count
        assert conn.execute("SELECT COUNT(*) FROM opportunity_fit_scores").fetchone()[0] == before_opportunity
        states = [row["new_state"] for row in conn.execute(
            "SELECT new_state FROM company_watch_history WHERE company_id='alpha-market' ORDER BY id"
        )]
        assert states == ["needs_research", "priority_watch"]
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute("UPDATE company_watch_history SET reason='changed' WHERE company_id='alpha-market'")
    finally:
        conn.close()


def test_score_staleness_after_identity_fact_config_or_tier_change(tmp_path):
    conn, _ = _prepared(tmp_path)
    try:
        score_companies(conn, company_ids=["alpha-market"], as_of=AS_OF)
        fresh = show_company_fit(conn, "alpha-market")
        assert fresh["freshness"] == {"stale": False, "reasons": []}
        conn.execute(
            """INSERT INTO company_desired_tier_history(
                 company_id, desired_tier, reason, actor, event_checksum, created_at
               ) VALUES ('alpha-market', 'tier_2', 'manual change', 'reviewer',
                         'tier-change', '2026-07-20T12:00:00+00:00')"""
        )
        conn.commit()
        stale = show_company_fit(conn, "alpha-market")
        assert "desired_company_tier_changed" in stale["freshness"]["reasons"]
        conn.execute(
            "UPDATE companies SET identity_checksum='changed-identity' WHERE id='alpha-market'"
        )
        conn.execute(
            """INSERT INTO company_facts(
                 fact_id, company_id, version, dimension, statement, source_url,
                 source_type, retrieved_at, freshness_policy, freshness_days,
                 confidence, status, fit_value, fact_checksum
               ) VALUES ('new-fact', 'alpha-market', 1, 'operating_complexity',
                         'A new bounded fact.', 'https://alpha.example/new',
                         'official_corporate', '2026-07-20T00:00:00+00:00',
                         'business_model', 1095, .9, 'accepted', .8, 'new-checksum')"""
        )
        conn.commit()
        stale = show_company_fit(conn, "alpha-market")
        assert "company_identity_changed" in stale["freshness"]["reasons"]
        assert "company_facts_changed" in stale["freshness"]["reasons"]

        config_copy = tmp_path / "scoring.yaml"
        config_text = Path("config/scoring.yaml").read_text().replace(
            "priority_watch_minimum: 80", "priority_watch_minimum: 81"
        )
        config_copy.write_text(config_text)
        stale = show_company_fit(
            conn, "alpha-market", scoring_config_path=config_copy
        )
        assert "scoring_config_changed" in stale["freshness"]["reasons"]
    finally:
        conn.close()


def test_manual_tier_and_watch_decisions_append_without_overwriting(tmp_path):
    conn, _ = _prepared(tmp_path)
    try:
        assert set_desired_company_tier(
            conn, "alpha-market", "tier_2", reason="reviewed", reviewer="human"
        )["history_event_created"]
        assert set_company_watch_state(
            conn, "alpha-market", "monitor", reason="manual hold", reviewer="human"
        )["history_event_created"]
        assert conn.execute(
            "SELECT COUNT(*) FROM company_desired_tier_history WHERE company_id='alpha-market'"
        ).fetchone()[0] == 2
        history = conn.execute(
            "SELECT previous_state, new_state, actor FROM company_watch_history WHERE company_id='alpha-market' ORDER BY id"
        ).fetchall()
        assert [row["new_state"] for row in history] == ["needs_research", "monitor"]
        assert history[-1]["actor"] == "human"
    finally:
        conn.close()


def test_read_only_inspections_and_cli_do_not_change_database(tmp_path, capsys):
    conn, database = _prepared(tmp_path)
    score_companies(conn, company_ids=["alpha-market"], as_of=AS_OF)
    assert show_watchlist(conn)["canonical_companies"] == 3
    conn.close()
    before = hashlib.sha256(database.read_bytes()).hexdigest()
    main(["show-company-fit", "--company-id", "alpha-market", "--db", str(database)])
    company_output = json.loads(capsys.readouterr().out)
    assert company_output["company"]["parent_company"]["id"] == "alpha-holdings"
    main(["show-watchlist", "--db", str(database)])
    watch_output = json.loads(capsys.readouterr().out)
    assert watch_output["canonical_companies"] == 3
    after = hashlib.sha256(database.read_bytes()).hexdigest()
    assert after == before
