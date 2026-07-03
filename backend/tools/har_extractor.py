"""Discover Variational's (undocumented) backend order API from a browser HAR.

Variational Omni does not publish a trading API, but its web client talks to a
JSON backend. Instead of guessing endpoints, we capture a *real* order flow in
the browser — the same flow the bot was previously driving by clicking — and
read the exact requests back out:

    1. Open the Omni app in Chrome, open DevTools -> Network.
    2. Tick "Preserve log". Optionally filter to Fetch/XHR.
    3. Place (or start to place) one small order so auth + RFQ + submit all fire.
    4. Right-click the request list -> "Save all as HAR with content".
    5. Run:  python -m tools.har_extractor capture.har -o config/variational_endpoints.json

The output is an *endpoint map*: for each interesting call it records the
method, URL template, which headers are required (secrets redacted), and a
JSON body template with values replaced by typed placeholders. The connector
(bot/exchanges/variational.py) loads this map so the payload shape always
matches what the live frontend actually sends.

This module has no third-party dependencies so it runs anywhere Python 3.10+ is
available.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional
from urllib.parse import urlparse

# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------

# Path keyword -> logical action. Order matters: first match wins, so put the
# most specific keywords first.
CATEGORY_KEYWORDS: list[tuple[str, tuple[str, ...]]] = [
    ("auth_nonce", ("nonce", "challenge", "siwe/init", "auth/init")),
    ("auth_login", ("login", "session", "verify", "siwe", "authenticate", "sign-in", "signin")),
    ("rfq", ("rfq", "quote", "price-request", "request-quote")),
    ("order_submit", ("order", "trade", "submit", "execute", "fill", "accept")),
    ("position", ("position", "portfolio", "account", "margin", "balance")),
    ("market_data", ("market", "statistics", "ticker", "orderbook", "funding", "listing")),
]

# Header names we must surface (the connector needs to replay them) but whose
# values are secret and must never be written to disk.
SENSITIVE_HEADERS = {"authorization", "cookie", "x-api-key", "x-auth-token", "x-session-token"}

# Header names that are pure noise for replay.
IGNORED_HEADERS = {
    "accept", "accept-encoding", "accept-language", "connection", "host",
    "user-agent", "referer", "origin", "sec-fetch-dest", "sec-fetch-mode",
    "sec-fetch-site", "sec-ch-ua", "sec-ch-ua-mobile", "sec-ch-ua-platform",
    "content-length", "pragma", "cache-control", "dnt", "priority", "te",
}


# Pure noise: CDN challenge machinery and static assets whose names happen to
# contain keywords (e.g. TradesTable.css matching "trade").
NOISE_PATH_PREFIXES = ("/cdn-cgi/", "/_app/", "/static/", "/assets/")
NOISE_EXTENSIONS = (".js", ".css", ".map", ".png", ".jpg", ".svg", ".ico",
                    ".woff", ".woff2", ".ttf", ".html")


def classify(path: str) -> Optional[str]:
    """Map a URL path to a logical action, or None if it looks uninteresting."""
    low = path.lower()
    if low.startswith(NOISE_PATH_PREFIXES) or low.endswith(NOISE_EXTENSIONS):
        return None
    for category, keywords in CATEGORY_KEYWORDS:
        if any(kw in low for kw in keywords):
            return category
    return None


# ---------------------------------------------------------------------------
# Body templating
# ---------------------------------------------------------------------------

def _placeholder(value: Any) -> Any:
    """Return a typed placeholder describing ``value`` without leaking secrets."""
    if isinstance(value, bool):
        return "<bool>"
    if isinstance(value, int):
        return "<int>"
    if isinstance(value, float):
        return "<float>"
    if isinstance(value, str):
        # 0x-prefixed long hex is almost certainly a signature/address/hash.
        if value.startswith("0x") and len(value) >= 40:
            return "<hex:signature-or-address>"
        return "<str>"
    if value is None:
        return None
    return "<value>"


def templatize(obj: Any) -> Any:
    """Recursively replace leaf values with typed placeholders, keeping shape."""
    if isinstance(obj, dict):
        return {k: templatize(v) for k, v in obj.items()}
    if isinstance(obj, list):
        # Keep one representative element so the shape is clear.
        return [templatize(obj[0])] if obj else []
    return _placeholder(obj)


def _parse_body(post_data: Optional[dict]) -> tuple[Optional[Any], Optional[Any]]:
    """Return (raw_parsed_body, templated_body) from a HAR request.postData."""
    if not post_data:
        return None, None
    text = post_data.get("text")
    mime = (post_data.get("mimeType") or "").lower()
    if text and ("json" in mime or text.lstrip().startswith(("{", "["))):
        try:
            parsed = json.loads(text)
            return parsed, templatize(parsed)
        except (json.JSONDecodeError, ValueError):
            pass
    # Form-encoded params, if present.
    params = post_data.get("params")
    if params:
        shape = {p.get("name"): "<str>" for p in params if p.get("name")}
        return params, shape
    return text, "<raw-body>" if text else None


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------

@dataclass
class Endpoint:
    category: str
    method: str
    url: str
    path: str
    query_keys: list[str] = field(default_factory=list)
    required_headers: list[str] = field(default_factory=list)
    sensitive_headers: list[str] = field(default_factory=list)
    body_template: Any = None
    response_status: Optional[int] = None
    response_sample: Any = None

    def to_map_entry(self) -> dict[str, Any]:
        return {
            "method": self.method,
            "url": self.url,
            "path": self.path,
            "query_keys": self.query_keys,
            "required_headers": self.required_headers,
            "sensitive_headers": self.sensitive_headers,
            "body_template": self.body_template,
            "response_status": self.response_status,
        }


def _header_names(headers: Iterable[dict]) -> tuple[list[str], list[str]]:
    """Split HAR headers into (required-to-replay, sensitive) name lists."""
    required, sensitive = [], []
    for h in headers:
        name = (h.get("name") or "").lower()
        if not name or name.startswith(":") or name in IGNORED_HEADERS:
            continue
        if name in SENSITIVE_HEADERS:
            sensitive.append(name)
        else:
            required.append(name)
    return sorted(set(required)), sorted(set(sensitive))


def _response_sample(response: dict) -> tuple[Optional[int], Any]:
    status = response.get("status")
    content = response.get("content") or {}
    text = content.get("text")
    if not text:
        return status, None
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return status, None
    # Keep only the top-level shape of the response so we don't persist balances.
    return status, templatize(parsed)


def extract(har: dict, host_filter: Optional[str] = None) -> list[Endpoint]:
    """Pull interesting endpoints out of a parsed HAR document."""
    entries = (har.get("log") or {}).get("entries") or []
    found: dict[tuple[str, str], Endpoint] = {}

    for entry in entries:
        req = entry.get("request") or {}
        method = (req.get("method") or "GET").upper()
        url = req.get("url") or ""
        if not url:
            continue
        parsed = urlparse(url)
        host, path = parsed.netloc, parsed.path
        if host_filter and host_filter not in host:
            continue
        category = classify(path)
        if category is None:
            continue

        required, sensitive = _header_names(req.get("headers") or [])
        _, body_template = _parse_body(req.get("postData"))
        status, resp_sample = _response_sample(entry.get("response") or {})
        query_keys = sorted({q.get("name") for q in (req.get("queryString") or []) if q.get("name")})

        # De-dup on (method, path); prefer the entry that carried a body.
        key = (method, path)
        endpoint = Endpoint(
            category=category,
            method=method,
            url=f"{parsed.scheme}://{host}{path}",
            path=path,
            query_keys=list(query_keys),
            required_headers=required,
            sensitive_headers=sensitive,
            body_template=body_template,
            response_status=status,
            response_sample=resp_sample,
        )
        existing = found.get(key)
        if existing is None or (body_template is not None and existing.body_template is None):
            found[key] = endpoint

    # Stable, human-friendly ordering by logical flow.
    order = [c for c, _ in CATEGORY_KEYWORDS]
    return sorted(found.values(), key=lambda e: (order.index(e.category), e.path))


def build_endpoint_map(endpoints: list[Endpoint], host: Optional[str]) -> dict[str, Any]:
    """Group endpoints by category into the map the connector consumes."""
    by_category: dict[str, list[dict[str, Any]]] = {}
    for ep in endpoints:
        by_category.setdefault(ep.category, []).append(ep.to_map_entry())
    return {
        "_note": (
            "Auto-generated by tools/har_extractor.py from a browser capture. "
            "Values are templated placeholders; secrets are redacted. Review and "
            "trim before wiring into bot/exchanges/variational.py."
        ),
        "host": host,
        "endpoints": by_category,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _print_summary(endpoints: list[Endpoint], out: "Any" = sys.stdout) -> None:
    if not endpoints:
        print("No matching endpoints found. Check the host filter and that the "
              "HAR was saved 'with content'.", file=out)
        return
    print(f"Discovered {len(endpoints)} endpoint(s):\n", file=out)
    for ep in endpoints:
        print(f"  [{ep.category}] {ep.method} {ep.path}", file=out)
        if ep.query_keys:
            print(f"       query:   {', '.join(ep.query_keys)}", file=out)
        if ep.sensitive_headers:
            print(f"       auth:    {', '.join(ep.sensitive_headers)} (redacted)", file=out)
        if ep.body_template is not None:
            body = json.dumps(ep.body_template, indent=8)[:600]
            print(f"       body:    {body}", file=out)
        print(f"       status:  {ep.response_status}", file=out)
        print(file=out)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Extract Variational backend endpoints from a browser HAR capture.",
    )
    parser.add_argument("har", help="Path to the .har file exported from DevTools.")
    parser.add_argument(
        "-o", "--out",
        help="Write the endpoint map JSON here (e.g. config/variational_endpoints.json).",
    )
    parser.add_argument(
        "--host",
        default="variational.io",
        help="Only keep requests whose host contains this substring "
             "(default: variational.io; use '' to keep all).",
    )
    args = parser.parse_args(argv)

    try:
        with open(args.har, "r", encoding="utf-8") as fh:
            har = json.load(fh)
    except FileNotFoundError:
        print(f"HAR file not found: {args.har}", file=sys.stderr)
        return 2
    except json.JSONDecodeError as exc:
        print(f"Not a valid HAR/JSON file: {exc}", file=sys.stderr)
        return 2

    host_filter = args.host or None
    endpoints = extract(har, host_filter=host_filter)
    _print_summary(endpoints)

    if args.out:
        host = urlparse(endpoints[0].url).netloc if endpoints else host_filter
        endpoint_map = build_endpoint_map(endpoints, host)
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(endpoint_map, fh, indent=2)
            fh.write("\n")
        print(f"Wrote endpoint map -> {args.out}")

    return 0 if endpoints else 1


if __name__ == "__main__":
    raise SystemExit(main())
