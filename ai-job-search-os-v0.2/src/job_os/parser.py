from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html import unescape
from typing import Iterable
from urllib.parse import urlsplit

from bs4 import BeautifulSoup

from .url_utils import normalize_url

LINKEDIN_JOB_RE = re.compile(
    r"(?:https?://)?(?:www\.)?linkedin\.com/(?:comm/)?jobs/view/\d+[^\s\"'<>)]*",
    re.I,
)
JOB_ID_RE = re.compile(
    r"(?:currentJobId|jobId)=(\d+)|/jobs/view/(\d+)(?:[/?#]|$)",
    re.I,
)
MARKDOWN_LINK_RE = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)", re.S)
WORK_MODE_RE = re.compile(r"\s*\((?:hybrid|on-site|remote)\)\s*$", re.I)
SEPARATOR_RE = re.compile(r"^[-–—_=•·\s]+$")


@dataclass(frozen=True)
class ParsedJobAlert:
    title: str
    company: str
    location: str
    source_url: str | None
    canonical_job_url: str | None
    job_identifier: str
    alert_timestamp: datetime
    gmail_message_id: str


def html_to_text(html: str) -> str:
    soup = BeautifulSoup(html or "", "html.parser")
    for tag in soup(["script", "style"]):
        tag.decompose()
    return "\n".join(
        line.strip() for line in soup.get_text("\n").splitlines() if line.strip()
    )


def _is_linkedin_url(url: str) -> bool:
    parsed = urlsplit(url if "://" in url else "https://" + url)
    hostname = (parsed.hostname or "").lower()
    return hostname == "linkedin.com" or hostname.endswith(".linkedin.com")


def extract_job_id(url: str | None) -> str | None:
    if not url:
        return None
    decoded = unescape(url)
    if not _is_linkedin_url(decoded):
        return None
    match = JOB_ID_RE.search(decoded)
    if not match:
        return None
    return next(group for group in match.groups() if group)


def extract_links(html: str, text: str = "") -> list[str]:
    """Return only stable LinkedIn job-detail links, never navigation links."""
    links: list[str] = []
    soup = BeautifulSoup(html or "", "html.parser")
    for anchor in soup.find_all("a", href=True):
        href = unescape(anchor["href"])
        if extract_job_id(href):
            links.append(href)
    links.extend(match.group(0) for match in LINKEDIN_JOB_RE.finditer(text or ""))
    unique: dict[str, str] = {}
    for link in links:
        canonical = normalize_url(link)
        if canonical:
            unique.setdefault(canonical, link)
    return list(unique.values())


def _timestamp(headers: dict[str, str]) -> datetime:
    raw = headers.get("Date") or headers.get("date")
    if raw:
        try:
            return parsedate_to_datetime(raw).astimezone(timezone.utc)
        except (TypeError, ValueError, OverflowError):
            try:
                parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                return parsed.astimezone(timezone.utc)
            except ValueError:
                pass
    return datetime.now(timezone.utc)


def _clean_lines(value: str | Iterable[str]) -> list[str]:
    raw_lines = value.splitlines() if isinstance(value, str) else value
    lines: list[str] = []
    for raw in raw_lines:
        line = re.sub(r"\s+", " ", raw).strip(" \t•")
        if not line or SEPARATOR_RE.fullmatch(line):
            continue
        lines.append(line)
    return lines


def _listing_fields(lines: Iterable[str]) -> tuple[str, str, str] | None:
    cleaned = _clean_lines(lines)
    if len(cleaned) < 2:
        return None
    title = cleaned[0]
    detail = next((line for line in cleaned[1:] if " · " in line), None)
    if not detail:
        return None
    company, location = (part.strip() for part in detail.rsplit(" · ", 1))
    location = WORK_MODE_RE.sub("", location).strip()
    if not all((title, company, location)):
        return None
    if title == company or len(title) > 300 or len(company) > 200 or len(location) > 200:
        return None
    return title, company, location


