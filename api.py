from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from urllib.parse import urlencode

import aiohttp

from database import Database

EXPECTED = {"PlayerName","PlayerUID","DuoPartnerName","DuoPartnerUID","DuoLevel","DuoDays","DuoScore","DuoStatus","DuoCreationDate"}

@dataclass
class DuoResult:
    data: dict
    cached: bool = False
    latency_ms: int = 0


def validate_uid(uid: str, min_len: int, max_len: int) -> str | None:
    value = uid.strip()
    if not value or not value.isdigit() or not (min_len <= len(value) <= max_len):
        return None
    return value


def normalize(payload: object) -> dict | None:
    if not isinstance(payload, dict):
        return None
    if not (payload.keys() & EXPECTED):
        return None
    out = {}
    for key in EXPECTED:
        value = payload.get(key)
        if value is None or value == "":
            out[key] = "N/A"
        elif key in {"DuoLevel","DuoDays","DuoScore"}:
            try: out[key] = int(value)
            except (TypeError,ValueError): out[key] = "N/A"
        else:
            out[key] = str(value)
    return out

class DuoAPI:
    def __init__(self, base_url: str, db: Database, connect_timeout: float, read_timeout: float, retries: int):
        self.base_url=base_url
        self.db=db
        self.timeout=aiohttp.ClientTimeout(total=connect_timeout+read_timeout, connect=connect_timeout, sock_read=read_timeout)
        self.retries=max(0,retries)
        self.session: aiohttp.ClientSession|None=None
        self.last_success=0.0
        self.last_latency_ms=0

    async def start(self):
        self.session=aiohttp.ClientSession(timeout=self.timeout, headers={"User-Agent":"FFDuoInfoBot/1.0"})

    async def close(self):
        if self.session and not self.session.closed:
            await self.session.close()

    async def fetch(self, uid: str, cache_ttl: int) -> DuoResult:
        cached=await self.db.cache_get(uid, cache_ttl)
        if cached:
            return DuoResult(cached, True, 0)
        if not self.session:
            raise RuntimeError("API client not started")
        url=f"{self.base_url}?{urlencode({'uid':uid})}"
        last_error: Exception|None=None
        for attempt in range(self.retries+1):
            started=time.perf_counter()
            try:
                async with self.session.get(url) as resp:
                    if resp.status >= 500:
                        raise aiohttp.ClientResponseError(resp.request_info, resp.history, status=resp.status, message="upstream 5xx", headers=resp.headers)
                    if resp.status >= 400:
                        raise ValueError(f"upstream_http_{resp.status}")
                    payload=await resp.json(content_type=None)
                    data=normalize(payload)
                    if data is None:
                        raise ValueError("invalid_api_shape")
                    self.last_latency_ms=round((time.perf_counter()-started)*1000)
                    self.last_success=time.time()
                    await self.db.cache_set(uid,data)
                    return DuoResult(data,False,self.last_latency_ms)
            except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
                last_error=exc
                if attempt < self.retries:
                    await asyncio.sleep(0.35*(2**attempt))
        raise RuntimeError(str(last_error or "api_unavailable"))
