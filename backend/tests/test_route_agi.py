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
