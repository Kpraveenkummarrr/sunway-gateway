#!/usr/bin/env python3
"""AGI bridge between the Asterisk dialplan and the call-centre API.

Two modes, both called from the [departments] context:

    AGI(route_agi.py,<digit>)
        Looks up the department for the DTMF digit and sets the dialplan
        variables the context loops over:
            ROUTE_COUNT, ROUTE_1..N, ROUTE_LABEL_1..N,
            ROUTE_TIMEOUT, ROUTE_NO_ANSWER_ACTION, ROUTE_DEPARTMENT
        ROUTE_COUNT=0 means "nobody to ring" (unknown/disabled department,
        no active agents, or the backend being unreachable).

    AGI(route_agi.py,attempt,<index>,<number>,<dialstatus>)
        Reports the outcome of one dial attempt so it lands in the call
        history. Never fails the call: reporting is best effort.

Configuration comes from the environment, or from the backend .env file
(SUNWAY_ENV_FILE, default /opt/sunway-gateway/backend/.env):
    SUNWAY_API_URL   (default http://127.0.0.1:8000)
    INTERNAL_API_KEY (sent as X-Internal-Api-Key; never logged)
"""

import json
import os
import sys
import urllib.error
import urllib.request

DEFAULT_ENV_FILE = "/opt/sunway-gateway/backend/.env"
DEFAULT_API_URL = "http://127.0.0.1:8000"
TIMEOUT_SECONDS = 5


def read_agi_env() -> dict:
    env = {}
    while True:
        line = sys.stdin.readline()
        if not line or not line.strip():
            break
        key, _, value = line.partition(":")
        env[key.strip()] = value.strip()
    return env


def agi_command(command: str) -> str:
    sys.stdout.write(command + "\n")
    sys.stdout.flush()
    return sys.stdin.readline().strip()


def set_variable(name: str, value) -> None:
    # Quotes would terminate the AGI argument early; the values here are
    # names and phone numbers, so stripping them is safe.
    cleaned = str(value).replace('"', "").replace("\n", " ")
    agi_command(f'SET VARIABLE {name} "{cleaned}"')


def log(message: str) -> None:
    agi_command(f'VERBOSE "route_agi: {message}" 1')


def attempt_channel_id(env: dict) -> str:
    """Return the identifier used by the backend's Call row lookup."""
    return env.get("agi_channel") or env.get("agi_uniqueid", "")


def load_settings() -> tuple[str, str]:
    api_url = os.environ.get("SUNWAY_API_URL", "")
    api_key = os.environ.get("INTERNAL_API_KEY", "")
    env_file = os.environ.get("SUNWAY_ENV_FILE", DEFAULT_ENV_FILE)
    if (not api_url or not api_key) and os.path.exists(env_file):
        try:
            with open(env_file, encoding="utf-8") as handle:
                for line in handle:
                    key, _, value = line.strip().partition("=")
                    if key == "INTERNAL_API_KEY" and not api_key:
                        api_key = value
                    elif key == "SUNWAY_API_URL" and not api_url:
                        api_url = value
        except OSError:
            pass
    return (api_url or DEFAULT_API_URL).rstrip("/"), api_key


def request(method: str, path: str, api_url: str, api_key: str, payload: dict | None = None) -> dict | None:
    req = urllib.request.Request(
        f"{api_url}{path}",
        method=method,
        data=json.dumps(payload).encode() if payload is not None else None,
        headers={"Content-Type": "application/json", **({"X-Internal-Api-Key": api_key} if api_key else {})},
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as response:
            body = response.read().decode()
            return json.loads(body) if body else {}
    except urllib.error.HTTPError as exc:
        log(f"{method} {path} -> HTTP {exc.code}")
    except Exception as exc:  # noqa: BLE001 - the call must continue regardless
        log(f"{method} {path} failed: {type(exc).__name__}")
    return None


def publish_route(digit: str, api_url: str, api_key: str) -> None:
    set_variable("ROUTE_COUNT", 0)
    set_variable("ROUTE_FOUND", 0)
    plan = request("GET", f"/api/callcentre/route/{digit}", api_url, api_key)
    if not plan:
        log(f"no routing plan for digit {digit}")
        return

    targets = plan.get("targets") or []
    set_variable("ROUTE_FOUND", 1)
    for index, target in enumerate(targets, start=1):
        set_variable(f"ROUTE_{index}", target.get("number", ""))
        set_variable(f"ROUTE_LABEL_{index}", target.get("label", ""))
    set_variable("ROUTE_COUNT", len(targets))
    set_variable("ROUTE_TIMEOUT", plan.get("ring_timeout_seconds", 25))
    set_variable("ROUTE_NO_ANSWER_ACTION", plan.get("no_answer_action", "ai"))
    set_variable("ROUTE_DEPARTMENT", plan.get("department_name", ""))
    log(f"digit {digit} -> {plan.get('department_name')} with {len(targets)} destination(s)")


def publish_attempt(args: list[str], env: dict, api_url: str, api_key: str) -> None:
    index, number, dialstatus = (args + ["", "", ""])[:3]
    log(f"attempt {index} to {number} -> {dialstatus}")
    # ARI stores calls by channel id (for example PJSIP/...-00000001),
    # whereas agi_uniqueid is only the call's numeric unique ID.  Prefer the
    # channel identifier so attempt events attach to the existing Call row;
    # retain uniqueid as a compatibility fallback for older/custom dialplans.
    channel_id = attempt_channel_id(env)
    request(
        "POST",
        "/api/callcentre/route/attempts",
        api_url,
        api_key,
        {
            "channel_id": channel_id,
            "number": number,
            "attempt": int(index) if str(index).isdigit() else 1,
            "result": {"ANSWER": "answered", "NOANSWER": "noanswer", "BUSY": "busy"}.get(
                dialstatus.upper(), (dialstatus or "failed").lower()
            ),
        },
    )


def main() -> int:
    env = read_agi_env()
    args = sys.argv[1:]
    if not args:
        log("called without arguments")
        return 0

    api_url, api_key = load_settings()
    if args[0] == "attempt":
        publish_attempt(args[1:], env, api_url, api_key)
    else:
        publish_route(args[0], api_url, api_key)
    return 0


if __name__ == "__main__":
    sys.exit(main())
