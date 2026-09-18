"""Read-only client stability sampler. Does not originate calls or copy secrets.

Run while the operator makes real calls; missing services remain failures.
The default is a 30-minute observation, not a synthetic load generator.
"""
import argparse
import asyncio
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import httpx
from sqlalchemy import text
from app.core.config import Settings
from app.core.db import AsyncSessionLocal, engine
from app.services.system_health import collect


def process_sample(pid):
    root = Path(f"/proc/{pid}")
    try:
        wanted = {"VmRSS", "VmSize", "Threads", "FDSize"}
        result = {k: v.strip() for line in (root / "status").read_text().splitlines()
                  for k, _, v in [line.partition(":")] if k in wanted}
        result["open_fds"] = len(list((root / "fd").iterdir()))
        result["cpu_ticks"] = (root / "stat").read_text().rsplit(")", 1)[1].split()[11:13]
        result["children"] = (root / "task" / str(pid) / "children").read_text().split()
        return result
    except (OSError, IndexError):
        return {"unavailable": True, "reason": "Linux PID absent or not readable"}


async def sample(settings, pid):
    started = time.monotonic()
    report = {"at_utc": datetime.now(timezone.utc).isoformat(), "worker": process_sample(pid) if pid else None}
    report["health"] = await collect(settings)
    try:
        async with AsyncSessionLocal() as db:
            await asyncio.wait_for(db.execute(text("SELECT 1")), timeout=3)
        report["database_ok"] = True
    except Exception as exc:
        report["database_ok"] = False
        report["database_error_type"] = type(exc).__name__
    try:
        async with httpx.AsyncClient(timeout=3) as client:
            response = await client.get(settings.resolved_ari_url() + "/channels",
                                        auth=(settings.asterisk_ari_username, settings.asterisk_ari_password))
            report["active_channels"] = len(response.json()) if response.status_code == 200 else None
            report["channels_status"] = response.status_code
    except (httpx.HTTPError, ValueError):
        report["active_channels"] = None
    report["sample_ms"] = round((time.monotonic() - started) * 1000)
    return report


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--duration", type=int, default=1800)
    parser.add_argument("--interval", type=int, default=10)
    parser.add_argument("--worker-pid", type=int)
    args = parser.parse_args()
    if args.duration < 1 or args.interval < 1:
        parser.error("duration and interval must be positive")
    args.out.parent.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    samples, unhealthy = 0, 0
    try:
        with args.out.open("x", encoding="utf-8") as stream:
            while True:
                report = await sample(Settings(), args.worker_pid)
                samples += 1
                unhealthy += not (report["database_ok"] and report["health"]["ok"])
                stream.write(json.dumps(report) + "\n")
                stream.flush()
                print(f"sample={samples} elapsed={time.monotonic()-started:.1f}s unhealthy_samples={unhealthy}", flush=True)
                remaining = args.duration - (time.monotonic() - started)
                if remaining <= 0:
                    break
                await asyncio.sleep(min(args.interval, remaining))
    finally:
        await engine.dispose()
    return 1 if unhealthy else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
