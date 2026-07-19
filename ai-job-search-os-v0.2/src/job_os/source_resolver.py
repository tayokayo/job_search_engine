from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Protocol
from urllib.parse import urlparse

import yaml

from .enrichment import (
    ExtractedPosting,
    RetrievalResult,
    Retriever,
    _checksum,
    _host,
    _host_matches,
    _iso,
    _organization_matches,
    _persist_snapshot,
    _stable_json,
    classify_source_url,
    extract_posting,
    is_official_ats_url,
    utc_now,
    validate_public_url,
    UnsafePublicUrl,
)


@dataclass(frozen=True)
class SourceSearchCandidate:
    url: str
    provider: str
    query: str | None
    rank: int | None
    discovery_method: str = "public_search"


class SourceSearchProvider(Protocol):
    name: str

    def search(
        self, job: sqlite3.Row, queries: tuple[str, ...]
    ) -> Iterable[SourceSearchCandidate]: ...


class EmptySearchProvider:
    name = "none"

    def search(
        self, job: sqlite3.Row, queries: tuple[str, ...]
    ) -> Iterable[SourceSearchCandidate]:
        return ()


class CapturedSearchProvider:
    """Provider-neutral replay of URL-only search results; snippets are discarded."""

    name = "captured_search"

    def __init__(self, results_by_source_id: Mapping[str, tuple[SourceSearchCandidate, ...]]):
        self.results_by_source_id = results_by_source_id

    @classmethod
    def from_json(cls, path: str | Path) -> "CapturedSearchProvider":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        rows = data.get("results", []) if isinstance(data, dict) else []
        grouped: dict[str, list[SourceSearchCandidate]] = {}
        for row in rows:
            source_id = str(row["source_id"])
            values = grouped.setdefault(source_id, [])
            for candidate in row.get("candidates", []):
                values.append(
                    SourceSearchCandidate(
                        url=str(candidate["url"]),
                        provider=str(candidate.get("provider") or cls.name),
                        query=str(candidate.get("query") or row.get("query") or "") or None,
                        rank=candidate.get("rank"),
                    )
                )
        return cls({key: tuple(value) for key, value in grouped.items()})

    def search(
        self, job: sqlite3.Row, queries: tuple[str, ...]
    ) -> Iterable[SourceSearchCandidate]:
        return self.results_by_source_id.get(str(job["source_id"]), ())


@dataclass(frozen=True)
class CompanySourceHint:
    reviewed: bool
    official_domains: tuple[str, ...]
    ats_domains: tuple[str, ...]
    candidate_urls: tuple[str, ...]


def load_source_hints(path: str | Path | None) -> dict[str, CompanySourceHint]:
    if not path:
        return {}
    hint_path = Path(path)
    if not hint_path.exists():
        return {}
    data = yaml.safe_load(hint_path.read_text(encoding="utf-8")) or {}
    companies = data.get("companies", {}) if isinstance(data, dict) else {}
    hints: dict[str, CompanySourceHint] = {}
    for company, value in companies.items():
        if not isinstance(value, dict):
            continue
        hints[_identity(company)] = CompanySourceHint(
            reviewed=bool(value.get("reviewed", False)),
            official_domains=tuple(
                _clean_domain(item) for item in value.get("official_domains", [])
            ),
            ats_domains=tuple(
                _clean_domain(item) for item in value.get("ats_domains", [])
            ),
            candidate_urls=tuple(str(item) for item in value.get("candidate_urls", [])),
        )
    return hints


def _clean_domain(value: Any) -> str:
    raw = str(value).strip().lower()
    return (urlparse(raw).hostname or raw).removeprefix("www.")


def _identity(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value).lower())


def _reviewed_organization_acronym_matches(expected: str, actual: str) -> bool:
    legal_suffixes = {
        "co",
        "company",
        "corp",
        "corporation",
        "inc",
        "limited",
        "ltd",
        "plc",
        "pte",
    }

    def acronym(value: str) -> str:
        tokens = [
            token
            for token in re.findall(r"[a-z]+", value.lower())
            if token not in legal_suffixes
        ]
        return "".join(token[0] for token in tokens)

    expected_norm = _identity(expected)
    actual_norm = _identity(actual)
    return (
        len(expected_norm) >= 2 and expected_norm == acronym(actual)
    ) or (
        len(actual_norm) >= 2 and actual_norm == acronym(expected)
    )


