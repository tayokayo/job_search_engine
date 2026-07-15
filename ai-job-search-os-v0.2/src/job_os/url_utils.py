from __future__ import annotations

from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit, unquote

TRACKING_PREFIXES = ("utm_",)
TRACKING_PARAMS = {
    "trk", "trkEmail", "midToken", "midSig", "lipi", "li_fat_id", "eBP", "eid",
    "refId", "trackingId", "mcid", "src", "source", "campaign", "fbclid", "gclid",
}


def normalize_url(url: str | None) -> str | None:
    if not url:
        return None
    url = url.strip()
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
    if path != "/":
        path = path.rstrip("/")
    query_pairs = []
    for key, value in parse_qsl(parts.query, keep_blank_values=True):
        if key in TRACKING_PARAMS or key.startswith(TRACKING_PREFIXES):
            continue
        query_pairs.append((key, value))
    query = urlencode(sorted(query_pairs), doseq=True)
    return urlunsplit((scheme, netloc, path, query, ""))
