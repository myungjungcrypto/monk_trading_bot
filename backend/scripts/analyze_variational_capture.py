"""Summarize Variational browser network-capture NDJSON files.

Usage:
  python -m backend.scripts.analyze_variational_capture \
    tools/variational-browser/runtime/network-captures/latest.ndjson

The script intentionally does not print captured headers. It focuses on
endpoint counts, browser request-processing windows, non-noisy POST requests,
and WebSocket frames that look related to orders or positions.
"""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse


NOISY_PATHS = {
    "/api/banner",
    "/api/candles",
    "/api/ff",
    "/api/funding/v2",
    "/api/metadata/supported_assets",
    "/api/metadata/tiers",
    "/api/metadata/v2/open_interest",
    "/api/ping",
    "/api/quotes/indicative",
    "/api/quotes/simple",
    "/api/settlement_pools/leverage",
    "/api/version",
}
IMPORTANT_PATHS = {
    "/api/auth/generate_signing_data",
    "/api/auth/login",
    "/api/auth/switch",
    "/api/orders/new/market",
    "/api/orders/tpsl",
    "/api/positions",
}
NOISY_PREFIXES = (
    "/_app/",
    "/cdn-cgi/",
)
CANDIDATE_RE = re.compile(
    r"order|trade|position|portfolio|account|perp|execute|submit|create|cancel|auth|login|tpsl",
    re.I,
)
WS_CANDIDATE_RE = re.compile(
    r"order|trade|position|execute|submit|create|cancel|buy|sell|BTC|ETH|perp",
    re.I,
)


@dataclass
class RequestContext:
    request_id: str
    action: str = ""
    batch_legs: int = 0
    started_at: str = ""
    finished_at: str = ""
    status: str = ""
    endpoint_counts: Counter = field(default_factory=Counter)
    candidates: list[dict] = field(default_factory=list)
    ws_candidates: list[dict] = field(default_factory=list)


def main() -> None:
    args = parse_args()
    path = Path(args.path).expanduser()
    if not path.exists():
        raise SystemExit(f"Capture file not found: {path}")

    summary = analyze_capture(path, sample_limit=args.sample_limit, body_chars=args.body_chars)
    print_summary(summary, path, sample_limit=args.sample_limit)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("path", help="NDJSON capture file")
    parser.add_argument("--sample-limit", type=int, default=20)
    parser.add_argument("--body-chars", type=int, default=1200)
    return parser.parse_args()


def analyze_capture(path: Path, *, sample_limit: int, body_chars: int) -> dict:
    type_counts: Counter = Counter()
    http_request_counts: Counter = Counter()
    non_noisy_request_counts: Counter = Counter()
    response_counts: Counter = Counter()
    non_noisy_post_samples: list[dict] = []
    important_request_samples: list[dict] = []
    important_response_samples: list[dict] = []
    ws_sent_samples: list[dict] = []
    contexts: dict[str, RequestContext] = {}
    total_lines = 0

    with path.open(encoding="utf-8") as file:
        for raw_line in file:
            total_lines += 1
            try:
                record = json.loads(raw_line)
            except json.JSONDecodeError:
                continue

            record_type = str(record.get("type") or "")
            type_counts[record_type] += 1
            context_id = context_request_id(record)
            ctx = get_context(contexts, record) if context_id else None

            if record_type == "request_processing_started" and ctx:
                ctx.started_at = record.get("ts", "")
                continue
            if record_type == "request_processing_finished" and ctx:
                ctx.finished_at = record.get("ts", "")
                ctx.status = str(record.get("status") or "")
                continue

            if record_type == "http_request":
                method = str(record.get("method") or "")
                key = endpoint_key(str(record.get("url") or ""), method=method)
                http_request_counts[key] += 1
                if ctx:
                    ctx.endpoint_counts[key] += 1

                if not is_noisy_url(str(record.get("url") or "")):
                    non_noisy_request_counts[key] += 1

                if is_http_candidate(record):
                    sample = request_sample(record, body_chars=body_chars)
                    if len(non_noisy_post_samples) < sample_limit:
                        non_noisy_post_samples.append(sample)
                    if ctx and len(ctx.candidates) < sample_limit:
                        ctx.candidates.append(sample)

                if is_important_url(str(record.get("url") or "")):
                    sample = request_sample(record, body_chars=body_chars)
                    if len(important_request_samples) < sample_limit:
                        important_request_samples.append(sample)

            elif record_type == "http_response":
                status = str(record.get("status") or "")
                key = endpoint_key(str(record.get("url") or ""), method=status)
                response_counts[key] += 1
                if is_important_url(str(record.get("url") or "")):
                    sample = response_sample(record, body_chars=body_chars)
                    if len(important_response_samples) < sample_limit:
                        important_response_samples.append(sample)

            elif record_type == "ws_frame_sent":
                payload = str(record.get("payload") or "")
                if WS_CANDIDATE_RE.search(payload):
                    sample = {
                        "ts": record.get("ts", ""),
                        "url": record.get("url", ""),
                        "payload": payload[:body_chars],
                    }
                    if len(ws_sent_samples) < sample_limit:
                        ws_sent_samples.append(sample)
                    if ctx and len(ctx.ws_candidates) < sample_limit:
                        ctx.ws_candidates.append(sample)

    return {
        "total_lines": total_lines,
        "type_counts": type_counts,
        "http_request_counts": http_request_counts,
        "non_noisy_request_counts": non_noisy_request_counts,
        "response_counts": response_counts,
        "non_noisy_post_samples": non_noisy_post_samples,
        "important_request_samples": important_request_samples,
        "important_response_samples": important_response_samples,
        "ws_sent_samples": ws_sent_samples,
        "contexts": contexts,
    }


