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

Install the package dependencies, place the Gmail OAuth client JSON at `credentials.json` (or set `GMAIL_CREDENTIALS`), then run:

```bash
PYTHONPATH=src python -m job_os.cli discover-alert-query
PYTHONPATH=src python -m job_os.cli ingest --dry-run --query '<confirmed Gmail query>'
PYTHONPATH=src python -m job_os.cli ingest --query '<confirmed Gmail query>'
```

The discovery command intentionally starts with a broad, attachment-excluding query and prints the proposed query plus sample Gmail metadata for confirmation before ingestion. Ingestion stores Gmail message IDs and deduplicates by LinkedIn job ID/canonical URL before falling back to normalized company/title/location. Later milestone commands are present but intentionally blocked until the first checkpoint is approved.
