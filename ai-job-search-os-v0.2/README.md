# AI Job Search Operating System v0.2

Career intelligence system for discovering, verifying, scoring, tracking and pursuing senior opportunities.

## MVP loop
LinkedIn alerts → Gmail → public job verification → official company careers verification → CRM → Opportunity Fit Score → Company Fit Score/watchlist → daily digest → application strategy pack.

## Target geography
Japan/Tokyo; Singapore; Thailand/Bangkok; Taiwan/Taipei; New York.

## Seniority
- Startup/SME: Director, Head, VP.
- Large corporate: high-scope senior IC roles are acceptable, including Principal, Staff, Senior Lead and exceptional Senior Manager roles.
- Evaluate actual scope, not title alone.

## Human control
No authenticated LinkedIn scraping, automated applications, automated outreach, or invented candidate/company facts.

## Milestones 1-2 CLI

Install the package dependencies, place the Gmail OAuth client JSON at `credentials.json` (or set `GMAIL_CREDENTIALS_PATH`), then run:

```bash
PYTHONPATH=src python -m job_os.cli discover-alert-query
PYTHONPATH=src python -m job_os.cli ingest --dry-run --query '<query copied from discovery>'
PYTHONPATH=src python -m job_os.cli ingest --query '<query copied from discovery>'
```

The discovery command starts with a broad, attachment-excluding query, validates job-card structure from shortlisted bodies, and derives a proposed query from the actual sender address and observed stable subject patterns. Live Gmail ingestion requires the operator to copy that proposed query into `--query`; it is never silently inferred from a display name or a single subject word.

Connector exports and sanitized fixtures can be tested without local Gmail OAuth:

```bash
PYTHONPATH=src python -m job_os.cli discover-alert-query --input-json messages.json
PYTHONPATH=src python -m job_os.cli ingest --dry-run --input-json messages.json
PYTHONPATH=src python -m job_os.cli ingest --input-json messages.json --db checkpoint.sqlite
PYTHONPATH=src python -m job_os.cli ingest --raw-mime alert-1.eml --raw-mime alert-2.eml --db checkpoint.sqlite
```

`--input-json` accepts a message list or an `emails`, `messages`, or `responses` wrapper. Messages may contain connector `body`/`body_text`/`body_html` fields or `raw_mime`/`raw_mime_base64url`. `--raw-mime` accepts RFC822 `.eml` files and may be repeated. Attachment MIME parts are skipped in every mode.

The checkpoint uses Gmail read-only OAuth scope and contains no label, archive, send, delete, or attachment-read operation. Ingestion stores Gmail message IDs, canonicalizes LinkedIn job URLs to `https://linkedin.com/jobs/view/<id>`, and deduplicates by stable LinkedIn job ID before URL or normalized field fallbacks. Later milestone commands remain intentionally blocked.

## Candidate evidence foundation

The public, human-editable candidate evidence source is `config/candidate_evidence.yaml`. Validate its schema, provenance graph, policy constraints, and deterministic checksum without modifying the artifact or database:

```bash
PYTHONPATH=src python -m job_os.cli validate-candidate-evidence
```

Use `--candidate-evidence-path <path>` or the `CANDIDATE_EVIDENCE_PATH` environment variable to validate another artifact. Personal contact details are prohibited in the public artifact; a future private contact source belongs at the gitignored `config/candidate_private.yaml`.

## Public job verification and enrichment

Enrich opportunities already stored in SQLite using unauthenticated public pages:

```bash
PYTHONPATH=src python3 -m job_os.cli enrich --db job_os.sqlite --max-results 25
PYTHONPATH=src python3 -m job_os.cli enrich --db job_os.sqlite --refresh
PYTHONPATH=src python3 -m job_os.cli show-enrichment --job-id 1 --db job_os.sqlite
```

Use `--job-id <database-id>` to limit the run; repeat the option for multiple jobs. `--refresh` rechecks previously enriched records. Tests and offline audits can supply sanitized captured responses with `--responses-json <path>`.

