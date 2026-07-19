from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit, unquote

TRACKING_PREFIXES = ("utm_", "trk", "mc_", "ga_")
TRACKING_PARAMS = {
    "authtoken",
    "campaign",
    "ebp",
    "eid",
    "fbclid",
    "gclid",
    "li_fat_id",
    "lipi",
    "mcid",
    "midsig",
    "midtoken",
    "otptoken",
    "refid",
    "session",
    "sig",
    "signature",
    "source",
    "src",
    "token",
    "trackingid",
}
LINKEDIN_JOB_PATH_RE = re.compile(r"/(?:comm/)?jobs/view/(\d+)(?:[/?#]|$)", re.I)
LINKEDIN_JOB_QUERY_KEYS = {"currentjobid", "jobid"}


def _linkedin_job_id(netloc: str, path: str, query: str) -> str | None:
    hostname = netloc.rsplit("@", 1)[-1].split(":", 1)[0]
    if hostname != "linkedin.com" and not hostname.endswith(".linkedin.com"):
        return None
    match = LINKEDIN_JOB_PATH_RE.search(path)
    if match:
        return match.group(1)
    for key, value in parse_qsl(query, keep_blank_values=True):
        if key.lower() in LINKEDIN_JOB_QUERY_KEYS and value.isdigit():
            return value
    return None


def normalize_url(url: str | None) -> str | None:
    if not url:
        return None
    url = url.strip().rstrip(")]}>,.;")
    if not url:
        return None
    if url.startswith(("linkedin.com/", "www.linkedin.com/")):
        url = "https://" + url
    parts = urlsplit(url)
    scheme = (parts.scheme or "https").lower()
    netloc = parts.netloc.lower()
    if netloc.startswith("www."):
        netloc = netloc[4:]
    path = unquote(parts.path or "")

    job_id = _linkedin_job_id(netloc, path, parts.query)
    if job_id:
        return f"https://linkedin.com/jobs/view/{job_id}"

    if path != "/":
        path = path.rstrip("/")
    query_pairs = []
    for key, value in parse_qsl(parts.query, keep_blank_values=True):
        lowered = key.lower()
        if lowered in TRACKING_PARAMS or lowered.startswith(TRACKING_PREFIXES):
            continue
        query_pairs.append((key, value))
    query = urlencode(sorted(query_pairs), doseq=True)
    return urlunsplit((scheme, netloc, path, query, ""))
