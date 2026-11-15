"""Shared shard proxy authentication and availability check."""
from __future__ import annotations

import json
import os
import urllib.request


def key() -> str | None:
    return os.environ.get("CPOOL_SHARD_KEY") or os.environ.get("CLI_PROXY_API_KEY") or None


def relay_supports_shard(base_url: str, timeout: float = 1.0) -> bool:
    try:
        request = urllib.request.Request(base_url.rstrip("/") + "/_relay/health")
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status == 200 and bool(json.load(response).get("shard"))
    except Exception:
        return False


def probe(url: str, key: str | None, timeout: float = 2.0) -> bool:
    try:
        request = urllib.request.Request(
            url.rstrip("/") + "/v1/models",
            headers={"x-api-key": key} if key is not None else {},
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status == 200
    except Exception:
        return False