Enrichment follows official-company, official ATS, public LinkedIn, then alert-email precedence. It stores immutable content-addressed source snapshots and separately records each field's source snapshot. Public access failures, authentication barriers, rate limits, closed postings, and incomplete pages are retained with explicit statuses; the retriever never supplies credentials, cookies, CAPTCHA handling, or anti-bot bypass behavior.

The optional official-source resolver accepts URL-only captured search results and human-reviewed domain hints. It never treats search-result snippets as evidence:

```bash
PYTHONPATH=src python3 -m job_os.cli enrich \
  --db job_os.sqlite \
  --resolver-results-json data/private/official_source_search_results.json \
  --source-hints data/private/official_source_hints.yaml
```

Start reviewed hints from `config/official_source_hints.example.yaml`; keep operational hints and captured results under the gitignored `data/private/` directory. Resolver candidates must match company, title, and location before acceptance. Accepted and rejected candidates are recorded with reasons, while unsafe schemes, private-network targets, untrusted redirects, and oversized responses are rejected.

`show-enrichment` opens the database read-only and displays selected and alternative values, provenance, source precedence, retrieval state, checksums, resolver decisions, and failure reasons. It does not display raw email content, OAuth data, or candidate-private information. The stored eligibility decision is deliberately non-numeric: verified official/ATS records are eligible, sufficiently complete LinkedIn-only records are conditionally eligible, partial/conflicting records require manual review, and unavailable/closed records are ineligible. This checkpoint does not implement opportunity scoring.

## Requirement extraction and evidence mapping

Extract explicit requirements from sufficiently complete eligible opportunities and map them to the validated candidate-evidence index:

```bash
PYTHONPATH=src python3 -m job_os.cli map-evidence --db job_os.sqlite
PYTHONPATH=src python3 -m job_os.cli show-evidence-map --job-id 1 --db job_os.sqlite
```

`map-evidence` is deterministic and retry-safe. By default it selects only `eligible` and `conditionally_eligible` jobs whose descriptions are marked complete. A blocked job can be mapped only by naming it with `--job-id`, adding `--human-override`, and recording an `--override-reason`; this exception remains inspectable in the mapping run.

Requirements retain their exact source span, source snapshot, and job-content checksum. Candidate mappings retain cited claims, resolved verified leaves, explicit unsupported gaps, and the candidate-evidence checksum. `show-evidence-map` opens SQLite read-only and reports freshness; a job-content or candidate-evidence checksum change makes the prior mapping stale. Unsupported or absent evidence never becomes affirmative evidence, and exact language levels and metric units are preserved. Mapping and calibration do not themselves calculate a score.

Calibrate deterministic mappings with provider-neutral captured AI proposals, inspect the local review queue, and record an explicit human decision without overwriting either machine result:

```bash
PYTHONPATH=src python3 -m job_os.cli calibrate-evidence-map \
  --db job_os.sqlite --ai-proposals-json data/private/evidence_mapping_ai.json
PYTHONPATH=src python3 -m job_os.cli show-evidence-review-queue --db job_os.sqlite
PYTHONPATH=src python3 -m job_os.cli review-evidence-map \
  --db job_os.sqlite --requirement-row-id 123 --assessment partial \
  --supporting-claim-id claim_id --reason "Reviewed against source evidence" \
  --reviewer local-reviewer
```

AI proposals are immutable, retain provider/model metadata, and are validated against the candidate-evidence index. Unknown IDs, unsupported gaps used as affirmative evidence, metric or attribution changes, leadership-scope upgrades, and hard-constraint overrides are rejected. Business/native Japanese or Mandarin requirements remain deterministic hard failures when validated proficiency is lower. Review-queue inspection is read-only; human decisions are appended separately.

## Opportunity Fit Scoring v1

Score only eligible, complete opportunities whose latest evidence mappings and calibrations are fresh:

