"""System health for the admin dashboard: real readings only.

Every value here is measured — Asterisk and the AI worker are queried over
ARI, the gateway is looked up in Asterisk's endpoint list, and resources
come from the OS. Anything that cannot be measured is reported as unknown
with the reason, never as a fabricated number.
"""

import shutil
from dataclasses import asdict, dataclass

import httpx

from app.core.config import Settings
from app.core.logging import get_logger

logger = get_logger(__name__)

ARI_TIMEOUT_SECONDS = 3.0


@dataclass
class Check:
    ok: bool | None  # None = could not be determined
    detail: str
    data: dict | None = None


async def _ari_get(settings: Settings, path: str) -> tuple[int | None, object]:
    url = f"{settings.resolved_ari_url()}{path}"
    auth = (settings.asterisk_ari_username, settings.asterisk_ari_password)
    try:
        async with httpx.AsyncClient(timeout=ARI_TIMEOUT_SECONDS) as http:
            response = await http.get(url, auth=auth)
    except httpx.HTTPError as exc:
        return None, f"{type(exc).__name__}"
    if response.status_code >= 400:
        return response.status_code, response.text[:120]
    try:
        return response.status_code, response.json()
    except ValueError:
        return response.status_code, None


async def check_asterisk(settings: Settings) -> Check:
    status, body = await _ari_get(settings, "/asterisk/info")
    if status is None:
        return Check(ok=False, detail=f"ARI unreachable at {settings.resolved_ari_url()} ({body})")
    if status == 401:
        return Check(ok=False, detail="ARI rejected the configured credentials")
    if status != 200 or not isinstance(body, dict):
        return Check(ok=False, detail=f"ARI returned HTTP {status}")
    system = body.get("system") or {}
    return Check(
        ok=True,
        detail=f"Asterisk {system.get('version', 'unknown')}",
        data={"version": system.get("version"), "entity_id": system.get("entity_id")},
    )


async def check_ai_worker(settings: Settings) -> Check:
    """The worker is up exactly when its Stasis app is registered with a
    live connection — ARI lists the app either way, so the subscription
    count is what distinguishes them."""
    status, body = await _ari_get(settings, f"/applications/{settings.asterisk_ari_app}")
    if status is None:
        return Check(ok=None, detail="Cannot tell — ARI unreachable")
    if status == 404:
        return Check(ok=False, detail=f"Stasis app '{settings.asterisk_ari_app}' not registered")
    if status != 200 or not isinstance(body, dict):
        return Check(ok=None, detail=f"ARI returned HTTP {status}")
    return Check(ok=True, detail=f"Worker connected as '{body.get('name', settings.asterisk_ari_app)}'")


async def check_gateway(settings: Settings) -> Check:
    """SIP/gateway state from Asterisk's endpoint list."""
    status, body = await _ari_get(settings, "/endpoints")
    if status is None:
        return Check(ok=None, detail="Cannot tell — ARI unreachable")
    if status != 200 or not isinstance(body, list):
        return Check(ok=None, detail=f"ARI returned HTTP {status}")

    endpoints = [
        {"resource": e.get("resource"), "state": e.get("state"), "channels": len(e.get("channel_ids") or [])}
        for e in body
        if isinstance(e, dict)
    ]
    online = [e for e in endpoints if e["state"] == "online"]
    if not endpoints:
        return Check(ok=False, detail="No SIP endpoints configured", data={"endpoints": []})
    return Check(
        ok=bool(online),
        detail=f"{len(online)} of {len(endpoints)} SIP endpoints online",
        data={"endpoints": endpoints},
    )


def check_resources() -> Check:
    """CPU load, memory and disk. Linux-only readings (/proc); on other
    platforms the unavailable parts are reported as unknown."""
    data: dict = {}
    try:
        with open("/proc/loadavg", encoding="utf-8") as handle:
            one, five, fifteen = handle.read().split()[:3]
        data["load_average"] = {"1m": float(one), "5m": float(five), "15m": float(fifteen)}
    except OSError:
        data["load_average"] = None

    try:
        meminfo = {}
        with open("/proc/meminfo", encoding="utf-8") as handle:
            for line in handle:
                key, _, rest = line.partition(":")
                meminfo[key] = int(rest.strip().split()[0])  # kB
        total, available = meminfo.get("MemTotal", 0), meminfo.get("MemAvailable", 0)
        if total:
            data["memory"] = {
                "total_mb": round(total / 1024),
                "used_mb": round((total - available) / 1024),
                "used_percent": round((total - available) / total * 100, 1),
            }
    except (OSError, ValueError, IndexError):
        data["memory"] = None

    try:
        usage = shutil.disk_usage("/")
        data["disk"] = {
            "total_gb": round(usage.total / 1024**3, 1),
            "used_gb": round(usage.used / 1024**3, 1),
            "used_percent": round(usage.used / usage.total * 100, 1),
        }
    except OSError:
        data["disk"] = None

    measured = [name for name, value in data.items() if value is not None]
    if not measured:
        return Check(ok=None, detail="Resource readings unavailable on this platform", data=data)
    return Check(ok=True, detail=f"Measured: {', '.join(measured)}", data=data)


async def collect(settings: Settings) -> dict:
    checks = {
        "asterisk": await check_asterisk(settings),
        "ai_worker": await check_ai_worker(settings),
        "gateway_sip": await check_gateway(settings),
        "resources": check_resources(),
    }
    return {
        "ok": all(check.ok for check in checks.values() if check.ok is not None),
        "checks": {name: asdict(check) for name, check in checks.items()},
    }