def _title_tokens(value: str) -> set[str]:
    return {
        token
        for token in re.findall(r"[a-z0-9]+|[\u3040-\u30ff\u3400-\u9fff]+", value.lower())
        if token not in {"at", "the", "and", "or", "of", "for"}
    }


def _title_match(expected: str, actual: str) -> tuple[bool, str]:
    expected_norm = _identity(expected)
    actual_norm = _identity(actual)
    if expected_norm == actual_norm:
        return True, "title_exact"
    if len(expected_norm) >= 8 and (
        expected_norm in actual_norm or actual_norm in expected_norm
    ):
        return True, "title_contained"
    expected_tokens = _title_tokens(expected)
    actual_tokens = _title_tokens(actual)
    overlap = len(expected_tokens & actual_tokens) / max(1, len(expected_tokens))
    return overlap >= 0.65, f"title_token_overlap:{overlap:.2f}"


def _location_match(expected: str, actual: str) -> tuple[bool, str]:
    expected_norm = _identity(expected)
    actual_norm = _identity(actual)
    matched = expected_norm == actual_norm or (
        len(expected_norm) >= 4
        and (expected_norm in actual_norm or actual_norm in expected_norm)
    )
    return matched, "location_match" if matched else "location_mismatch"


def search_queries(job: sqlite3.Row, hint: CompanySourceHint | None) -> tuple[str, ...]:
    stable = str(job["source_id"])
    base = f'"{job["company"]}" "{job["title"]}" "{job["location"]}"'
    queries = [f"{base} {stable}"]
    if hint and hint.reviewed:
        for domain in hint.official_domains + hint.ats_domains:
            queries.append(f"site:{domain} {base} {stable}")
    return tuple(queries)


@dataclass(frozen=True)
class CandidateDecision:
    accepted: bool
    reason: str
    confidence_reasons: tuple[str, ...]
    source_type: str
    extracted: ExtractedPosting | None
    result: RetrievalResult | None
    content_checksum: str | None


