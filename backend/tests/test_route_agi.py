import importlib.util
from pathlib import Path


SCRIPT = Path(__file__).parents[2] / "asterisk" / "scripts" / "route_agi.py"
SPEC = importlib.util.spec_from_file_location("route_agi", SCRIPT)
route_agi = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(route_agi)


def test_attempt_uses_ari_channel_id_when_available() -> None:
    assert route_agi.attempt_channel_id(
        {"agi_channel": "PJSIP/smg-00000001", "agi_uniqueid": "1726490000.7"}
    ) == "PJSIP/smg-00000001"


def test_attempt_keeps_uniqueid_fallback_for_legacy_dialplans() -> None:
    assert route_agi.attempt_channel_id({"agi_uniqueid": "1726490000.7"}) == "1726490000.7"


def test_department_without_staff_is_distinguished_from_missing_route(monkeypatch):
    values = {}
    monkeypatch.setattr(route_agi, "set_variable", lambda k, v: values.update({k: v}))
    monkeypatch.setattr(route_agi, "log", lambda *args: None)
    monkeypatch.setattr(route_agi, "request", lambda *args: {"targets": [], "no_answer_action": "ai"})
    route_agi.publish_route("1", "http://localhost", "test")
    assert values["ROUTE_FOUND"] == 1
    assert values["ROUTE_COUNT"] == 0
    assert values["ROUTE_NO_ANSWER_ACTION"] == "ai"
