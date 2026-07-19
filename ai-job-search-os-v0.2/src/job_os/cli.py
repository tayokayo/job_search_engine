from __future__ import annotations

import argparse
import base64
import json
import re
import sqlite3
from collections import Counter
from email import policy
from email.parser import BytesParser
from email.utils import parseaddr
from pathlib import Path

from .candidate_evidence import (
    DEFAULT_CANDIDATE_EVIDENCE_PATH,
    CandidateEvidenceError,
    build_candidate_evidence_index,
    candidate_evidence_checksum,
    claim_counts_by_status,
    evidence_counts_by_category,
    load_candidate_evidence,
    validate_candidate_evidence,
)
from .enrichment import FixtureRetriever, PublicHttpRetriever, enrich_opportunities
from .enrichment_inspection import show_enrichment
from .gmail import get_message, gmail_service, list_messages
from .parser import parse_alert_message
from .store import connect, insert_job
from .source_resolver import (
    CapturedSearchProvider,
    EmptySearchProvider,
    OfficialSourceResolver,
    load_source_hints,
)

DISCOVERY_QUERY = (
    "newer_than:30d from:linkedin.com -has:attachment -in:spam -in:trash"
)


def _raw_mime_message(raw: bytes, message_id: str) -> dict:
    mime = BytesParser(policy=policy.default).parsebytes(raw)
    text_parts: list[str] = []
    html_parts: list[str] = []
    for part in mime.walk():
        if part.get_content_disposition() == "attachment" or part.get_filename():
            continue
        if part.get_content_type() == "text/plain":
            text_parts.append(part.get_content())
        elif part.get_content_type() == "text/html":
            html_parts.append(part.get_content())
    return {
        "id": message_id,
        "payload": {
            "headers": [
                {"name": "Date", "value": mime.get("Date", "")},
                {"name": "From", "value": mime.get("From", "")},
                {"name": "Subject", "value": mime.get("Subject", "")},
            ]
        },
        "text": "".join(text_parts),
        "html": "".join(html_parts),
    }


def _connector_message(item: dict, index: int) -> dict:
    message_id = str(item.get("id") or item.get("message_id") or f"input-{index}")
    raw_mime = item.get("raw_mime")
    if raw_mime:
        return _raw_mime_message(raw_mime.encode("utf-8"), message_id)
    raw_mime_base64url = item.get("raw_mime_base64url")
    if raw_mime_base64url:
        padding = "=" * (-len(raw_mime_base64url) % 4)
        return _raw_mime_message(
            base64.urlsafe_b64decode(raw_mime_base64url + padding), message_id
        )
    if item.get("payload") and any(
        key in item for key in ("text", "html", "body_text", "body_html", "body")
    ):
        return {"id": message_id, **item}
    return {
        "id": message_id,
        "payload": {
            "headers": [
                {"name": "Date", "value": str(item.get("email_ts") or item.get("date") or "")},
                {"name": "From", "value": str(item.get("from_") or item.get("from") or "")},
                {"name": "Subject", "value": str(item.get("subject") or "")},
            ]
        },
        "text": str(
            item.get("body_text") or item.get("text") or item.get("body") or ""
        ),
        "html": str(item.get("body_html") or item.get("html") or ""),
    }


def load_json_messages(path: str | Path) -> list[dict]:
    data = json.loads(Path(path).read_text())
    if isinstance(data, dict):
        for key in ("emails", "messages", "responses"):
            if isinstance(data.get(key), list):
                data = data[key]
                break
    if not isinstance(data, list) or not all(isinstance(item, dict) for item in data):
        raise ValueError("input JSON must be a message list or contain emails/messages/responses")
    return [_connector_message(item, index) for index, item in enumerate(data, 1)]


def load_raw_mime_messages(paths: list[str]) -> list[dict]:
    return [
        _raw_mime_message(Path(path).read_bytes(), Path(path).stem)
        for path in paths
    ]


def _input_messages(args, query: str | None = None) -> list[dict]:
    if args.input_json:
        return load_json_messages(args.input_json)
    if args.raw_mime:
        return load_raw_mime_messages(args.raw_mime)
    if query is None:
        raise SystemExit("A confirmed --query is required for live Gmail ingestion.")
    service = gmail_service()
    return [
        get_message(service, item["id"])
        for item in list_messages(service, query, args.max_results)
    ]


def _subject_pattern(subject: str) -> str | None:
    if re.search(r"\bposted on\s+\d", subject, re.I):
        return "posted on"
    if re.search(r"\S.+\sat\s.+\S", subject, re.I):
        return "title at company"
    return None


