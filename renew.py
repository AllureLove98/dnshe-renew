#!/usr/bin/env python3
"""Renew DNSHE free subdomains and optionally send a Bark notification."""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


API_URL = "https://api005.dnshe.com/index.php?m=domain_hub"
TIMEOUT_SECONDS = 30


@dataclass
class RenewalResult:
    domain: str
    outcome: str
    detail: str


def request_json(method: str, url: str, api_key: str | None = None,
                 api_secret: str | None = None, body: dict[str, Any] | None = None) -> dict[str, Any]:
    headers = {"Accept": "application/json"}
    data = None
    if api_key and api_secret:
        headers.update({"X-API-Key": api_key, "X-API-Secret": api_secret})
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body).encode("utf-8")

    request = Request(url, data=data, headers=headers, method=method)
    try:
        with urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            raw = response.read().decode("utf-8")
    except HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            payload = {}
        payload.setdefault("success", False)
        payload.setdefault("message", f"HTTP {exc.code}: {raw[:300]}")
        return payload
    except URLError as exc:
        return {"success": False, "message": f"Network error: {exc.reason}"}

    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {"success": False, "message": f"Invalid JSON response: {raw[:300]}"}


def list_subdomains(api_key: str, api_secret: str) -> list[dict[str, Any]]:
    all_domains: list[dict[str, Any]] = []
    page = 1
    while True:
        params = {
            "endpoint": "subdomains", "action": "list", "page": page,
            "per_page": 500, "fields": "id,full_domain,status,expires_at,never_expires",
        }
        response = request_json("GET", f"{API_URL}&{urlencode(params)}", api_key, api_secret)
        if not response.get("success"):
            raise RuntimeError(response.get("message", "Unable to list subdomains"))
        all_domains.extend(response.get("subdomains", []))
        if not response.get("pagination", {}).get("has_more"):
            return all_domains
        page += 1


def configured_domains() -> set[str]:
    """Return an optional comma/newline-separated domain allowlist."""
    value = os.environ.get("DNSHE_DOMAINS", "").strip()
    return {item.strip().lower() for item in value.replace("\n", ",").split(",") if item.strip()}


def renew_domains(api_key: str, api_secret: str) -> list[RenewalResult]:
    allowlist = configured_domains()
    domains = list_subdomains(api_key, api_secret)
    if allowlist:
        domains = [d for d in domains if str(d.get("full_domain", "")).lower() in allowlist]
        found = {str(d.get("full_domain", "")).lower() for d in domains}
        missing = allowlist - found
        results = [RenewalResult(domain, "failed", "Domain not found in this API account") for domain in sorted(missing)]
    else:
        results = []

    for domain in domains:
        name = str(domain.get("full_domain") or domain.get("id"))
        if domain.get("never_expires"):
            results.append(RenewalResult(name, "skipped", "Never expires"))
            continue
        response = request_json(
            "POST", f"{API_URL}&endpoint=subdomains&action=renew", api_key, api_secret,
            {"subdomain_id": domain["id"]},
        )
        message = str(response.get("message", "No message"))
        if response.get("success"):
            expiry = response.get("new_expires_at")
            results.append(RenewalResult(name, "renewed", f"{message}" + (f"; expires {expiry}" if expiry else "")))
        elif response.get("error_code") == "renewal_not_yet_available" or "not yet available" in message.lower():
            results.append(RenewalResult(name, "not_due", message))
        else:
            results.append(RenewalResult(name, "failed", message))
    return results


def notify_bark(bark_url: str, title: str, body: str, level: str) -> None:
    # Bark's endpoint accepts a JSON POST. A full endpoint URL keeps self-hosted Bark compatible.
    response = request_json("POST", bark_url.rstrip("/"), body={
        "title": title, "body": body, "group": "DNSHE", "level": level,
    })
    if not response.get("code", 200) == 200 and response.get("success") is False:
        raise RuntimeError(response.get("message", "Bark notification failed"))


def main() -> int:
    api_key = os.environ.get("DNSHE_API_KEY")
    api_secret = os.environ.get("DNSHE_API_SECRET")
    bark_url = os.environ.get("BARK_URL", "").strip()
    if not api_key or not api_secret:
        print("DNSHE_API_KEY and DNSHE_API_SECRET must be set.", file=sys.stderr)
        return 2

    try:
        results = renew_domains(api_key, api_secret)
    except RuntimeError as exc:
        summary = f"DNSHE renewal failed before processing domains: {exc}"
        print(summary, file=sys.stderr)
        if bark_url:
            try:
                notify_bark(bark_url, "DNSHE renewal failed", summary, "critical")
            except RuntimeError as bark_exc:
                print(f"Bark notification failed: {bark_exc}", file=sys.stderr)
        return 1

    counts = {outcome: sum(r.outcome == outcome for r in results) for outcome in ("renewed", "not_due", "skipped", "failed")}
    lines = [f"{r.domain}: {r.outcome} — {r.detail}" for r in results] or ["No matching subdomains."]
    summary = "DNSHE renewal: " + ", ".join(f"{k}={v}" for k, v in counts.items()) + "\n" + "\n".join(lines)
    print(summary)

    failed = counts["failed"] > 0
    if bark_url:
        try:
            notify_bark(bark_url, "DNSHE renewal " + ("failed" if failed else "completed"), summary, "critical" if failed else "active")
        except RuntimeError as exc:
            print(f"Bark notification failed: {exc}", file=sys.stderr)
            failed = True
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
