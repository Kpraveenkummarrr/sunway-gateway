"""Minimal async Asterisk REST Interface (ARI) client.

Deliberately thin — only the REST calls and event types the AI call
controller actually needs (answer, play, record, hangup, the event
stream). Not a general-purpose ARI SDK. Uses httpx for REST (already a
project dependency) and `websockets` for the event stream.
"""

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx
import websockets


class AriError(Exception):
    """Raised for any ARI REST call failure. Never includes the
    username/password in its message. `status_code` is the HTTP status, or
    None for transport failures."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class AriClient:
    def __init__(self, *, base_url: str, username: str, password: str, app: str) -> None:
        self._base_url = base_url.rstrip("/")
        self._username = username
        self._password = password
        self._app = app
        self._http = httpx.AsyncClient(
            base_url=self._base_url, auth=(username, password), timeout=10.0
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        try:
            response = await self._http.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise AriError(f"ARI request failed: {method} {path}: {exc}") from exc
        if response.status_code >= 400:
            # ARI error bodies are small JSON docs like {"message": "..."} —
            # safe to include, never contains the auth credentials.
            raise AriError(
                f"ARI {method} {path} returned {response.status_code}: {response.text}",
                status_code=response.status_code,
            )
        return response

    async def answer(self, channel_id: str) -> None:
        await self._request("POST", f"/channels/{channel_id}/answer")

    async def hangup(self, channel_id: str, *, reason: str | None = None) -> None:
        params = {"reason": reason} if reason else {}
        try:
            await self._request("DELETE", f"/channels/{channel_id}", params=params)
        except AriError:
            # Already gone (e.g. caller hung up first) — not an error for
            # our purposes, the channel is gone either way.
            pass

    async def play(self, channel_id: str, *, media: str) -> dict:
        response = await self._request("POST", f"/channels/{channel_id}/play", params={"media": media})
        return response.json()

    async def record(
        self,
        channel_id: str,
        *,
        name: str,
        max_duration_seconds: int,
        max_silence_seconds: int,
        audio_format: str = "wav",
    ) -> dict:
        response = await self._request(
            "POST",
            f"/channels/{channel_id}/record",
            params={
                "name": name,
                "format": audio_format,
                "maxDurationSeconds": max_duration_seconds,
                "maxSilenceSeconds": max_silence_seconds,
                "ifExists": "overwrite",
                "beep": "true",
            },
        )
        return response.json()

    async def get_channel(self, channel_id: str) -> dict:
        response = await self._request("GET", f"/channels/{channel_id}")
        return response.json()

    async def events(self) -> AsyncIterator[dict]:
        """Yields parsed ARI events from the WebSocket stream for this
        client's Stasis app. Runs until the connection closes."""
        ws_scheme = "wss" if self._base_url.startswith("https") else "ws"
        host_and_path = self._base_url.split("://", 1)[1]
        ws_url = (
            f"{ws_scheme}://{host_and_path}/events"
            f"?app={self._app}&api_key={self._username}:{self._password}&subscribeAll=true"
        )
        async with websockets.connect(ws_url) as ws:
            async for raw_message in ws:
                try:
                    yield json.loads(raw_message)
                except json.JSONDecodeError:
                    continue
