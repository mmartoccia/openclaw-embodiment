"""OpenClaw context API client with retry and auth support."""

from typing import List, Optional

import httpx

from ..core.exceptions import ContextAuthError, ContextNetworkError, ContextRateLimitError, ContextServiceUnavailableError
from .models import ContextPayload, MemoryChunk


class ContextClient:
    """REST client for `/v1/wearable/context/query`."""

    def __init__(self, base_url: str = "http://localhost:8080", token: Optional[str] = None, timeout_ms: int = 1000) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout_ms / 1000.0

    def _headers(self) -> dict:
        headers = {"content-type": "application/json"}
        if self.token:
            headers["authorization"] = "Bearer %s" % self.token
        return headers

    def query(self, payload: ContextPayload, max_chunks: int = 5, max_total_chars: int = 2048) -> List[MemoryChunk]:
        """Query context service and return ranked memory chunks."""
        req = {
            "device_id": payload.device_id,
            "timestamp": payload.timestamp_epoch,
            "gate_confidence": payload.scene_gate_confidence / 32767.0 if payload.scene_gate_confidence else 0.0,
            "max_chunks": max(1, min(10, max_chunks)),
            "max_total_chars": max_total_chars,
        }
        url = "%s/v1/wearable/context/query" % self.base_url
        for attempt in (0, 1):
            try:
                resp = httpx.post(url, json=req, headers=self._headers(), timeout=self.timeout)
            except httpx.HTTPError as exc:
                if attempt == 0:
                    continue
                raise ContextNetworkError(str(exc), "CTX_NETWORK_ERROR", "Verify node API availability")
            if resp.status_code == 200:
                data = resp.json()
                chunks = []
                for item in data.get("chunks", []):
                    chunks.append(MemoryChunk(
                        chunk_id=item.get("chunk_id", ""),
                        source=item.get("source", "unknown"),
                        content=item.get("content", ""),
                        relevance_score=float(item.get("relevance_score", 0.0)),
                        timestamp_epoch=int(item.get("timestamp", 0)),
                        metadata=item.get("metadata", {}),
                    ))
                return chunks
            if resp.status_code == 401:
                raise ContextAuthError("unauthorized", "CTX_AUTH_FAILED", "Check wearable API token")
            if resp.status_code == 429:
                raise ContextRateLimitError("rate limited", "CTX_RATE_LIMITED", "Back off and retry later")
            if resp.status_code == 503:
                raise ContextServiceUnavailableError("service unavailable", "CTX_SERVICE_UNAVAILABLE", "Retry once service recovers")
            if attempt == 1:
                raise ContextNetworkError("context query failed", "CTX_NETWORK_ERROR", "Inspect API logs")
        return []

    def health_check(self) -> bool:
        """Return True when API health endpoint responds OK."""
        try:
            resp = httpx.get("%s/health" % self.base_url, timeout=self.timeout)
            return resp.status_code == 200
        except httpx.HTTPError:
            return False
