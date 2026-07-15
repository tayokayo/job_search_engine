from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html import unescape
from typing import Iterable

from bs4 import BeautifulSoup

from .url_utils import normalize_url

LINKEDIN_JOB_RE = re.compile(r"linkedin\.com/(?:comm/)?jobs/view/[^\s\"'<>]+|linkedin\.com/jobs/collections/[^\s\"'<>]+", re.I)
JOB_ID_RE = re.compile(r"(?:currentJobId|jobId)=(\d+)|jobs/view/(\d+)")


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
    return "\n".join(line.strip() for line in soup.get_text("\n").splitlines() if line.strip())


def extract_links(html: str, text: str = "") -> list[str]:
    links: list[str] = []
    soup = BeautifulSoup(html or "", "html.parser")
    for a in soup.find_all("a", href=True):
        href = unescape(a["href"])
        if "linkedin.com" in href and "/jobs/" in href:
            links.append(href)
    links.extend(match.group(0) for match in LINKEDIN_JOB_RE.finditer(text or html or ""))
    unique = {}
    for link in links:
        unique.setdefault(normalize_url(link) or link, link)
    return list(unique.values())


def extract_job_id(url: str | None) -> str | None:
    if not url:
        return None
    match = JOB_ID_RE.search(url)
    if not match:
        return None
    return next(group for group in match.groups() if group)


def _timestamp(headers: dict[str, str]) -> datetime:
    raw = headers.get("Date") or headers.get("date")
    if raw:
        try:
            return parsedate_to_datetime(raw).astimezone(timezone.utc)
        except Exception:
            pass
    return datetime.now(timezone.utc)


def parse_alert_message(message: dict) -> list[ParsedJobAlert]:
    headers = {h["name"]: h["value"] for h in message.get("payload", {}).get("headers", [])}
    html = message.get("html", "")
    text = message.get("text") or html_to_text(html)
    links = extract_links(html, text)
    lines = [line.strip(" •\t") for line in text.splitlines() if line.strip()]
    jobs: list[ParsedJobAlert] = []
    for index, link in enumerate(links):
        job_id = extract_job_id(link)
        canonical = normalize_url(link)
        title = company = location = "Unknown"
        # LinkedIn alert snippets commonly show title/company/location in nearby lines.
        for i, line in enumerate(lines):
            if job_id and job_id in line or (link in line):
                window = lines[max(0, i - 4): i + 5]
                title, company, location = _infer_triplet(window)
                break
        if title == "Unknown" and index * 3 + 2 < len(lines):
            title, company, location = _infer_triplet(lines[index * 3:index * 3 + 5])
        jobs.append(ParsedJobAlert(title, company, location, link, canonical, job_id or canonical or f"message:{message['id']}:{index}", _timestamp(headers), message["id"]))
    return jobs


def _infer_triplet(lines: Iterable[str]) -> tuple[str, str, str]:
    candidates = [l for l in lines if not l.lower().startswith(("view", "apply", "see", "jobs", "linkedin"))]
    title = candidates[0] if len(candidates) > 0 else "Unknown"
    company = candidates[1] if len(candidates) > 1 else "Unknown"
    location = candidates[2] if len(candidates) > 2 else "Unknown"
    return title[:200], company[:200], location[:200]
