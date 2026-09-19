"""Shared HTTP client for the Bhashini (Dhruva) pipeline inference endpoint.

Used by both the Hindi ASR and TTS providers. Authenticates with the raw
inference API key in the `Authorization` header (no "Bearer" prefix — that
is the request shape verified against the live endpoint).

Error messages never contain the API key, request headers, or the request
body (which carries base64 caller audio): only the HTTP status, a short
truncated excerpt of the server's error body, and a scrub pass as a final
safety net in case the server ever echoes the key back.
"""

from typing import Any

import httpx

DEFAULT_INFERENCE_URL = "https://dhruva-api.bhashini.gov.in/services/inference/pipeline"
_MAX_ERROR_DETAIL_CHARS = 200


class BhashiniError(Exception):
    """Any failure talking to Bhashini. `status_code` is None for transport
    errors (timeout, connection failure) and for malformed responses."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


_STATUS_MEANING = {
    400: "bad request",
    401: "authentication failed — check BHASHINI_INFERENCE_API_KEY",
    403: "access forbidden for this key/service",
    404: "endpoint or service not found",
    422: "request payload rejected",
    429: "rate limited",
}


class BhashiniClient:
    def __init__(
        self,
        *,
        api_key: str,
        inference_url: str = DEFAULT_INFERENCE_URL,
        timeout_seconds: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
        keepalive_expiry_seconds: float = 120.0,
    ) -> None:
        if not api_key:
            raise BhashiniError("BHASHINI_INFERENCE_API_KEY is not set")
        self._api_key = api_key
        self._inference_url = inference_url
        self._timeout_seconds = timeout_seconds
        self._transport = transport
        self._keepalive_expiry_seconds = keepalive_expiry_seconds
        self._http: httpx.AsyncClient | None = None

    def _get_http(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(
                timeout=self._timeout_seconds,
                transport=self._transport,
                # Reuse the connection between turns instead of re-doing DNS,
                # TCP and TLS for every request.
                limits=httpx.Limits(keepalive_expiry=self._keepalive_expiry_seconds),
            )
        return self._http

    async def warm_up(self) -> None:
        """Opens the connection (DNS, TCP, TLS) before the first real request,
        so the caller's first question does not pay for it. Best effort: the
        response is irrelevant and nothing here may ever raise."""
        try:
            await self._get_http().head(self._inference_url, timeout=5.0)
        except Exception:  # noqa: BLE001
            pass

    async def aclose(self) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    def _scrub(self, text: str) -> str:
        return text.replace(self._api_key, "<redacted>") if self._api_key else text

    async def run_task(self, *, task_config: dict[str, Any], input_data: dict[str, Any]) -> dict[str, Any]:
        """Runs a single-task pipeline and returns that task's
        `pipelineResponse[0]` entry (already structurally checked to be a
        dict). Raises BhashiniError on any transport, HTTP, or shape problem."""
        payload = {"pipelineTasks": [task_config], "inputData": input_data}
        headers = {"Authorization": self._api_key, "Content-Type": "application/json"}

        try:
            response = await self._get_http().post(self._inference_url, json=payload, headers=headers)
        except httpx.TimeoutException:
            raise BhashiniError(f"Bhashini request timed out after {self._timeout_seconds}s") from None
        except httpx.HTTPError as exc:
            # str(exc) for transport errors describes the connection, never
            # the headers — but scrub anyway, and drop the chained traceback
            # (`from None`) so the request object isn't carried along.
            raise BhashiniError(f"Bhashini connection failed: {type(exc).__name__}: {self._scrub(str(exc))[:200]}") from None

        if response.status_code >= 400:
            meaning = _STATUS_MEANING.get(response.status_code) or (
                "server error" if response.status_code >= 500 else "request failed"
            )
            detail = self._scrub(response.text or "")[:_MAX_ERROR_DETAIL_CHARS]
            raise BhashiniError(
                f"Bhashini returned HTTP {response.status_code} ({meaning}): {detail}",
                status_code=response.status_code,
            )

        try:
            body = response.json()
        except ValueError:
            raise BhashiniError("Bhashini returned a non-JSON response", status_code=response.status_code) from None

        results = body.get("pipelineResponse") if isinstance(body, dict) else None
        if not isinstance(results, list) or not results or not isinstance(results[0], dict):
            raise BhashiniError(
                "Bhashini response is missing 'pipelineResponse'", status_code=response.status_code
            )
        return results[0]
