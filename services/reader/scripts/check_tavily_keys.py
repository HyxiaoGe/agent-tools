"""逐个检查 Tavily 号池 key 是否可用：调 /usage（不消耗额度），只输出序号和用量，不输出 key。

用法（容器内）：python scripts/check_tavily_keys.py
"""

from __future__ import annotations

import os
import sys

import httpx

TAVILY_USAGE_URL = "https://api.tavily.com/usage"


def _keys() -> list[str]:
    raw = os.getenv("TAVILY_API_KEYS", os.getenv("TAVILY_API_KEY", ""))
    return [key.strip() for key in raw.split(",") if key.strip()]


def _describe(index: int, response: httpx.Response | None) -> tuple[bool, str]:
    if response is None:
        return False, f"key#{index} unreachable"
    if response.status_code != 200:
        return False, f"key#{index} http_status={response.status_code}"
    try:
        payload = response.json()
    except ValueError:
        return False, f"key#{index} invalid_json"
    account = payload.get("account") if isinstance(payload, dict) else None
    account = account if isinstance(account, dict) else {}
    used = account.get("plan_usage")
    limit = account.get("plan_limit")
    usable = not (isinstance(used, int) and isinstance(limit, int) and used >= limit)
    return usable, (
        f"key#{index} {'ok' if usable else 'exhausted'} "
        f"plan={account.get('current_plan')} usage={used}/{limit} "
        f"paygo={account.get('paygo_usage')}/{account.get('paygo_limit')}"
    )


def main() -> int:
    keys = _keys()
    if not keys:
        print("no tavily keys configured", file=sys.stderr)
        return 1
    usable_count = 0
    seen: set[str] = set()
    with httpx.Client(timeout=15) as client:
        for index, key in enumerate(keys, start=1):
            if key in seen:
                print(f"key#{index} duplicate")
                continue
            seen.add(key)
            try:
                response = client.get(
                    TAVILY_USAGE_URL, headers={"Authorization": f"Bearer {key}"}
                )
            except httpx.HTTPError:
                response = None
            usable, line = _describe(index, response)
            usable_count += usable
            print(line)
    print(f"usable={usable_count}/{len(keys)}")
    return 0 if usable_count else 1


if __name__ == "__main__":
    raise SystemExit(main())
