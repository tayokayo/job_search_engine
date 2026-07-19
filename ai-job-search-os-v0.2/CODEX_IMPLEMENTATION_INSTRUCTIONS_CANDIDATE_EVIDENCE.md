# Codex Implementation Instructions: Candidate Evidence Foundation

## Objective

Implement `candidate_evidence.yaml` as a self-contained evidence foundation in the AI Job Search OS.

This checkpoint is limited to:

- schema models;
- artifact loading;
- structural and semantic validation;
- flattened indexes;
- deterministic derived-claim provenance;
- a stable content checksum;
- focused tests and a validation CLI.

Do **not** connect the artifact to opportunity scoring, the CRM/database, job enrichment, resume tailoring, outreach, daily digests, prompts, or stale-evaluation persistence in this checkpoint. Those are later integrations that must consume the validated foundation rather than expand its scope now.

Do not change the working Gmail discovery, parser, URL normalization, or ingestion pipeline.

## File placement

1. The supplied download may arrive at the repository root. Move it to exactly `config/candidate_evidence.yaml` before implementing the loader; do not retain a second root-level copy.
2. Add one configurable path, such as `candidate_evidence_path`, with that path as the default.
3. Keep the source artifact human-editable and do not generate or overwrite it at runtime.
4. The version-controlled artifact must not contain a personal email address or phone number.
5. If contact details are needed later, load them from `config/candidate_private.yaml` or environment variables. Add `config/candidate_private.yaml` to `.gitignore`; never create or commit a populated private contact file during this checkpoint.
6. Add `.DS_Store` to `.gitignore` and remove the two untracked `.DS_Store` files before staging. Do not use a broad recursive deletion command.

## Supported schema

Support `schema_version: "1.1"` exactly. Reject unsupported versions with a clear error.

Implement typed models for:

- the document and evidence policy;
- identity and structured candidate facts;
- career facts, including time-bounded calculated claims;
- positioning claims;
- experience records and their scope/achievement claims;
- project claims;
- education, language, and skill records;
- evidence gaps and prohibited inferences;
- maintenance metadata.

An atomic narrative claim has:

- `claim_id`;
- `text`;
- `status`;
- optional `derivation_type`;
- `derived_from_claim_ids` when its status is `derived`.

Allowed statuses are only:

- `verified`;
- `derived`;
- `unsupported`.

## Deterministic provenance contract

Every candidate narrative claim intended for external use must have a unique
`claim_id`. Policy metadata, maintenance notes, privacy instructions, and
prohibited-inference rules are not candidate claims and do not require claim IDs.

Every `derived` claim must:

1. contain a non-empty `derived_from_claim_ids` list;
2. reference exact, existing claim IDs—not parent evidence IDs, prose descriptions, or YAML paths;
3. have no duplicate upstream IDs;
4. not cite itself;
5. participate in an acyclic provenance graph;
6. terminate transitively only in `verified` claims;
7. use a permitted `derivation_type` of `paraphrase`, `synthesis`, or `calculation`;
8. when `derivation_type` is `calculation`, include a deterministic `calculation_rule` and an `as_of_date`.

Verified and unsupported claims must not declare `derived_from_claim_ids`.

Build a reverse dependency index so a claim can report both its upstream sources and downstream derived claims.

Do not attempt general semantic scope validation in this checkpoint. Deterministically validate structure, provenance, identifiers, metrics, and explicitly prohibited transformations. Record semantic scope-expansion review as a later generation-layer responsibility.

## Indexes

Build immutable in-memory indexes for:

- `evidence_by_id`;
- `claim_by_id`;
- `upstream_claim_ids_by_claim_id`;
- `downstream_claim_ids_by_claim_id`;
- `claims_by_status`;
- `claims_by_evidence_id` where a parent evidence record exists.

Each flattened claim entry must retain:

- its exact canonical text;
- status and derivation type;
- direct upstream claim IDs;
- fully resolved verified leaf claim IDs;
- parent evidence ID and category, when applicable;
- company, project, or positioning context when applicable.

Positioning claims do not need a fabricated parent evidence ID. Their exact upstream claim IDs are their provenance.

Unsupported evidence gaps are first-class atomic claims. They must appear in `claim_by_id` and `claims_by_status`, remain citable as gaps, and must never be treated as affirmative candidate experience.

## Checksum

Compute a SHA-256 checksum from a deterministic canonical serialization of the parsed artifact:

- recursively sort mapping keys;
- preserve list order;
- serialize with UTF-8;
- exclude no fields in this version;
- do not hash raw file bytes, because formatting-only YAML changes must not alter the checksum.

The same semantic document must produce the same checksum across runs. A semantic value change must produce a different checksum.

This checkpoint reports the checksum only. Do not add database columns or stale-record behavior yet.