def _rejection_reason(url: str) -> str | None:
    lowered = unescape(url).lower()
    if not _is_linkedin_url(lowered):
        return None
    if "unsubscribe" in lowered:
        return "unsubscribe"
    if "/jobs/alerts" in lowered:
        return "alert_management"
    if "/settings" in lowered or "/psettings" in lowered:
        return "settings"
    if "/jobs/search" in lowered or "/jobs/search-results" in lowered:
        return "search"
    if any(path in lowered for path in ("/feed", "/messaging", "/mynetwork", "/notifications")):
        return "navigation"
    if "/jobs/" in lowered:
        return "non_job_path"
    return None


def _record_rejections(
    urls: Iterable[str], diagnostics: Counter[str] | None
) -> None:
    if diagnostics is None:
        return
    seen: set[tuple[str, str]] = set()
    for url in urls:
        if extract_job_id(url):
            continue
        reason = _rejection_reason(url)
        if not reason:
            continue
        key = (reason, normalize_url(url) or url)
        if key not in seen:
            diagnostics[reason] += 1
            seen.add(key)


def _html_candidates(
    html: str, diagnostics: Counter[str] | None
) -> dict[str, list[tuple[str, list[str]]]]:
    candidates: dict[str, list[tuple[str, list[str]]]] = defaultdict(list)
    soup = BeautifulSoup(html or "", "html.parser")
    anchors = list(soup.find_all("a", href=True))
    _record_rejections((unescape(a["href"]) for a in anchors), diagnostics)
    for anchor in anchors:
        url = unescape(anchor["href"])
        job_id = extract_job_id(url)
        if not job_id:
            continue
        candidates[job_id].append(
            (url, _clean_lines(anchor.get_text("\n", strip=True).splitlines()))
        )
    return candidates


def _markdown_candidates(
    text: str, diagnostics: Counter[str] | None
) -> dict[str, list[tuple[str, list[str]]]]:
    candidates: dict[str, list[tuple[str, list[str]]]] = defaultdict(list)
    links = [(label, unescape(url)) for label, url in MARKDOWN_LINK_RE.findall(text or "")]
    _record_rejections((url for _, url in links), diagnostics)
    for label, url in links:
        job_id = extract_job_id(url)
        if job_id:
            candidates[job_id].append((url, _clean_lines(label)))
    return candidates


def parse_alert_message(
    message: dict, diagnostics: Counter[str] | None = None
) -> list[ParsedJobAlert]:
    headers = {
        header["name"]: header["value"]
        for header in message.get("payload", {}).get("headers", [])
    }
    html = message.get("html") or message.get("body_html") or ""
    text = (
        message.get("text")
        or message.get("body_text")
        or message.get("body")
        or ""
    )
    candidates = _html_candidates(html, diagnostics) if html else {}
    markdown = _markdown_candidates(text, diagnostics)
    for job_id, items in markdown.items():
        candidates.setdefault(job_id, []).extend(items)

    jobs: list[ParsedJobAlert] = []
    for job_id, items in candidates.items():
        valid = [
            (url, fields)
            for url, lines in items
            if (fields := _listing_fields(lines)) is not None
        ]
        if not valid:
            if diagnostics is not None:
                diagnostics["malformed_listing"] += 1
            continue
        if diagnostics is not None and len(items) > 1:
            diagnostics["duplicate_job_link"] += len(items) - 1
        source_url, (title, company, location) = valid[0]
        canonical = normalize_url(source_url)
        if not canonical:
            if diagnostics is not None:
                diagnostics["invalid_url"] += 1
            continue
        jobs.append(
            ParsedJobAlert(
                title=title,
                company=company,
                location=location,
                source_url=source_url,
                canonical_job_url=canonical,
                job_identifier=job_id,
                alert_timestamp=_timestamp(headers),
                gmail_message_id=message["id"],
            )
        )
    return jobs
