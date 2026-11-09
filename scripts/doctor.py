"""Read-only model connectivity check; print no keys or response/error bodies."""

import asyncio
import json
import os
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from dotenv import load_dotenv


async def main():
    load_dotenv(Path(__file__).resolve().parents[1] / ".env", encoding="utf-8-sig")
    base = os.getenv("MODEL_BASE_URL") or os.getenv("LLM_BASE_URL")
    key = os.getenv("MODEL_API_KEY") or os.getenv("LLM_API_KEY")
    if not base or not key:
        raise SystemExit("Missing model URL or key")
    rows = []
    for trust_env in [True, False]:
        row = {"host": urlsplit(base).hostname, "trust_env": trust_env}
        try:
            async with httpx.AsyncClient(
                timeout=12, trust_env=trust_env, follow_redirects=False
            ) as client:
                response = await client.get(
                    base.rstrip("/") + "/models", headers={"Authorization": f"Bearer {key}"}
                )
                row["http_status"] = response.status_code
                if response.status_code == 200:
                    row["models"] = [m["id"] for m in response.json().get("data", [])]
        except Exception as exc:
            chain = []
            while exc is not None and len(chain) < 5:
                chain.append(type(exc).__name__)
                exc = exc.__cause__
            row["error_chain"] = chain
        rows.append(row)
    print(json.dumps(rows, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
