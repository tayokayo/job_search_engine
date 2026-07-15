from __future__ import annotations

import argparse
from collections import Counter

from .gmail import get_message, gmail_service, list_messages
from .parser import parse_alert_message
from .store import connect, insert_job

DISCOVERY_QUERY = 'newer_than:30d (from:(linkedin.com) OR from:(linkedin.com/jobs) OR subject:("jobs" "LinkedIn")) -has:attachment'
INGEST_QUERY_TEMPLATE = 'newer_than:30d from:{sender} subject:("{subject_token}") -has:attachment'


def discover_alert_query(args):
    service = gmail_service()
    messages = [get_message(service, item["id"]) for item in list_messages(service, DISCOVERY_QUERY, args.max_results)]
    senders = Counter()
    subjects = Counter()
    samples = []
    for msg in messages:
        headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
        sender = headers.get("From", "")
        subject = headers.get("Subject", "")
        if "linkedin" in (sender + subject).lower():
            senders[sender] += 1
            subjects[subject] += 1
            samples.append({"id": msg["id"], "from": sender, "subject": subject, "date": headers.get("Date", "")})
    print("Discovery query used:", DISCOVERY_QUERY)
    print("Sample matched metadata:")
    for sample in samples[:10]:
        print(sample)
    if senders and subjects:
        sender = senders.most_common(1)[0][0]
        token = subjects.most_common(1)[0][0].split()[0].strip(':"') or "jobs"
        print("Proposed Gmail query:", INGEST_QUERY_TEMPLATE.format(sender=sender, subject_token=token))
    else:
        print("No LinkedIn alert pattern found. Do not ingest until discovery confirms the sender/subject.")


def ingest(args):
    service = gmail_service()
    conn = connect(args.db)
    total = inserted = duplicates = 0
    for item in list_messages(service, args.query, args.max_results):
        msg = get_message(service, item["id"])
        for job in parse_alert_message(msg):
            total += 1
            if args.dry_run:
                print({"message_id": job.gmail_message_id, "job_id": job.job_identifier, "title": job.title, "company": job.company, "location": job.location, "canonical_url": job.canonical_job_url})
            elif insert_job(conn, job):
                inserted += 1
            else:
                duplicates += 1
    print({"parsed": total, "inserted": inserted, "duplicates": duplicates, "dry_run": args.dry_run})


def not_yet(args):
    raise SystemExit(f"{args.command} is reserved for a later milestone and is intentionally not implemented yet.")


def main(argv=None):
    parser = argparse.ArgumentParser(prog="job-os")
    sub = parser.add_subparsers(dest="command", required=True)
    d = sub.add_parser("discover-alert-query")
    d.add_argument("--max-results", type=int, default=25)
    d.set_defaults(func=discover_alert_query)
    i = sub.add_parser("ingest")
    i.add_argument("--dry-run", action="store_true")
    i.add_argument("--query", default=DISCOVERY_QUERY)
    i.add_argument("--max-results", type=int, default=25)
    i.add_argument("--db", default="job_os.sqlite")
    i.set_defaults(func=ingest)
    for name in ["enrich", "evaluate", "check-watchlist", "generate-strategy", "digest", "list-jobs", "add-job", "update-status"]:
        p = sub.add_parser(name)
        if name == "generate-strategy":
            p.add_argument("job_id")
        p.set_defaults(func=not_yet)
    args = parser.parse_args(argv)
    return args.func(args)

if __name__ == "__main__":
    main()
