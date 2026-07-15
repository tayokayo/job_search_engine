# Codex Build Prompt — AI Job Search OS v0.2

LinkedIn job alerts are already configured and are arriving in Gmail. Build the MVP in `SPEC.md`.

## Read first
README.md, SPEC.md, config/profile.yaml, config/scoring.yaml, config/company_watchlist.csv, and all existing src/job_os files.

## Milestone 1 — Discover and ingest real alert emails
1. Implement Gmail OAuth.
2. Add `discover-alert-query` to inspect recent inbox messages and identify the actual LinkedIn job-alert sender/subject patterns in this mailbox.
3. Do not assume a hardcoded sender before mailbox evidence confirms it.
4. Show the proposed Gmail query and sample matched metadata for human confirmation.
5. Implement `ingest --dry-run` and `ingest`.
6. Store Gmail message ID for idempotency and traceability.
7. Never process attachments or execute downloaded files.

## Milestone 2 — Parse, normalize and deduplicate
Extract title, company, location, source URL, job identifier and alert timestamp.
Normalize URLs and remove tracking parameters where safe.
Deduplicate by: stable job ID/canonical URL → official company URL → normalized company+title+location.
Re-running ingestion must not create duplicates.

STOP AFTER MILESTONES 1–2. Report results and wait for human approval.

## Later milestones after approval
3. Enrich via accessible public job URL and official company careers/ATS page; no authenticated LinkedIn scraping or access-control bypass.
4. Persist jobs and companies in SQLite; seed company watchlist.
5. Score opportunities using deterministic rules plus structured LLM review. Separate verified evidence, inference and unsupported requirements.
6. Apply hard constraints: target geography; business/native Japanese required; business/native Chinese/Mandarin required. Do not reject preferred languages.
7. Apply seniority logic: startup/SME Director/Head/VP; large corporate high-scope senior IC accepted; judge scope, not title alone.
8. Compute Company Fit Score and dynamically promote companies producing A or repeated B opportunities.
9. For A roles, collect recent trustworthy company intelligence with source URL/date.
10. Generate Application Strategy Pack: resume recommendation, exact tailoring, verified evidence, gaps, company intelligence, positioning angle, confidently identified hiring manager if available, and draft message. Do not send.
11. Generate daily digest: A roles, B roles, C summary, new watchlist companies, status changes.
12. After successful digest, label processed alert emails; archive only if configured; never delete.

## Required CLI
discover-alert-query
ingest [--dry-run]
enrich
evaluate
check-watchlist
generate-strategy <job-id>
digest
list-jobs
add-job
update-status

## Tests
Email discovery helpers; parsing; URL normalization; deduplication; idempotency; language required vs preferred; seniority logic; A/B/C boundaries; invalid LLM output; company promotion logic; strategy-pack grounding.

## First checkpoint acceptance criteria
- discover the actual LinkedIn alert pattern in the mailbox
- dry-run parses current real alerts
- second ingestion creates no duplicates
- no authenticated LinkedIn scraping