def proposed_gmail_query(senders: Counter[str], patterns: Counter[str]) -> str | None:
    if not senders:
        return None
    sender = senders.most_common(1)[0][0]
    clauses: list[str] = []
    if patterns["posted on"]:
        clauses.append('subject:"posted on"')
    if patterns["title at company"]:
        clauses.append('subject:" at "')
    subject_clause = ""
    if len(clauses) == 1:
        subject_clause = f" {clauses[0]}"
    elif clauses:
        subject_clause = " {" + " ".join(clauses) + "}"
    return (
        f"newer_than:30d from:{sender}{subject_clause} "
        "-has:attachment -in:spam -in:trash"
    )


def discover_alert_query(args):
    messages = _input_messages(args, DISCOVERY_QUERY)
    senders: Counter[str] = Counter()
    patterns: Counter[str] = Counter()
    samples = []
    for message in messages:
        headers = {
            header["name"]: header["value"]
            for header in message.get("payload", {}).get("headers", [])
        }
        jobs = parse_alert_message(message)
        sender = headers.get("From", "")
        address = parseaddr(sender)[1].lower()
        subject = headers.get("Subject", "")
        if not jobs or not address.endswith("linkedin.com"):
            continue
        senders[address] += 1
        if pattern := _subject_pattern(subject):
            patterns[pattern] += 1
        samples.append(
            {
                "id": message["id"],
                "from": address,
                "subject": subject,
                "date": headers.get("Date", ""),
                "job_count": len(jobs),
            }
        )
    print("Discovery query used:", DISCOVERY_QUERY)
    print("Sample matched metadata:")
    for sample in samples[:10]:
        print(sample)
    print("Observed subject patterns:", dict(patterns))
    proposed = proposed_gmail_query(senders, patterns)
    if proposed:
        print("Proposed Gmail query:", proposed)
        print("Confirmation required: copy this query into ingest --query for live Gmail reads.")
    else:
        print("No structurally valid LinkedIn alert pattern found. Do not ingest.")


def ingest(args):
    messages = _input_messages(args, args.query)
    conn = None if args.dry_run else connect(args.db)
    total = inserted = duplicates = 0
    rejected: Counter[str] = Counter()
    try:
        for message in messages:
            for job in parse_alert_message(message, rejected):
                total += 1
                if args.dry_run:
                    print(
                        {
                            "message_id": job.gmail_message_id,
                            "job_id": job.job_identifier,
                            "title": job.title,
                            "company": job.company,
                            "location": job.location,
                            "canonical_url": job.canonical_job_url,
                        }
                    )
                elif insert_job(conn, job):
                    inserted += 1
                else:
                    duplicates += 1
    finally:
        if conn is not None:
            conn.close()
    print(
        {
            "parsed": total,
            "inserted": inserted,
            "duplicates": duplicates,
            "dry_run": args.dry_run,
            "rejected_links": dict(sorted(rejected.items())),
        }
    )


def not_yet(args):
    raise SystemExit(
        f"{args.command} is reserved for a later milestone and is intentionally not implemented yet."
    )


def validate_candidate_evidence_command(args):
    try:
        artifact = load_candidate_evidence(args.candidate_evidence_path)
        report = validate_candidate_evidence(artifact)
        index = build_candidate_evidence_index(artifact) if report.valid else None
        issues = report.issues
        result = {
            "valid": report.valid,
            "schema_version": artifact.schema_version,
            "evidence_counts": evidence_counts_by_category(artifact),
            "claim_counts": claim_counts_by_status(artifact),
            "provenance": {
                "nodes": len(index.claim_by_id) if index else 0,
                "edges": sum(
                    len(values)
                    for values in index.upstream_claim_ids_by_claim_id.values()
                ) if index else 0,
            },
            "error_counts": {
                "duplicate": sum(
                    issue.code in {"duplicate_evidence_id", "duplicate_claim_id", "duplicate_upstream"}
                    for issue in issues
                ),
                "reference": sum(
                    issue.code in {"unknown_upstream", "non_verified_leaf", "self_reference"}
                    for issue in issues
                ),
                "cycle": sum(issue.code == "provenance_cycle" for issue in issues),
            },
            "checksum": candidate_evidence_checksum(artifact),
            "errors": [
                {
                    "category": issue.category,
                    "code": issue.code,
                    "path": issue.path,
                    "message": issue.message,
                }
                for issue in issues
            ],
        }
    except CandidateEvidenceError as exc:
        issue = exc.issue
        result = {
            "valid": False,
            "schema_version": None,
            "evidence_counts": {},
            "claim_counts": {},
            "provenance": {"nodes": 0, "edges": 0},
            "error_counts": {
                "duplicate": 0,
                "reference": 0,
                "cycle": 0,
            },
            "checksum": None,
            "errors": [
                {
                    "category": issue.category,
                    "code": issue.code,
                    "path": issue.path,
                    "message": issue.message,
                }
            ],
        }
    print(json.dumps(result, sort_keys=True))


