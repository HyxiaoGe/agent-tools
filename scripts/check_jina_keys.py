"""逐个检查 Jina 号池 key 是否可用：查账户余额（不消耗额度），只输出序号和余额，不输出 key。

余额按账号算，同账号的多个 key 会显示同一个 account 指纹，放进号池没有意义。
用法（容器内）：python scripts/check_jina_keys.py
"""

from __future__ import annotations

import hashlib
import os
import sys

import httpx

JINA_BALANCE_URL = "https://embeddings-dashboard-api.jina.ai/api/v1/api_key/fe_user"


def _keys() -> list[str]:
    raw = os.getenv("JINA_API_KEYS", os.getenv("JINA_API_KEY", ""))
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
    payload = payload if isinstance(payload, dict) else {}
    wallet = payload.get("wallet") if isinstance(payload.get("wallet"), dict) else {}
    balance = wallet.get("total_balance")
    account = hashlib.sha256(str(payload.get("user_id")).encode()).hexdigest()[:8]
    usable = not (isinstance(balance, (int, float)) and balance <= 0)
    return usable, (
        f"key#{index} {'ok' if usable else 'exhausted'} balance={balance} account={account}"
    )


def main() -> int:
    keys = _keys()
    if not keys:
        print("no jina keys configured", file=sys.stderr)
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
                response = client.get(JINA_BALANCE_URL, params={"api_key": key})
            except httpx.HTTPError:
                response = None
            usable, line = _describe(index, response)
            usable_count += usable
            print(line)
    print(f"usable={usable_count}/{len(keys)}")
    return 0 if usable_count else 1


if __name__ == "__main__":
    raise SystemExit(main())
