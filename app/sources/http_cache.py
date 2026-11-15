"""Cache layer 1: raw HTTP responses saved on disk.

Historical data never changes, so a cached response never expires. This
- protects the Open-Meteo free quota (it counts long requests as many calls),
- lets the scoring be re-run during development without re-downloading,
- keeps the original responses as an audit trail ("where did this number come from?").
"""
import hashlib
import json
from pathlib import Path

import httpx
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from app.settings import settings

USER_AGENT = "weather-risk-agent/0.1 (take-home prototype)"


class SourceError(RuntimeError):
    pass


def _cache_path(source: str, url: str, params: dict) -> Path:
    # The API key is excluded from the key so paid and free calls share a cache.
    key_params = {k: v for k, v in params.items() if k != "apikey"}
    digest = hashlib.sha256(json.dumps([url, key_params], sort_keys=True).encode()).hexdigest()[:16]
    return settings.raw_cache_dir / source / f"{digest}.json"


def _is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, httpx.TransportError):
        return True
    return isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code >= 500


@retry(retry=retry_if_exception(_is_retryable), stop=stop_after_attempt(4),
       wait=wait_exponential(multiplier=1, max=20), reraise=True)
def _get(url: str, params: dict) -> dict:
    resp = httpx.get(url, params=params, timeout=60, headers={"User-Agent": USER_AGENT})
    if resp.status_code == 429 or (resp.status_code == 400 and "limit" in resp.text.lower()):
        # Rate limits are not retried: retrying burns more quota and will not succeed today.
        raise SourceError(f"{url}: rate limited ({resp.status_code}): {resp.text[:200]}")
    resp.raise_for_status()
    return resp.json()


def cached_get_json(source: str, url: str, params: dict, refresh: bool = False) -> tuple[dict, Path, bool]:
    """Return (json, cache_file, from_cache). Only successful responses are cached."""
    path = _cache_path(source, url, params)
    if path.exists() and not refresh:
        return json.loads(path.read_text(encoding="utf-8")), path, True
    data = _get(url, params)
    if isinstance(data, dict) and data.get("error"):
        raise SourceError(f"{source}: {data.get('reason', data)}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")
    return data, path, False