```bash
PYTHONPATH=src python3 -m job_os.cli score-opportunities \
  --db job_os.sqlite --job-id 1 --job-id 6
PYTHONPATH=src python3 -m job_os.cli show-opportunity-score \
  --job-id 1 --db job_os.sqlite
PYTHONPATH=src python3 -m job_os.cli show-score-review-plan \
  --job-id 6 --db job_os.sqlite
```

The score is configuration-driven through `config/scoring.yaml`. Opportunity fit and evidence confidence are separate values: missing dimensions and ambiguous evidence reduce confidence without creating fit credit. Geography and business/native Japanese or Mandarin requirements are non-compensating hard gates. Each immutable score retains the mapping, calibration, job-content, candidate-evidence, reviewed-assessment, and scoring-configuration fingerprints plus dimension and requirement-level contributions. Repeating a score with the same inputs reuses the existing record; a checksum, calibration, or human-review change makes it stale.

`show-opportunity-score` opens SQLite read-only and explains the provisional A/B/C result from dimensions down to individual requirements. Desired-company status is excluded from Opportunity Fit and remains solely a Company Fit input.

Scoring review plans keep target geography separate from current residence, relocation willingness, work authorization, and sponsorship availability. A verified residence conflict is a non-compensating hard failure; unknown authorization, sponsorship, or relocation facts remain manual feasibility blockers. Review planning calculates conservative and plausible score bounds without granting unsupported requirements hypothetical evidence. It selects at most five questions according to classification impact, protected scope, mapping validity, and high-weight dimension impact. Generic partial or unsupported assessments do not create review work on their own, and stable low-potential C records produce no optional review queue.

## Company Fit and dynamic watchlist v1

Import seed companies, then import provider-neutral captured public research and calculate Company Fit independently from Opportunity Fit:

```bash
PYTHONPATH=src python3 -m job_os.cli import-company-watchlist --db job_os.sqlite
PYTHONPATH=src python3 -m job_os.cli import-company-research \
  --db job_os.sqlite --research-json data/private/company_research.json
PYTHONPATH=src python3 -m job_os.cli score-companies --db job_os.sqlite
PYTHONPATH=src python3 -m job_os.cli show-company-fit \
  --company-id shopee --db job_os.sqlite
PYTHONPATH=src python3 -m job_os.cli show-watchlist --db job_os.sqlite
PYTHONPATH=src python3 -m job_os.cli show-combined-decision \
  --job-id 6 --db job_os.sqlite
```

Canonical identities preserve aliases, legal names, verified domains, target markets, parent/subsidiary relationships, and per-job employer relationships. Staffing intermediaries and job boards are never substituted for a hidden employer; unresolved opportunities retain `underlying_company_unknown` and receive no employer Company Fit.

Company research consists of immutable, atomic facts with source URL, source type, retrieval time, differentiated freshness policy, confidence, acceptance decision, and bounded fit signal. Search snippets are not facts. Missing dimensions reduce Company confidence rather than being treated as negative evidence. Conflicts and stale facts remain visible and force further research. Company scores retain identity, fact, desired-tier, and scoring-configuration checksums and are idempotent for unchanged inputs.

The seed list remains complete even when most companies are unresearched. Dynamic qualification can come from an A opportunity, multiple recent B opportunities, or the active-watch Company Fit threshold. Every automatic or manual watch decision is append-only. Manual desired tier and watch decisions are explicit local operations:

```bash
PYTHONPATH=src python3 -m job_os.cli set-company-tier \
  --company-id shopee --tier tier_1 --reason "Reviewed preference" \
  --reviewer local-reviewer --db job_os.sqlite
PYTHONPATH=src python3 -m job_os.cli set-company-watch \
  --company-id shopee --state priority_watch --reason "Reviewed evidence" \
  --reviewer local-reviewer --db job_os.sqlite
```

The Company and Opportunity scores are displayed side by side and are never averaged. `show-company-fit`, `show-watchlist`, and `show-combined-decision` open SQLite read-only. Daily digests, application generation, resume tailoring, and outreach remain unimplemented.
