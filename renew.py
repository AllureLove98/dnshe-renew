#!/usr/bin/env python3
"""自动续期 DNSHE 免费子域名，并可选地推送 Bark 通知。"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone
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
        payload.setdefault("message", f"HTTP 请求失败（状态码 {exc.code}）：{raw[:300]}")
        return payload
    except URLError as exc:
        return {"success": False, "message": f"网络请求失败：{exc.reason}"}

    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {"success": False, "message": f"接口返回了无效 JSON：{raw[:300]}"}


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
            raise RuntimeError(response.get("message", "无法获取子域名列表"))
        all_domains.extend(response.get("subdomains", []))
        if not response.get("pagination", {}).get("has_more"):
            return all_domains
        page += 1


def configured_domains() -> set[str]:
    """读取可选的域名白名单，支持逗号或换行分隔。"""
    value = os.environ.get("DNSHE_DOMAINS", "").strip()
    return {item.strip().lower() for item in value.replace("\n", ",").split(",") if item.strip()}


def renew_domains(api_key: str, api_secret: str) -> list[RenewalResult]:
    allowlist = configured_domains()
    domains = list_subdomains(api_key, api_secret)
    if allowlist:
        domains = [d for d in domains if str(d.get("full_domain", "")).lower() in allowlist]
        found = {str(d.get("full_domain", "")).lower() for d in domains}
        missing = allowlist - found
        results = [RenewalResult(domain, "failed", "当前 DNSHE API Key 名下未找到该域名") for domain in sorted(missing)]
    else:
        results = []

    for domain in domains:
        name = str(domain.get("full_domain") or domain.get("id"))
        if domain.get("never_expires"):
            results.append(RenewalResult(name, "skipped", "该域名设置为永不过期，无需续期"))
            continue
        response = request_json(
            "POST", f"{API_URL}&endpoint=subdomains&action=renew", api_key, api_secret,
            {"subdomain_id": domain["id"]},
        )
        message = str(response.get("message", "接口未返回说明信息"))
        if response.get("success"):
            expiry = response.get("new_expires_at")
            detail = "DNSHE 接口续期成功"
            if expiry:
                detail += f"，新到期时间：{expiry}"
            results.append(RenewalResult(name, "renewed", detail))
        elif response.get("error_code") == "renewal_not_yet_available" or "not yet available" in message.lower():
            results.append(RenewalResult(name, "not_due", message))
        else:
            results.append(RenewalResult(name, "failed", message))
    return results


def notify_bark(bark_url: str, title: str, body: str, level: str) -> None:
    # Bark 接口接受 JSON POST；使用完整地址也兼容自建 Bark 服务。
    response = request_json("POST", bark_url.rstrip("/"), body={
        "title": title, "body": body, "group": "DNSHE", "level": level,
    })
    if not response.get("code", 200) == 200 and response.get("success") is False:
        raise RuntimeError(response.get("message", "Bark 推送失败"))


def main() -> int:
    api_key = os.environ.get("DNSHE_API_KEY")
    api_secret = os.environ.get("DNSHE_API_SECRET")
    bark_url = os.environ.get("BARK_URL", "").strip()
    if not api_key or not api_secret:
        print("必须设置 DNSHE_API_KEY 和 DNSHE_API_SECRET。", file=sys.stderr)
        return 2

    try:
        results = renew_domains(api_key, api_secret)
    except RuntimeError as exc:
        summary = f"DNSHE 域名续期任务在处理域名之前失败：{exc}"
        print(summary, file=sys.stderr)
        if bark_url:
            try:
                notify_bark(bark_url, "DNSHE 域名续期失败", summary, "critical")
            except RuntimeError as bark_exc:
                print(f"Bark 推送失败：{bark_exc}", file=sys.stderr)
        return 1

    counts = {outcome: sum(r.outcome == outcome for r in results) for outcome in ("renewed", "not_due", "skipped", "failed")}
    labels = {
        "renewed": "续期成功", "not_due": "暂未开放续期", "skipped": "已跳过", "failed": "续期失败",
    }
    scope = ", ".join(sorted(configured_domains())) if configured_domains() else "当前 API Key 名下全部子域名"
    beijing_tz = timezone(timedelta(hours=8), name="北京时间")
    executed_at = datetime.now(timezone.utc).astimezone(beijing_tz).strftime("%Y-%m-%d %H:%M:%S %Z")
    lines = [f"• {r.domain}\n  结果：{labels[r.outcome]}\n  详情：{r.detail}" for r in results]
    if not lines:
        lines = ["未找到符合条件的子域名，未执行续期。"]
    summary = (
        "DNSHE 域名自动续期执行报告\n"
        f"执行时间：{executed_at}\n"
        f"处理范围：{scope}\n"
        "统计："
        + "，".join(f"{labels[key]} {counts[key]} 个" for key in counts)
        + "\n\n域名明细：\n"
        + "\n".join(lines)
    )
    print(summary)

    failed = counts["failed"] > 0
    if bark_url:
        try:
            notify_bark(bark_url, "DNSHE 域名续期" + ("失败" if failed else "完成"), summary, "critical" if failed else "active")
        except RuntimeError as exc:
            print(f"Bark 推送失败：{exc}", file=sys.stderr)
            failed = True
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