def print_summary(summary: dict, path: Path, *, sample_limit: int) -> None:
    print(f"file: {path}")
    print(f"lines: {summary['total_lines']}")
    print("\n== Types ==")
    for key, count in summary["type_counts"].most_common():
        print(f"{count:8d} {key}")

    print("\n== Top HTTP Requests ==")
    for key, count in summary["http_request_counts"].most_common(30):
        print(f"{count:8d} {key}")

    print("\n== Top Non-Noisy HTTP Requests ==")
    for key, count in summary["non_noisy_request_counts"].most_common(40):
        print(f"{count:8d} {key}")

    print("\n== Request Processing Windows ==")
    for ctx in sorted(summary["contexts"].values(), key=context_sort_key):
        print(
            f"- {ctx.request_id} action={ctx.action or '-'} legs={ctx.batch_legs} "
            f"status={ctx.status or '-'} start={ctx.started_at or '-'} finish={ctx.finished_at or '-'}"
        )
        for key, count in ctx.endpoint_counts.most_common(12):
            if not endpoint_is_noisy_key(key):
                print(f"    {count:5d} {key}")
        if ctx.candidates:
            print("    candidate_http:")
            for sample in ctx.candidates[: min(sample_limit, 5)]:
                print(f"      {sample['ts']} {sample['method']} {sample['url']}")
                if sample["post_data"]:
                    print(indent(sample["post_data"], "        "))
        if ctx.ws_candidates:
            print("    candidate_ws_sent:")
            for sample in ctx.ws_candidates[: min(sample_limit, 5)]:
                print(f"      {sample['ts']} {sample['url']}")
                print(indent(sample["payload"], "        "))

    print("\n== Important HTTP Requests ==")
    for sample in summary["important_request_samples"]:
        print(f"\n--- {sample['ts']} {sample['method']} {sample['url']}")
        if sample["post_data"]:
            print(sample["post_data"])

    print("\n== Important HTTP Responses ==")
    for sample in summary["important_response_samples"]:
        print(f"\n--- {sample['ts']} {sample['status']} {sample['url']}")
        if sample["body"]:
            print(sample["body"])

    print("\n== Candidate HTTP Requests ==")
    for sample in summary["non_noisy_post_samples"]:
        print(f"\n--- {sample['ts']} {sample['method']} {sample['url']}")
        if sample["post_data"]:
            print(sample["post_data"])

    print("\n== Candidate WS Sent Frames ==")
    for sample in summary["ws_sent_samples"]:
        print(f"\n--- {sample['ts']} {sample['url']}")
        print(sample["payload"])


def get_context(contexts: dict[str, RequestContext], record: dict) -> RequestContext | None:
    context = record.get("context") or {}
    if not isinstance(context, dict):
        return None
    request_id = str(context.get("request_id") or "")
    if not request_id:
        return None
    if request_id not in contexts:
        contexts[request_id] = RequestContext(
            request_id=request_id,
            action=str(context.get("action") or ""),
            batch_legs=int(context.get("batch_legs") or 0),
        )
    return contexts[request_id]


def context_request_id(record: dict) -> str:
    context = record.get("context") or {}
    if not isinstance(context, dict):
        return ""
    return str(context.get("request_id") or "")


def is_http_candidate(record: dict) -> bool:
    method = str(record.get("method") or "").upper()
    url = str(record.get("url") or "")
    body = str(record.get("post_data") or "")
    if is_noisy_url(url):
        return False
    if method in {"POST", "PUT", "PATCH", "DELETE"}:
        return True
    return bool(CANDIDATE_RE.search(url) or CANDIDATE_RE.search(body))


def request_sample(record: dict, *, body_chars: int) -> dict:
    return {
        "ts": record.get("ts", ""),
        "method": record.get("method", ""),
        "url": record.get("url", ""),
        "post_data": str(record.get("post_data") or "")[:body_chars],
    }


def response_sample(record: dict, *, body_chars: int) -> dict:
    return {
        "ts": record.get("ts", ""),
        "status": record.get("status", ""),
        "url": record.get("url", ""),
        "body": str(record.get("body") or "")[:body_chars],
    }


def endpoint_key(url: str, *, method: str = "") -> str:
    parsed = urlparse(url)
    path = parsed.path or url.split("?", 1)[0]
    prefix = f"{method} " if method else ""
    return f"{prefix}{parsed.scheme}://{parsed.netloc}{path}" if parsed.netloc else f"{prefix}{path}"


def is_noisy_url(url: str) -> bool:
    parsed = urlparse(url)
    path = parsed.path
    if path in NOISY_PATHS:
        return True
    return any(path.startswith(prefix) for prefix in NOISY_PREFIXES)


def is_important_url(url: str) -> bool:
    return urlparse(url).path in IMPORTANT_PATHS


def endpoint_is_noisy_key(key: str) -> bool:
    parts = key.split(" ", 1)
    url = parts[-1]
    return is_noisy_url(url)


def context_sort_key(ctx: RequestContext) -> tuple:
    value = ctx.started_at or ctx.finished_at
    return (value, ctx.request_id)


def indent(value: str, prefix: str) -> str:
    return "\n".join(f"{prefix}{line}" for line in value.splitlines())


if __name__ == "__main__":
    main()
