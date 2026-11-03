from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.gpu_broker import GpuBroker, _token_from_env_file


def _broker(**kwargs) -> GpuBroker:
    return GpuBroker(lambda: None, lambda: False, lambda: None, token="test", **kwargs)


def test_registration_is_preemptible_batch():
    registration = _broker().registration()

    assert registration["service_classes_supported"] == ["BATCH"]
    assert registration["durability"] == "RETRYABLE"
    mode = registration["modes"][0]
    assert mode["transition"]["type"] == "HIBERNATABLE"
    assert mode["transition"]["safe_point"] == "NEXT_SEGMENT"


def test_resource_request_cannot_bypass_local_controlled_provider():
    request = _broker(provider_id="local-controlled").resource_request(
        "job-1", "request-1"
    )

    assert request["service_class"] == "BATCH"
    assert request["wait_policy"]["can_queue"] is True
    assert request["wait_policy"]["maximum_wait_ms"] is None
    assert request["placement_policy"] == {
        "remote_allowed": False,
        "opportunistic_allowed": False,
        "best_effort_allowed": False,
        "migration_allowed": False,
        "preferred_provider_id": "local-controlled",
    }


def test_token_file_uses_first_configured_broker_key(tmp_path):
    env_file = tmp_path / "broker.env"
    env_file.write_text(
        "# comment\n'BROKER_API_KEYS'='[\"first-key\", \"second-key\"]'\nAGENT_API_KEY=ignored\n",
        encoding="utf-8",
    )

    assert _token_from_env_file(str(env_file)) == "first-key"


def test_authorization_header_uses_bearer_token():
    broker = _broker()

    assert broker._headers()["Authorization"] == "Bearer test"
