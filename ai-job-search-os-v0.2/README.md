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
