from __future__ import annotations

import hashlib
import ipaddress
import json
import re
import socket
import sqlite3
from collections import Counter, defaultdict
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from html import unescape
from pathlib import Path
from typing import Any, Mapping, Protocol
from urllib.parse import urljoin, urlparse, urlunparse

import httpx
from bs4 import BeautifulSoup

VERIFICATION_STATUSES = {
    "verified_official",
    "verified_ats",
    "linkedin_only",
    "partial",
    "unavailable",
    "closed",
    "conflicting",
}
SOURCE_PRECEDENCE = {
    "alert_email": 1,
    "linkedin": 2,
    "official_ats": 3,
    "official_company": 4,
    "other": 0,
}
ATS_HOST_SUFFIXES = (
    "ashbyhq.com",
    "bamboohr.com",
    "greenhouse.io",
    "herp.careers",
    "hrmos.co",
    "icims.com",
    "jobvite.com",
    "lever.co",
    "myworkdayjobs.com",
    "smartrecruiters.com",
    "taleo.net",
    "talentio.com",
    "workable.com",
    "workdayjobs.com",
)
EXCLUDED_EXTERNAL_HOST_SUFFIXES = (
    "facebook.com",
    "google.com",
    "instagram.com",
    "linkedin.com",
    "t.co",
    "twitter.com",
    "x.com",
    "youtube.com",
)
CONFLICT_FIELDS = {
    "job_title",
    "company",
    "location",
    "workplace_type",
    "employment_type",
    "published_date",
    "closing_date",
    "compensation",
}
CLOSED_PATTERNS = (
    "job is no longer available",
    "position is no longer available",
    "position has been filled",
    "this job has closed",
    "this job is closed",
    "applications are closed",
)
ACCESS_RESTRICTION_PATTERNS = (
    "verify you are human",
    "unusual traffic",
    "access denied",
    "temporarily blocked",
)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _normalized_space(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _stable_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _checksum(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _source_url(value: str) -> str:
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https", "gmail"}:
        raise ValueError("source URL must use http, https, or the local gmail provenance scheme")
    if parsed.scheme in {"http", "https"} and not parsed.netloc:
        raise ValueError("source URL must include a host")
    return urlunparse(parsed._replace(fragment=""))


def _host(url: str) -> str:
    return (urlparse(url).hostname or "").lower().removeprefix("www.")


def _host_matches(host: str, suffixes: tuple[str, ...]) -> bool:
    return any(host == suffix or host.endswith("." + suffix) for suffix in suffixes)


def is_linkedin_url(url: str) -> bool:
    return _host_matches(_host(url), ("linkedin.com",))


def is_official_ats_url(url: str) -> bool:
    return _host_matches(_host(url), ATS_HOST_SUFFIXES)


def classify_source_url(url: str) -> str:
    if is_linkedin_url(url):
        return "linkedin"
    if is_official_ats_url(url):
        return "official_ats"
    return "official_company"


class UnsafePublicUrl(ValueError):
    pass


def validate_public_url(url: str, *, resolve_dns: bool = False) -> str:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise UnsafePublicUrl("unsupported_url_scheme")
    if not parsed.hostname or parsed.username or parsed.password:
        raise UnsafePublicUrl("invalid_public_url")
    hostname = parsed.hostname.lower().rstrip(".")
    if (
        hostname == "localhost"
        or hostname.endswith(".localhost")
        or hostname.endswith(".local")
        or hostname.endswith(".internal")
    ):
        raise UnsafePublicUrl("private_network_target")

    def reject_address(address: str) -> None:
        try:
            value = ipaddress.ip_address(address)
        except ValueError:
            return
        if not value.is_global:
            raise UnsafePublicUrl("private_network_target")

    reject_address(hostname)
    if resolve_dns:
        try:
            addresses = {
                item[4][0]
                for item in socket.getaddrinfo(
                    hostname,
                    parsed.port or (443 if parsed.scheme == "https" else 80),
                    type=socket.SOCK_STREAM,
                )
            }
        except OSError as exc:
            raise UnsafePublicUrl("dns_resolution_failed") from exc
        for address in addresses:
            reject_address(address)
    return _source_url(url)


def _trusted_redirect(source_url: str, target_url: str) -> bool:
    source_host = _host(source_url)
    target_host = _host(target_url)
    same_domain = (
        source_host == target_host
        or source_host.endswith("." + target_host)
        or target_host.endswith("." + source_host)
    )
    return same_domain


@dataclass(frozen=True)
class RetrievalResult:
    requested_url: str
    final_url: str
    retrieved_at: datetime
    status_code: int | None
    body: str
    retrieval_status: str
    failure_reason: str | None = None


class Retriever(Protocol):
    def retrieve(self, url: str) -> RetrievalResult: ...


class PublicHttpRetriever:
    """Unauthenticated public HTTP retriever with no cookies or bypass behavior."""

    def __init__(
        self,
        timeout_seconds: float = 15.0,
        *,
        max_response_bytes: int = 2_000_000,
        max_redirects: int = 3,
    ):
        self.max_response_bytes = max_response_bytes
        self.max_redirects = max_redirects
        self._client = httpx.Client(
            follow_redirects=False,
            timeout=timeout_seconds,
            headers={
                "User-Agent": "JobSearchOS-PublicVerification/0.2 (+public unauthenticated retrieval)",
                "Accept": "text/html,application/xhtml+xml,application/ld+json",
            },
        )

    def close(self) -> None:
        self._client.close()

    def retrieve(self, url: str) -> RetrievalResult:
        checked_at = utc_now()
        current_url = url
        try:
            for redirect_count in range(self.max_redirects + 1):
                current_url = validate_public_url(current_url, resolve_dns=True)
                with self._client.stream("GET", current_url) as response:
                    if response.is_redirect:
                        location = response.headers.get("location")
                        if not location:
                            raise UnsafePublicUrl("redirect_missing_location")
                        target = validate_public_url(
                            urljoin(current_url, location), resolve_dns=True
                        )
                        if not _trusted_redirect(current_url, target):
                            raise UnsafePublicUrl("untrusted_redirect")
                        if redirect_count >= self.max_redirects:
                            raise UnsafePublicUrl("too_many_redirects")
                        current_url = target
                        continue
                    content_length = response.headers.get("content-length")
                    if content_length and int(content_length) > self.max_response_bytes:
                        raise UnsafePublicUrl("response_too_large")
                    chunks: list[bytes] = []
                    size = 0
                    for chunk in response.iter_bytes():
                        size += len(chunk)
                        if size > self.max_response_bytes:
                            raise UnsafePublicUrl("response_too_large")
                        chunks.append(chunk)
                    body_bytes = b"".join(chunks)
                    status_code = response.status_code
                    headers = dict(response.headers)
                    final_url = str(response.url)
                    encoding = response.encoding or "utf-8"
                    body_text = body_bytes.decode(encoding, errors="replace")
                    break
            else:
                raise UnsafePublicUrl("too_many_redirects")
        except (httpx.HTTPError, UnsafePublicUrl, ValueError) as exc:
            return RetrievalResult(
                requested_url=url,
                final_url=current_url,
                retrieved_at=checked_at,
                status_code=None,
                body="",
                retrieval_status=(
                    "security_rejected"
                    if isinstance(exc, UnsafePublicUrl)
                    else "network_error"
                ),
                failure_reason=str(exc) or type(exc).__name__,
            )
        content_type = headers.get("content-type", "").lower()
        if content_type and not any(
            allowed in content_type
            for allowed in (
                "text/html",
                "application/xhtml+xml",
                "application/ld+json",
                "text/plain",
            )
        ):
            return RetrievalResult(
                requested_url=url,
                final_url=final_url,
                retrieved_at=checked_at,
                status_code=status_code,
                body="",
                retrieval_status="unsupported_content_type",
                failure_reason=(
                    "unsupported content type: " + content_type.split(";", 1)[0]
                ),
            )
        body = body_text if status_code < 500 else ""
        visible_lower = sanitized_html_to_text(body).lower() if body else ""
        if status_code == 410 or any(term in visible_lower for term in CLOSED_PATTERNS):
            status, reason = "closed", "posting explicitly reports that it is closed"
        elif status_code in {401, 403}:
            status, reason = "access_restricted", f"HTTP {status_code}"
        elif status_code == 429:
            status, reason = "rate_limited", "HTTP 429"
        elif status_code == 404:
            status, reason = "unavailable", "HTTP 404"
        elif status_code >= 400:
            status, reason = "http_error", f"HTTP {status_code}"
        elif any(term in visible_lower for term in ACCESS_RESTRICTION_PATTERNS):
            status, reason = "access_restricted", "public page presented an access-control challenge"
        else:
            status, reason = "success", None
        return RetrievalResult(
            requested_url=url,
            final_url=final_url,
            retrieved_at=checked_at,
            status_code=status_code,
            body=body,
            retrieval_status=status,
            failure_reason=reason,
        )


class FixtureRetriever:
    def __init__(self, responses: Mapping[str, Mapping[str, Any]]):
        self.responses = responses

    @classmethod
    def from_json(cls, path: str | Path) -> "FixtureRetriever":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        responses = data.get("responses", data) if isinstance(data, dict) else None
        if not isinstance(responses, dict):
            raise ValueError("retrieval fixture must be a URL-to-response mapping")
        return cls(responses)

    def retrieve(self, url: str) -> RetrievalResult:
        checked_at = utc_now()
        response = self.responses.get(url)
        if response is None:
            return RetrievalResult(
                requested_url=url,
                final_url=url,
                retrieved_at=checked_at,
                status_code=None,
                body="",
                retrieval_status="network_error",
                failure_reason="fixture response not found",
            )
        status_code = response.get("status_code")
        body = str(response.get("body") or "")
        status = str(response.get("retrieval_status") or "")
        if not status:
            lower = body.lower()
            if status_code == 410 or any(term in lower for term in CLOSED_PATTERNS):
                status = "closed"
            elif status_code in {401, 403}:
                status = "access_restricted"
            elif status_code == 429:
                status = "rate_limited"
            elif status_code == 404:
                status = "unavailable"
            elif status_code is not None and status_code >= 400:
                status = "http_error"
            else:
                status = "success"
        return RetrievalResult(
            requested_url=url,
            final_url=str(response.get("final_url") or url),
            retrieved_at=checked_at,
            status_code=status_code,
            body=body,
            retrieval_status=status,
            failure_reason=response.get("failure_reason"),
        )


@dataclass(frozen=True)
class ExtractedPosting:
    fields: Mapping[str, Any]
    sanitized_text: str
    posting_links: tuple[str, ...]
    careers_links: tuple[str, ...]
    complete_description: bool


def sanitized_html_to_text(value: str) -> str:
    soup = BeautifulSoup(value or "", "html.parser")
    for element in soup(["script", "style", "noscript", "svg", "form", "nav", "header", "footer"]):
        element.decompose()
    lines = [_normalized_space(unescape(line)) for line in soup.get_text("\n").splitlines()]
    return "\n".join(line for line in lines if line)


def _json_ld_objects(soup: BeautifulSoup) -> list[dict[str, Any]]:
    values: list[dict[str, Any]] = []

    def collect(item: Any) -> None:
        if isinstance(item, dict):
            values.append(item)
            for child in item.get("@graph", []):
                collect(child)
        elif isinstance(item, list):
            for child in item:
                collect(child)

    for script in soup.find_all("script", attrs={"type": re.compile(r"ld\+json", re.I)}):
        try:
            collect(json.loads(script.string or script.get_text() or "null"))
        except (json.JSONDecodeError, TypeError):
            continue
    return values


def _is_type(value: Any, expected: str) -> bool:
    types = value if isinstance(value, list) else [value]
    return any(str(item).lower() == expected.lower() for item in types)


def _job_location(value: Any) -> str | None:
    locations = value if isinstance(value, list) else [value]
    rendered: list[str] = []
    for location in locations:
        if not isinstance(location, dict):
            continue
        address = location.get("address", location)
        if not isinstance(address, dict):
            continue
        pieces = [
            address.get("addressLocality"),
            address.get("addressRegion"),
            address.get("addressCountry"),
        ]
        text = ", ".join(str(piece) for piece in pieces if piece)
        if text and text not in rendered:
            rendered.append(text)
    return "; ".join(rendered) or None


def _as_text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, list):
        text = ", ".join(str(item) for item in value if item)
    elif isinstance(value, dict):
        text = _stable_json(value)
    else:
        text = str(value)
    return _normalized_space(text) or None


def _description_text(value: Any) -> str:
    return sanitized_html_to_text(str(value or ""))


def _section_lines(text: str, heading_terms: tuple[str, ...]) -> list[str]:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    all_headings = (
        "responsibilities",
        "what you will do",
        "what you'll do",
        "qualifications",
        "requirements",
        "what you bring",
        "language requirements",
        "languages",
        "about the role",
        "about us",
        "benefits",
    )
    capture = False
    found: list[str] = []
    for line in lines:
        normalized = re.sub(r"[^a-z ]", "", line.lower()).strip()
        is_heading = len(line) <= 80 and any(normalized == term for term in all_headings)
        if is_heading:
            capture = any(normalized == term for term in heading_terms)
            continue
        if capture:
            found.append(line)
    return found


def _exact_signal_lines(text: str, terms: tuple[str, ...]) -> list[str]:
    pieces = re.split(r"(?<=[.!?。])\s+|\n+", text)
    found: list[str] = []
    for piece in pieces:
        line = _normalized_space(piece)
        if line and any(re.search(term, line, re.I) for term in terms) and line not in found:
            found.append(line)
    return found


def _candidate_links(soup: BeautifulSoup, base_url: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    posting: list[str] = []
    careers: list[str] = []
    for anchor in soup.find_all("a", href=True):
        href = urljoin(base_url, str(anchor["href"]))
        parsed = urlparse(href)
        if parsed.scheme not in {"http", "https"}:
            continue
        host = _host(href)
        text = _normalized_space(anchor.get_text(" ", strip=True)).lower()
        if _host_matches(host, EXCLUDED_EXTERNAL_HOST_SUFFIXES):
            continue
        clean = _source_url(href)
        if "career" in text or re.search(r"/(careers?|jobs?)(?:/|$)", parsed.path, re.I):
            if clean not in careers:
                careers.append(clean)
        if is_official_ats_url(clean) or any(
            term in text for term in ("apply", "view job", "job details", "original job")
        ):
            if clean not in posting:
                posting.append(clean)
    return tuple(posting), tuple(careers)


def extract_posting(html: str, source_url: str) -> ExtractedPosting:
    soup = BeautifulSoup(html or "", "html.parser")
    page_text = sanitized_html_to_text(html)
    posting_links, careers_links = _candidate_links(soup, source_url)
    fields: dict[str, Any] = {}
    job_posting: dict[str, Any] | None = None
    for item in _json_ld_objects(soup):
        if _is_type(item.get("@type"), "JobPosting"):
            job_posting = item
            break
    description = ""
    if job_posting:
        organization = job_posting.get("hiringOrganization")
        if isinstance(organization, dict):
            fields["company"] = _as_text(organization.get("name"))
        fields.update(
            {
                "job_title": _as_text(job_posting.get("title")),
                "location": _job_location(job_posting.get("jobLocation")),
                "workplace_type": _as_text(job_posting.get("jobLocationType")),
                "employment_type": _as_text(job_posting.get("employmentType")),
                "published_date": _as_text(job_posting.get("datePosted")),
                "closing_date": _as_text(job_posting.get("validThrough")),
                "compensation": _as_text(job_posting.get("baseSalary")),
            }
        )
        description = _description_text(job_posting.get("description"))
        if responsibilities := _description_text(job_posting.get("responsibilities")):
            fields["responsibilities"] = [
                line for line in responsibilities.splitlines() if line.strip()
            ]
        if qualifications := _description_text(job_posting.get("qualifications")):
            fields["qualifications"] = [
                line for line in qualifications.splitlines() if line.strip()
            ]
        linked_url = job_posting.get("url")
        if linked_url:
            linked = _source_url(urljoin(source_url, str(linked_url)))
            if not is_linkedin_url(linked) and linked not in posting_links:
                posting_links = posting_links + (linked,)
    if not description:
        main = (
            soup.select_one("div.description__text")
            if is_linkedin_url(source_url)
            else None
        )
        main = main or soup.find("main") or soup.find("article")
        description = sanitized_html_to_text(str(main)) if main else page_text
    if description:
        fields["job_description"] = description
    if not fields.get("responsibilities"):
        values = _section_lines(description, ("responsibilities", "what you will do", "what you'll do"))
        if values:
            fields["responsibilities"] = values
    if not fields.get("qualifications"):
        values = _section_lines(description, ("qualifications", "requirements", "what you bring"))
        if values:
            fields["qualifications"] = values
    languages = _exact_signal_lines(
        description,
        (
            r"\b(?:japanese|mandarin|chinese|english|thai)\b",
            r"\blanguage (?:requirement|proficiency|fluency)\b",
        ),
    )
    if languages:
        fields["language_requirements"] = languages
    seniority = _exact_signal_lines(
        description,
        (r"\b(?:senior|principal|staff|director|head|vice president|vp|c-level|executive)\b",),
    )
    if seniority:
        fields["seniority_signals"] = seniority
    leadership = _exact_signal_lines(
        description,
        (
            r"\b(?:lead|manage|mentor|direct reports?|team of|hiring|strategy|ownership|stakeholders?)\b",
        ),
    )
    if leadership:
        fields["leadership_scope_signals"] = leadership
    if careers_links:
        fields["company_careers_url"] = careers_links[0]
    fields = {key: value for key, value in fields.items() if value not in (None, "", [], ())}
    complete = len(description) >= 200 and bool(
        fields.get("responsibilities") or fields.get("qualifications")
    )
    return ExtractedPosting(
        fields=fields,
        sanitized_text=page_text,
        posting_links=tuple(dict.fromkeys(posting_links)),
        careers_links=tuple(dict.fromkeys(careers_links)),
        complete_description=complete,
    )


def _normalized_identity(value: Any) -> str:
    if isinstance(value, str):
        return re.sub(r"[^a-z0-9]+", "", value.lower())
    return _stable_json(value).lower()


def _identity_values_conflict(field_name: str, values: set[str]) -> bool:
    if len(values) <= 1:
        return False
    if field_name not in {"company", "location"}:
        return True
    ordered = sorted(values, key=len)
    return any(
        shorter not in longer
        for index, shorter in enumerate(ordered)
        for longer in ordered[index + 1 :]
    )


def _organization_matches(expected: str, actual: Any) -> bool:
    if not actual:
        return False
    expected_norm = _normalized_identity(expected)
    actual_norm = _normalized_identity(actual)
    return expected_norm == actual_norm or (
        len(expected_norm) >= 5
        and (expected_norm in actual_norm or actual_norm in expected_norm)
    )


def _posting_identity_matches(job: sqlite3.Row, fields: Mapping[str, Any]) -> bool:
    if not _organization_matches(job["company"], fields.get("company")):
        return False
    expected_title = _normalized_identity(job["title"])
    actual_title = _normalized_identity(fields.get("job_title", ""))
    if not expected_title or not actual_title or not (
        expected_title == actual_title
        or expected_title in actual_title
        or actual_title in expected_title
    ):
        return False
    expected_location = _normalized_identity(job["location"])
    actual_location = _normalized_identity(fields.get("location", ""))
    return bool(
        expected_location
        and actual_location
        and (
            expected_location == actual_location
            or expected_location in actual_location
            or actual_location in expected_location
        )
    )


def _snapshot_status(source_type: str, retrieval_status: str, fields: Mapping[str, Any]) -> str:
    if retrieval_status == "closed":
        return "closed"
    if retrieval_status != "success":
        return "unavailable"
    if source_type == "official_company":
        return "verified_official"
    if source_type == "official_ats":
        return "verified_ats"
    if source_type == "linkedin" and fields:
        return "linkedin_only"
    return "partial"


def _persist_snapshot(
    conn: sqlite3.Connection,
    job_id: int,
    source_url: str,
    source_type: str,
    result: RetrievalResult,
    extracted: ExtractedPosting | None,
) -> int:
    final_url = _source_url(result.final_url or source_url)
    fields = dict(extracted.fields) if extracted else {}
    content_text = str(fields.get("job_description") or "")
    content_basis = _stable_json(
        {
            "text": _normalized_space(content_text),
            "fields": fields,
            "http_status": result.status_code,
            "retrieval_status": result.retrieval_status,
            "failure_reason": result.failure_reason,
        }
    )
    checksum = _checksum(content_basis)
    verification = _snapshot_status(source_type, result.retrieval_status, fields)
    conn.execute(
        """
        INSERT OR IGNORE INTO job_source_snapshots(
          job_id, source_url, source_type, retrieved_at, http_status,
          retrieval_status, verification_status, content_checksum, content_text,
          extracted_json, failure_reason
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            job_id,
            final_url,
            source_type,
            _iso(result.retrieved_at),
            result.status_code,
            result.retrieval_status,
            verification,
            checksum,
            content_text,
            _stable_json(fields),
            result.failure_reason,
        ),
    )
    snapshot_id = conn.execute(
        """
        SELECT id FROM job_source_snapshots
        WHERE job_id = ? AND source_url = ? AND content_checksum = ?
        """,
        (job_id, final_url, checksum),
    ).fetchone()[0]
    successful = result.retrieval_status in {"success", "partial", "closed"}
    conn.execute(
        """
        INSERT INTO job_source_state(
          job_id, source_url, source_type, last_checked_at,
          last_successfully_checked_at, http_status, retrieval_status,
          failure_reason, current_snapshot_id
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(job_id, source_url) DO UPDATE SET
          source_type = excluded.source_type,
          last_checked_at = excluded.last_checked_at,
          last_successfully_checked_at = CASE
            WHEN excluded.last_successfully_checked_at IS NOT NULL
            THEN excluded.last_successfully_checked_at
            ELSE job_source_state.last_successfully_checked_at
          END,
          http_status = excluded.http_status,
          retrieval_status = excluded.retrieval_status,
          failure_reason = excluded.failure_reason,
          current_snapshot_id = CASE
            WHEN excluded.last_successfully_checked_at IS NOT NULL
            THEN excluded.current_snapshot_id
            ELSE job_source_state.current_snapshot_id
          END
        """,
        (
            job_id,
            final_url,
            source_type,
            _iso(result.retrieved_at),
            _iso(result.retrieved_at) if successful else None,
            result.status_code,
            result.retrieval_status,
            result.failure_reason,
            snapshot_id if successful else None,
        ),
    )
    for field_name, value in fields.items():
        value_json = _stable_json(value)
        conn.execute(
            """
            INSERT OR IGNORE INTO job_field_values(
              job_id, field_name, value_json, value_checksum, source_snapshot_id
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (job_id, field_name, value_json, _checksum(value_json), snapshot_id),
        )
    return snapshot_id


def _persist_alert_snapshot(conn: sqlite3.Connection, job: sqlite3.Row) -> int:
    source_url = f"gmail://message/{job['gmail_message_id']}"
    retrieved_at = datetime.fromisoformat(job["alert_timestamp"])
    if retrieved_at.tzinfo is None:
        retrieved_at = retrieved_at.replace(tzinfo=timezone.utc)
    fields = {
        "job_title": job["title"],
        "company": job["company"],
        "location": job["location"],
    }
    extracted = ExtractedPosting(
        fields=fields,
        sanitized_text=_stable_json(fields),
        posting_links=(),
        careers_links=(),
        complete_description=False,
    )
    return _persist_snapshot(
        conn,
        job["id"],
        source_url,
        "alert_email",
        RetrievalResult(
            requested_url=source_url,
            final_url=source_url,
            retrieved_at=retrieved_at,
            status_code=None,
            body="",
            retrieval_status="success",
        ),
        extracted,
    )


def _retrieve_source(
    conn: sqlite3.Connection,
    retriever: Retriever,
    job: sqlite3.Row,
    url: str,
    source_type: str,
) -> tuple[RetrievalResult, ExtractedPosting | None, str]:
    result = retriever.retrieve(url)
    extracted = extract_posting(result.body, result.final_url) if result.body else None
    if result.retrieval_status == "success" and (
        extracted is None
        or not any(
            field in extracted.fields
            for field in ("job_title", "company")
        )
    ):
        result = replace(
            result,
            retrieval_status="partial",
            failure_reason="public page contained no extractable job posting content",
        )
    effective_type = source_type
    if source_type in {"official_company", "official_ats"}:
        if not extracted or not _posting_identity_matches(job, extracted.fields):
            effective_type = "other"
    _persist_snapshot(conn, job["id"], url, effective_type, result, extracted)
    return result, extracted, effective_type


def _current_field_candidates(conn: sqlite3.Connection, job_id: int) -> dict[str, list[sqlite3.Row]]:
    rows = conn.execute(
        """
        SELECT fv.field_name, fv.value_json, fv.source_snapshot_id,
               snapshots.source_type, snapshots.source_url, snapshots.retrieved_at
        FROM job_field_values AS fv
        JOIN job_source_snapshots AS snapshots ON snapshots.id = fv.source_snapshot_id
        JOIN job_source_state AS state
          ON state.job_id = snapshots.job_id
         AND state.source_url = snapshots.source_url
         AND state.current_snapshot_id = snapshots.id
        WHERE fv.job_id = ?
        """,
        (job_id,),
    ).fetchall()
    grouped: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for row in rows:
        grouped[row["field_name"]].append(row)
    return grouped


def _aggregate_job(
    conn: sqlite3.Connection,
    job: sqlite3.Row,
    attempted_at: datetime,
    careers_links: set[str],
) -> dict[str, Any]:
    candidates = _current_field_candidates(conn, job["id"])
    selected: dict[str, sqlite3.Row] = {}
    conflicts: list[str] = []
    for field_name, rows in candidates.items():
        rows.sort(
            key=lambda row: (
                SOURCE_PRECEDENCE.get(row["source_type"], 0),
                row["retrieved_at"],
            ),
            reverse=True,
        )
        selected[field_name] = rows[0]
        if field_name in CONFLICT_FIELDS:
            normalized_values = {
                _normalized_identity(json.loads(row["value_json"])) for row in rows
            }
            if _identity_values_conflict(field_name, normalized_values):
                conflicts.append(field_name)
    conn.execute("DELETE FROM job_current_fields WHERE job_id = ?", (job["id"],))
    for field_name, row in selected.items():
        conn.execute(
            """
            INSERT INTO job_current_fields(
              job_id, field_name, value_json, source_snapshot_id, selected_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                job["id"],
                field_name,
                row["value_json"],
                row["source_snapshot_id"],
                _iso(attempted_at),
            ),
        )
    states = conn.execute(
        """
        SELECT state.*, snapshots.extracted_json, snapshots.verification_status,
               snapshots.retrieved_at AS snapshot_retrieved_at
        FROM job_source_state AS state
        LEFT JOIN job_source_snapshots AS snapshots
          ON snapshots.id = state.current_snapshot_id
        WHERE state.job_id = ? AND state.source_type != 'alert_email'
        """,
        (job["id"],),
    ).fetchall()
    active = []
    closed = []
    official_postings: list[sqlite3.Row] = []
    for state in states:
        extracted_fields = json.loads(state["extracted_json"] or "{}")
        if state["retrieval_status"] == "closed":
            closed.append(state)
        if state["current_snapshot_id"] and extracted_fields:
            if state["verification_status"] == "closed":
                closed.append(state)
            else:
                active.append(state)
            if state["source_type"] in {"official_company", "official_ats"} and (
                extracted_fields.get("job_title") or extracted_fields.get("job_description")
            ):
                official_postings.append(state)
    if closed and active:
        if "availability" not in conflicts:
            conflicts.append("availability")
    complete_description = False
    if "job_description" in selected:
        description = json.loads(selected["job_description"]["value_json"])
        selected_source = selected["job_description"]["source_snapshot_id"]
        detail_fields = {
            field: selected[field]["source_snapshot_id"]
            for field in ("responsibilities", "qualifications")
            if field in selected
        }
        complete_description = len(description) >= 200 and any(
            source_id == selected_source for source_id in detail_fields.values()
        )
    if conflicts:
        verification = "conflicting"
    elif closed and not active:
        verification = "closed"
    else:
        active_types = {state["source_type"] for state in active}
        if "official_company" in active_types:
            verification = "verified_official"
        elif "official_ats" in active_types:
            verification = "verified_ats"
        elif "linkedin" in active_types:
            verification = "linkedin_only" if complete_description else "partial"
        elif any(state["retrieval_status"] == "partial" for state in states):
            verification = "partial"
        else:
            verification = "unavailable"
    official_postings.sort(
        key=lambda state: SOURCE_PRECEDENCE[state["source_type"]], reverse=True
    )
    official_posting_url = official_postings[0]["source_url"] if official_postings else None
    if "company_careers_url" in selected:
        company_careers_url = json.loads(
            selected["company_careers_url"]["value_json"]
        )
    else:
        company_careers_url = sorted(careers_links)[0] if careers_links else None
    successful_times = [
        state["last_successfully_checked_at"]
        for state in states
        if state["last_successfully_checked_at"]
    ]
    failures = [
        state["failure_reason"] or state["retrieval_status"]
        for state in states
        if state["retrieval_status"] not in {"success", "closed"}
    ]
    failure_reason = "; ".join(dict.fromkeys(failures)) or None
    conn.execute(
        """
        INSERT INTO job_enrichments(
          job_id, verification_status, official_posting_url, company_careers_url,
          complete_description, conflict_fields_json, last_attempted_at,
          last_successfully_checked_at, failure_reason, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(job_id) DO UPDATE SET
          verification_status = excluded.verification_status,
          official_posting_url = excluded.official_posting_url,
          company_careers_url = excluded.company_careers_url,
          complete_description = excluded.complete_description,
          conflict_fields_json = excluded.conflict_fields_json,
          last_attempted_at = excluded.last_attempted_at,
          last_successfully_checked_at = excluded.last_successfully_checked_at,
          failure_reason = excluded.failure_reason,
          updated_at = excluded.updated_at
        """,
        (
            job["id"],
            verification,
            official_posting_url,
            company_careers_url,
            int(complete_description),
            _stable_json(sorted(conflicts)),
            _iso(attempted_at),
            max(successful_times) if successful_times else None,
            failure_reason,
            _iso(attempted_at),
        ),
    )
    return {
        "job_id": job["id"],
        "title": job["title"],
        "company": job["company"],
        "location": job["location"],
        "verification_status": verification,
        "official_posting_url": official_posting_url,
        "company_careers_url": company_careers_url,
        "complete_description": complete_description,
        "conflict_fields": sorted(conflicts),
        "failure_reason": failure_reason,
    }


def enrich_job(
    conn: sqlite3.Connection,
    job: sqlite3.Row,
    retriever: Retriever,
    attempted_at: datetime | None = None,
    resolver: Any | None = None,
) -> dict[str, Any]:
    attempted_at = attempted_at or utc_now()
    _persist_alert_snapshot(conn, job)
    existing_enrichment = conn.execute(
        "SELECT company_careers_url FROM job_enrichments WHERE job_id = ?",
        (job["id"],),
    ).fetchone()
    careers_links: set[str] = set()
    if existing_enrichment and existing_enrichment[0]:
        careers_links.add(existing_enrichment[0])
    public_url = job["canonical_job_url"] or job["source_url"]
    discovered_postings: list[str] = []
    if public_url:
        _, extracted, _ = _retrieve_source(
            conn, retriever, job, public_url, "linkedin" if is_linkedin_url(public_url) else classify_source_url(public_url)
        )
        if extracted:
            discovered_postings.extend(extracted.posting_links)
            careers_links.update(extracted.careers_links)
    prior_official_urls = [
        row[0]
        for row in conn.execute(
            """
            SELECT source_url FROM job_source_state
            WHERE job_id = ? AND source_type IN ('official_company', 'official_ats')
            """,
            (job["id"],),
        ).fetchall()
    ]
    for url in dict.fromkeys(discovered_postings + prior_official_urls):
        if public_url and _source_url(url) == _source_url(public_url):
            continue
        source_type = classify_source_url(url)
        _, extracted, effective_type = _retrieve_source(
            conn, retriever, job, url, source_type
        )
        if extracted:
            careers_links.update(extracted.careers_links)
            if effective_type in {"official_company", "official_ats"}:
                careers_links.update(
                    link for link in extracted.careers_links if not is_linkedin_url(link)
                )
    if resolver is not None:
        careers_links.update(resolver.resolve(conn, job))
    result = _aggregate_job(conn, job, attempted_at, careers_links)
    decision, reason = eligibility_for(
        result["verification_status"], result["complete_description"]
    )
    conn.execute(
        """
        INSERT INTO job_eligibility_decisions(
          job_id, decision, reason, verification_status,
          complete_description, decided_at
        ) VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(job_id) DO UPDATE SET
          decision = excluded.decision,
          reason = excluded.reason,
          verification_status = excluded.verification_status,
          complete_description = excluded.complete_description,
          decided_at = excluded.decided_at
        """,
        (
            job["id"],
            decision,
            reason,
            result["verification_status"],
            int(result["complete_description"]),
            _iso(attempted_at),
        ),
    )
    result["eligibility"] = decision
    result["eligibility_reason"] = reason
    conn.commit()
    return result


def eligibility_for(
    verification_status: str, complete_description: bool
) -> tuple[str, str]:
    if verification_status in {"verified_official", "verified_ats"}:
        return "eligible", "verified official source"
    if verification_status == "linkedin_only" and complete_description:
        return (
            "conditionally_eligible",
            "complete public LinkedIn description without an official match",
        )
    if verification_status in {"partial", "conflicting"}:
        return "manual_review", f"verification status is {verification_status}"
    return "ineligible", f"verification status is {verification_status}"


def _selected_jobs(
    conn: sqlite3.Connection,
    job_ids: list[int] | None,
    max_results: int,
    refresh: bool,
) -> list[sqlite3.Row]:
    parameters: list[Any] = []
    where: list[str] = []
    if job_ids:
        where.append("jobs.id IN (" + ",".join("?" for _ in job_ids) + ")")
        parameters.extend(job_ids)
    if not refresh:
        where.append("enrichment.job_id IS NULL")
    query = """
        SELECT jobs.* FROM jobs
        LEFT JOIN job_enrichments AS enrichment ON enrichment.job_id = jobs.id
    """
    if where:
        query += " WHERE " + " AND ".join(where)
    query += " ORDER BY jobs.id LIMIT ?"
    parameters.append(max_results)
    return conn.execute(query, parameters).fetchall()


def enrichment_summary(
    conn: sqlite3.Connection,
    results: list[dict[str, Any]],
    attempted_job_ids: list[int],
) -> dict[str, Any]:
    status_counts = Counter(result["verification_status"] for result in results)
    source_counts: Counter[str] = Counter()
    if attempted_job_ids:
        placeholders = ",".join("?" for _ in attempted_job_ids)
        source_counts.update(
            row[0]
            for row in conn.execute(
                f"SELECT source_type FROM job_source_snapshots WHERE job_id IN ({placeholders})",
                attempted_job_ids,
            ).fetchall()
        )
    official = sum(bool(result["official_posting_url"]) for result in results)
    complete = sum(bool(result["complete_description"]) for result in results)
    attempted = len(results)
    return {
        "attempted": attempted,
        "verification_status_counts": dict(sorted(status_counts.items())),
        "source_type_counts": dict(sorted(source_counts.items())),
        "official_posting_matches": official,
        "official_posting_match_rate": official / attempted if attempted else 0.0,
        "complete_descriptions": complete,
        "complete_description_rate": complete / attempted if attempted else 0.0,
        "conflicts": [result for result in results if result["conflict_fields"]],
        "failures": [result for result in results if result["failure_reason"]],
        "results": results,
    }


def enrich_opportunities(
    conn: sqlite3.Connection,
    retriever: Retriever,
    *,
    job_ids: list[int] | None = None,
    max_results: int = 25,
    refresh: bool = False,
    resolver: Any | None = None,
) -> dict[str, Any]:
    jobs = _selected_jobs(conn, job_ids, max_results, refresh)
    results = [
        enrich_job(conn, job, retriever, resolver=resolver) for job in jobs
    ]
    return enrichment_summary(conn, results, [job["id"] for job in jobs])
