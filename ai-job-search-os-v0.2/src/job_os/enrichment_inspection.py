from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from typing import Any
from urllib.parse import urlparse

from .enrichment import SOURCE_PRECEDENCE


def _domain(url: str) -> str:
    return (urlparse(url).hostname or "").lower().removeprefix("www.")


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return bool(
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table,),
        ).fetchone()
    )


def show_enrichment(conn: sqlite3.Connection, job_id: int) -> dict[str, Any]:
    job = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if not job:
        raise KeyError(f"unknown job id: {job_id}")
    enrichment = (
        conn.execute(
            "SELECT * FROM job_enrichments WHERE job_id = ?", (job_id,)
        ).fetchone()
        if _table_exists(conn, "job_enrichments")
        else None
    )
    eligibility = (
        conn.execute(
            "SELECT * FROM job_eligibility_decisions WHERE job_id = ?", (job_id,)
        ).fetchone()
        if _table_exists(conn, "job_eligibility_decisions")
        else None
    )
    current_rows = conn.execute(
        """
        SELECT current.field_name, current.value_json, current.selected_at,
               snapshots.id AS snapshot_id, snapshots.source_url,
               snapshots.source_type, snapshots.retrieved_at,
               snapshots.content_checksum
        FROM job_current_fields AS current
        JOIN job_source_snapshots AS snapshots
          ON snapshots.id = current.source_snapshot_id
        WHERE current.job_id = ?
        ORDER BY current.field_name
        """,
        (job_id,),
    ).fetchall() if _table_exists(conn, "job_current_fields") else []
    value_rows = conn.execute(
        """
        SELECT values_table.field_name, values_table.value_json,
               snapshots.id AS snapshot_id, snapshots.source_url,
               snapshots.source_type, snapshots.retrieved_at,
               snapshots.content_checksum
        FROM job_field_values AS values_table
        JOIN job_source_snapshots AS snapshots
          ON snapshots.id = values_table.source_snapshot_id
        WHERE values_table.job_id = ?
        ORDER BY values_table.field_name, snapshots.retrieved_at DESC
        """,
        (job_id,),
    ).fetchall() if _table_exists(conn, "job_field_values") else []
    current_by_field = {row["field_name"]: row for row in current_rows}
    alternatives: dict[str, list[dict[str, Any]]] = defaultdict(list)
    seen: dict[str, set[tuple[str, str]]] = defaultdict(set)
    for row in value_rows:
        selected = current_by_field.get(row["field_name"])
        if selected and (
            row["value_json"] == selected["value_json"]
            and row["snapshot_id"] == selected["snapshot_id"]
        ):
            continue
        key = (row["value_json"], row["source_url"])
        if key in seen[row["field_name"]]:
            continue
        seen[row["field_name"]].add(key)
        alternatives[row["field_name"]].append(
            {
                "value": json.loads(row["value_json"]),
                "source": _source_metadata(row),
            }
        )
    source_rows = conn.execute(
        """
        SELECT snapshots.id AS snapshot_id, snapshots.source_url,
               snapshots.source_type, snapshots.retrieved_at,
               snapshots.http_status, snapshots.retrieval_status,
               snapshots.verification_status, snapshots.content_checksum,
               snapshots.failure_reason, state.last_checked_at,
               state.last_successfully_checked_at
        FROM job_source_snapshots AS snapshots
        LEFT JOIN job_source_state AS state
          ON state.job_id = snapshots.job_id
         AND state.source_url = snapshots.source_url
        WHERE snapshots.job_id = ?
        ORDER BY snapshots.retrieved_at DESC, snapshots.id DESC
        """,
        (job_id,),
    ).fetchall() if _table_exists(conn, "job_source_snapshots") else []
    candidate_rows = conn.execute(
        """
        SELECT candidate_url, domain, source_type, discovery_method, provider,
               search_query, provider_rank, discovered_at, evaluated_at,
               decision, decision_reason, confidence_reasons_json,
               retrieval_status, http_status, content_checksum
        FROM job_source_candidates
        WHERE job_id = ?
        ORDER BY decision, provider_rank, candidate_url
        """,
        (job_id,),
    ).fetchall() if _table_exists(conn, "job_source_candidates") else []
    conflict_fields = (
        json.loads(enrichment["conflict_fields_json"]) if enrichment else []
    )
    failures = [
        {
            "source_url": row["source_url"],
            "domain": _domain(row["source_url"]),
            "retrieval_status": row["retrieval_status"],
            "http_status": row["http_status"],
            "failure_reason": row["failure_reason"],
        }
        for row in source_rows
        if row["failure_reason"]
        or row["retrieval_status"] not in {"success", "closed"}
    ]
    failures.extend(
        {
            "source_url": row["candidate_url"],
            "domain": row["domain"],
            "retrieval_status": row["retrieval_status"],
            "http_status": row["http_status"],
            "failure_reason": row["decision_reason"],
        }
        for row in candidate_rows
        if row["decision"] == "rejected"
    )
    return {
        "job": {
            "id": job["id"],
            "stable_source_id": job["source_id"],
            "canonical_job_url": job["canonical_job_url"],
            "ingested_title": job["title"],
            "ingested_company": job["company"],
            "ingested_location": job["location"],
        },
        "verification": (
            {
                "status": enrichment["verification_status"],
                "official_posting_url": enrichment["official_posting_url"],
                "company_careers_url": enrichment["company_careers_url"],
                "complete_description": bool(enrichment["complete_description"]),
                "conflict_fields": conflict_fields,
                "last_attempted_at": enrichment["last_attempted_at"],
                "last_successfully_checked_at": enrichment[
                    "last_successfully_checked_at"
                ],
            }
            if enrichment
            else None
        ),
        "eligibility": dict(eligibility) if eligibility else None,
        "source_precedence": dict(
            sorted(SOURCE_PRECEDENCE.items(), key=lambda item: item[1], reverse=True)
        ),
        "selected_fields": {
            row["field_name"]: {
                "value": json.loads(row["value_json"]),
                "source": _source_metadata(row),
            }
            for row in current_rows
        },
        "alternative_values": dict(alternatives),
        "sources": [
            {
                "snapshot_id": row["snapshot_id"],
                "source_url": row["source_url"],
                "domain": _domain(row["source_url"]),
                "source_type": row["source_type"],
                "precedence": SOURCE_PRECEDENCE.get(row["source_type"], 0),
                "retrieved_at": row["retrieved_at"],
                "http_status": row["http_status"],
                "retrieval_status": row["retrieval_status"],
                "verification_status": row["verification_status"],
                "content_checksum": row["content_checksum"],
                "last_checked_at": row["last_checked_at"],
                "last_successfully_checked_at": row[
                    "last_successfully_checked_at"
                ],
                "failure_reason": row["failure_reason"],
            }
            for row in source_rows
        ],
        "source_candidates": [
            {
                **{
                    key: row[key]
                    for key in (
                        "candidate_url",
                        "domain",
                        "source_type",
                        "discovery_method",
                        "provider",
                        "search_query",
                        "provider_rank",
                        "discovered_at",
                        "evaluated_at",
                        "decision",
                        "decision_reason",
                        "retrieval_status",
                        "http_status",
                        "content_checksum",
                    )
                },
                "confidence_reasons": json.loads(row["confidence_reasons_json"]),
            }
            for row in candidate_rows
        ],
        "failures": failures,
    }


def _source_metadata(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "snapshot_id": row["snapshot_id"],
        "source_url": row["source_url"],
        "domain": _domain(row["source_url"]),
        "source_type": row["source_type"],
        "precedence": SOURCE_PRECEDENCE.get(row["source_type"], 0),
        "retrieved_at": row["retrieved_at"],
        "content_checksum": row["content_checksum"],
    }
