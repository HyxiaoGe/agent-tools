"""逐个检查 Firecrawl 号池 key 是否可用：调 credit-usage（不消耗额度），只输出序号和余额，不输出 key。

用法（容器内）：python scripts/check_firecrawl_keys.py
"""

from __future__ import annotations

import os
import sys

import httpx

FIRECRAWL_CREDIT_URL = "https://api.firecrawl.dev/v2/team/credit-usage"


def _keys() -> list[str]:
    raw = os.getenv("FIRECRAWL_API_KEYS", "")
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
    data = payload.get("data") if isinstance(payload, dict) else None
    data = data if isinstance(data, dict) else {}
    remaining = data.get("remainingCredits")
    usable = not (isinstance(remaining, int) and remaining <= 0)
    return usable, (
        f"key#{index} {'ok' if usable else 'exhausted'} "
        f"remaining={remaining} plan={data.get('planCredits')} "
        f"period_end={data.get('billingPeriodEnd')}"
    )


def main() -> int:
    keys = _keys()
    if not keys:
        print("no firecrawl keys configured", file=sys.stderr)
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
                    FIRECRAWL_CREDIT_URL, headers={"Authorization": f"Bearer {key}"}
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