class OfficialSourceResolver:
    def __init__(
        self,
        retriever: Retriever,
        search_provider: SourceSearchProvider | None = None,
        hints: Mapping[str, CompanySourceHint] | None = None,
    ):
        self.retriever = retriever
        self.search_provider = search_provider or EmptySearchProvider()
        self.hints = dict(hints or {})

    def _hint(self, company: str) -> CompanySourceHint | None:
        return self.hints.get(_identity(company))

    def _domain_allowed(
        self, url: str, source_type: str, hint: CompanySourceHint | None
    ) -> bool:
        if source_type == "official_ats":
            if is_official_ats_url(url):
                return True
            return bool(
                hint
                and hint.reviewed
                and _host_matches(_host(url), hint.ats_domains)
            )
        return bool(
            hint
            and hint.reviewed
            and _host_matches(_host(url), hint.official_domains)
        )

    def evaluate(
        self,
        job: sqlite3.Row,
        candidate: SourceSearchCandidate,
        hint: CompanySourceHint | None,
    ) -> CandidateDecision:
        try:
            url = validate_public_url(candidate.url, resolve_dns=False)
        except UnsafePublicUrl as exc:
            return CandidateDecision(
                False, str(exc), (), "other", None, None, None
            )
        source_type = classify_source_url(url)
        if source_type == "linkedin":
            return CandidateDecision(
                False,
                "linkedin_not_official_candidate",
                (),
                "other",
                None,
                None,
                None,
            )
        if not self._domain_allowed(url, source_type, hint):
            return CandidateDecision(
                False,
                "unreviewed_or_unrecognized_domain",
                (),
                source_type,
                None,
                None,
                None,
            )
        result = self.retriever.retrieve(url)
        if result.retrieval_status not in {"success", "partial"}:
            return CandidateDecision(
                False,
                f"retrieval_{result.retrieval_status}",
                (),
                source_type,
                None,
                result,
                None,
            )
        extracted = extract_posting(result.body, result.final_url) if result.body else None
        if not extracted:
            return CandidateDecision(
                False,
                "posting_not_extractable",
                (),
                source_type,
                None,
                result,
                None,
            )
        fields = extracted.fields
        if not fields.get("company"):
            reason = "missing_company"
        elif not _organization_matches(job["company"], fields["company"]) and not (
            hint
            and hint.reviewed
            and _reviewed_organization_acronym_matches(
                job["company"], fields["company"]
            )
        ):
            reason = "company_mismatch"
        elif not fields.get("job_title"):
            reason = "missing_title"
        else:
            title_ok, title_reason = _title_match(job["title"], fields["job_title"])
            if not title_ok:
                reason = "title_mismatch"
            elif not fields.get("location"):
                reason = "missing_location"
            else:
                location_ok, location_reason = _location_match(
                    job["location"], fields["location"]
                )
                if not location_ok:
                    reason = "location_mismatch"
                else:
                    confidence = (
                        "company_match",
                        title_reason,
                        location_reason,
                        "reviewed_domain" if hint and hint.reviewed else "recognized_ats",
                    )
                    checksum = _checksum(
                        _stable_json(
                            {
                                "fields": dict(fields),
                            }
                        )
                    )
                    return CandidateDecision(
                        True,
                        "identity_verified",
                        confidence,
                        source_type,
                        extracted,
                        result,
                        checksum,
                    )
        checksum = _checksum(
            _stable_json(
                {
                    "fields": dict(fields),
                }
            )
        )
        return CandidateDecision(
            False,
            reason,
            (),
            source_type,
            extracted,
            result,
            checksum,
        )

    def _record(
        self,
        conn: sqlite3.Connection,
        job: sqlite3.Row,
        candidate: SourceSearchCandidate,
        decision: CandidateDecision,
    ) -> None:
        evaluated_at = utc_now()
        try:
            domain = _host(validate_public_url(candidate.url, resolve_dns=False))
        except UnsafePublicUrl:
            domain = urlparse(candidate.url).hostname or ""
        result = decision.result
        conn.execute(
            """
            INSERT INTO job_source_candidates(
              job_id, candidate_url, domain, source_type, discovery_method,
              provider, search_query, provider_rank, discovered_at, evaluated_at,
              decision, decision_reason, confidence_reasons_json,
              retrieval_status, http_status, content_checksum
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(job_id, candidate_url, provider) DO UPDATE SET
              domain = excluded.domain,
              source_type = excluded.source_type,
              search_query = excluded.search_query,
              provider_rank = excluded.provider_rank,
              evaluated_at = excluded.evaluated_at,
              decision = excluded.decision,
              decision_reason = excluded.decision_reason,
              confidence_reasons_json = excluded.confidence_reasons_json,
              retrieval_status = excluded.retrieval_status,
              http_status = excluded.http_status,
              content_checksum = excluded.content_checksum
            """,
            (
                job["id"],
                candidate.url,
                domain,
                decision.source_type,
                candidate.discovery_method,
                candidate.provider,
                candidate.query,
                candidate.rank,
                _iso(evaluated_at),
                _iso(evaluated_at),
                "accepted" if decision.accepted else "rejected",
                decision.reason,
                _stable_json(decision.confidence_reasons),
                result.retrieval_status if result else None,
                result.status_code if result else None,
                decision.content_checksum,
            ),
        )

    def resolve(self, conn: sqlite3.Connection, job: sqlite3.Row) -> set[str]:
        hint = self._hint(job["company"])
        queries = search_queries(job, hint)
        candidates: list[SourceSearchCandidate] = []
        if hint and hint.reviewed:
            candidates.extend(
                SourceSearchCandidate(
                    url=url,
                    provider="human_reviewed_hint",
                    query=None,
                    rank=index,
                    discovery_method="human_reviewed_hint",
                )
                for index, url in enumerate(hint.candidate_urls, 1)
            )
        candidates.extend(self.search_provider.search(job, queries))
        unique: dict[tuple[str, str], SourceSearchCandidate] = {}
        for candidate in candidates:
            unique.setdefault((candidate.url, candidate.provider), candidate)
        careers_links: set[str] = set()
        for candidate in unique.values():
            decision = self.evaluate(job, candidate, hint)
            self._record(conn, job, candidate, decision)
            if decision.accepted and decision.result and decision.extracted:
                _persist_snapshot(
                    conn,
                    job["id"],
                    candidate.url,
                    decision.source_type,
                    decision.result,
                    decision.extracted,
                )
                careers_links.update(decision.extracted.careers_links)
        conn.commit()
        return careers_links