## Validation

Fail closed and return actionable, path-aware errors for:

1. missing or unreadable artifact;
2. invalid YAML;
3. unsupported schema version;
4. missing required sections or fields;
5. invalid claim status or derivation type;
6. duplicate evidence IDs;
7. duplicate claim IDs across all claim collections;
8. derived claim without exact upstream claim IDs;
9. reference to an unknown upstream claim ID;
10. self-reference or provenance cycle;
11. a derived graph that does not terminate in verified claims;
12. verified or unsupported claim carrying derived provenance;
13. duplicate upstream references;
14. malformed dates or date ordering;
15. an `end_date` of `present` on a non-current record where the schema makes that detectable;
16. calculated derived claim without a valid rule or `as_of_date`;
17. prohibited metric transformations in deterministic validator test cases;
18. language proficiency outside the artifact's accepted vocabulary;
19. evidence gaps that are not structured atomic claims with `status: unsupported`;
20. personal email addresses or phone numbers in the public artifact.

Keep a distinction between:

- schema errors;
- provenance errors;
- policy errors.

Do not repair invalid evidence automatically.

## Foundation API

Expose a small local API equivalent to:

```python
artifact = load_candidate_evidence(path)
report = validate_candidate_evidence(artifact)
index = build_candidate_evidence_index(artifact)
checksum = candidate_evidence_checksum(artifact)
resolved = index.resolve_verified_leaves("positioning_summary_01")
```

The exact names may follow the repository's conventions, but loading, validation, indexing, checksum calculation, and leaf resolution must remain separately testable.

Add a read-only CLI command equivalent to:

```bash
PYTHONPATH=src python -m job_os.cli validate-candidate-evidence
```

It should print only:

- validity;
- schema version;
- counts by evidence category and claim status;
- duplicate/reference/cycle error counts;
- checksum;
- redacted validation errors.

It must not print identity contact fields or full claim text by default.

## Required tests

Add fixtures and tests proving that the foundation:

- loads the supplied canonical artifact;
- finds every evidence ID and claim ID across all collections;
- reports no duplicates in the canonical artifact;
- resolves `amazon_scope_02` to `amazon_scope_01` and `amazon_achievement_05`;
- resolves `positioning_summary_01` transitively to exact verified leaf claims;
- resolves `years_experience_01` to `career_start_01` and validates its calculation rule and `as_of_date`;
- rejects a derived claim supported only by a parent evidence ID or prose `basis`;
- rejects a missing, empty, unknown, duplicated, or self-referential upstream claim ID;
- rejects direct and indirect provenance cycles;
- rejects a derived provenance chain ending in an unsupported claim;
- rejects invalid statuses and derivation types;
- rejects a calculated claim without a calculation rule or `as_of_date`;
- rejects verified claims that declare derived provenance;
- produces identical checksums for semantically identical YAML with different formatting or mapping-key order;
- changes the checksum after a semantic value change;
- fails closed on missing, malformed, or unsupported-version artifacts;
- redacts email and phone from CLI output and validation logs;
- rejects personal contact details in the public artifact;
- indexes every explicit evidence gap as an unsupported claim;
- preserves metric tokens and qualifiers in the indexed canonical text;
- detects prohibited test transformations such as `more than 20 percentage points` becoming `20%` and `contributed to 30% growth` becoming sole ownership;
- does not modify the artifact, Gmail, SQLite database, or external systems.

## Checkpoint report

At completion, report:

1. files changed;
2. schema version and validation result;
3. evidence counts by category;
4. claim counts by status;
5. provenance graph node and edge counts;
6. resolved verified leaf IDs for `amazon_scope_02`;
7. resolved verified leaf IDs for `positioning_summary_01`;
8. checksum and repeatability result;
9. test command and complete outcome;
10. one deliberately invalid provenance fixture and its rejected error;
11. confirmation that `config/candidate_evidence.yaml` is the only public artifact copy, `config/candidate_private.yaml` is ignored, and no personal contact details are tracked;
12. confirmation that `.DS_Store` is ignored and the two untracked files were removed;
13. confirmation that ingestion behavior and external systems were untouched;
14. remaining blockers or decisions.

Stop after this checkpoint. Do not commit, push, enrich, score, generate application materials, or mutate Gmail/CRM state without explicit approval.

## Deferred integration work

The following requirements are intentionally deferred until the foundation passes:

- requirement-to-evidence mapping;
- Opportunity Fit and Company Fit scoring;
- generation assertion manifests;
- resume selection and tailoring;
- hiring-manager messages;
- prompt enforcement and structured LLM output;
- database audit trails and stale-evaluation persistence;
- daily-digest integration.

The future integration must use this loader, validator, index, resolved provenance, and checksum rather than creating a second evidence interpretation path.