def enrich_command(args):
    conn = connect(args.db)
    retriever = (
        FixtureRetriever.from_json(args.responses_json)
        if args.responses_json
        else PublicHttpRetriever(timeout_seconds=args.timeout)
    )
    search_provider = (
        CapturedSearchProvider.from_json(args.resolver_results_json)
        if args.resolver_results_json
        else EmptySearchProvider()
    )
    resolver = OfficialSourceResolver(
        retriever,
        search_provider=search_provider,
        hints=load_source_hints(args.source_hints),
    )
    try:
        result = enrich_opportunities(
            conn,
            retriever,
            job_ids=args.job_id,
            max_results=args.max_results,
            refresh=args.refresh,
            resolver=resolver,
        )
    finally:
        conn.close()
        close = getattr(retriever, "close", None)
        if close:
            close()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))


def show_enrichment_command(args):
    database = Path(args.db).resolve()
    if not database.exists():
        raise SystemExit(f"database does not exist: {database}")
    conn = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        result = show_enrichment(conn, args.job_id)
    except KeyError as exc:
        raise SystemExit(str(exc)) from exc
    finally:
        conn.close()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))


def _add_input_options(parser: argparse.ArgumentParser) -> None:
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--input-json", help="connector-exported Gmail message JSON")
    source.add_argument(
        "--raw-mime",
        action="append",
        help="RFC822 .eml input; repeat for multiple messages",
    )


def main(argv=None):
    parser = argparse.ArgumentParser(prog="job-os")
    sub = parser.add_subparsers(dest="command", required=True)
    discovery = sub.add_parser("discover-alert-query")
    discovery.add_argument("--max-results", type=int, default=25)
    _add_input_options(discovery)
    discovery.set_defaults(func=discover_alert_query)

    ingestion = sub.add_parser("ingest")
    ingestion.add_argument("--dry-run", action="store_true")
    ingestion.add_argument(
        "--query",
        help="confirmed Gmail query copied from discover-alert-query",
    )
    ingestion.add_argument("--max-results", type=int, default=25)
    ingestion.add_argument("--db", default="job_os.sqlite")
    _add_input_options(ingestion)
    ingestion.set_defaults(func=ingest)

    evidence = sub.add_parser("validate-candidate-evidence")
    evidence.add_argument(
        "--candidate-evidence-path",
        default=str(DEFAULT_CANDIDATE_EVIDENCE_PATH),
    )
    evidence.set_defaults(func=validate_candidate_evidence_command)

    enrichment = sub.add_parser("enrich")
    enrichment.add_argument("--db", default="job_os.sqlite")
    enrichment.add_argument("--job-id", action="append", type=int)
    enrichment.add_argument("--max-results", type=int, default=25)
    enrichment.add_argument("--refresh", action="store_true")
    enrichment.add_argument(
        "--responses-json",
        help="sanitized public-response fixture for deterministic offline verification",
    )
    enrichment.add_argument(
        "--resolver-results-json",
        help="URL-only captured public-search results; snippets are never ingested",
    )
    enrichment.add_argument(
        "--source-hints",
        help="human-reviewed company-domain, ATS-domain, and source-URL hints",
    )
    enrichment.add_argument("--timeout", type=float, default=15.0)
    enrichment.set_defaults(func=enrich_command)

    inspection = sub.add_parser("show-enrichment")
    inspection.add_argument("--job-id", type=int, required=True)
    inspection.add_argument("--db", default="job_os.sqlite")
    inspection.set_defaults(func=show_enrichment_command)

    for name in [
        "evaluate",
        "check-watchlist",
        "generate-strategy",
        "digest",
        "list-jobs",
        "add-job",
        "update-status",
    ]:
        command = sub.add_parser(name)
        if name == "generate-strategy":
            command.add_argument("job_id")
        command.set_defaults(func=not_yet)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    main()
