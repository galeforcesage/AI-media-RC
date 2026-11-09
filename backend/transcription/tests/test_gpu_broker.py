from __future__ import annotations

import asyncio
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.gpu_broker import GpuBroker, GpuBrokerUnavailable, _token_from_env_file


def _broker(**kwargs) -> GpuBroker:
    return GpuBroker(lambda: None, lambda: False, lambda: None, token="test", **kwargs)


def test_registration_is_preemptible_batch():
    registration = _broker().registration()

    assert registration["service_classes_supported"] == ["BATCH"]
    assert registration["durability"] == "RETRYABLE"
    assert registration["placement"]["allowed_provider_types"] == ["MANAGED_GPU"]
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


def test_active_lease_is_renewed():
    async def run():
        broker = _broker(renew_interval=0.01)
        renewed = asyncio.Event()

        async def request(method, path, body=None):
            if path == "/v1/requests":
                return 201, {
                    "result": "GRANTED",
                    "grant": {
                        "provider_id": "sagetv-cl-ollama",
                        "lease_id": "lease-1",
                        "lease_generation": 1,
                    },
                }
            if path.endswith("/running"):
                return 200, {}
            if path == "/v1/leases/lease-1/renew":
                renewed.set()
                return 200, {"generation": 2}
            if path.endswith("/complete"):
                return 200, {}
            raise AssertionError((method, path, body))

        broker._request = request
        await broker.acquire("job-1")
        await asyncio.wait_for(renewed.wait(), timeout=1)
        assert broker._lease.generation == 2
        await broker.complete()

    asyncio.run(run())


def test_failed_running_transition_releases_lease():
    async def run():
        broker = _broker()
        calls = []

        async def request(method, path, body=None):
            calls.append((method, path, body))
            if path == "/v1/requests":
                return 201, {
                    "result": "GRANTED",
                    "grant": {
                        "provider_id": "sagetv-cl-ollama",
                        "lease_id": "lease-1",
                        "lease_generation": 3,
                    },
                }
            if path.endswith("/running"):
                return 404, {"detail": "request missing"}
            if path == "/v1/leases/lease-1/release":
                return 200, {}
            raise AssertionError((method, path, body))

        broker._request = request

        try:
            await broker.acquire("job-1")
        except GpuBrokerUnavailable as exc:
            assert "could not enter running state" in str(exc)
        else:
            raise AssertionError("acquire should fail when running transition fails")

        assert broker._lease is None
        assert calls[-1] == (
            "POST",
            "/v1/leases/lease-1/release",
            {"generation": 3},
        )

    asyncio.run(run())


def test_authorization_header_uses_bearer_token():
    broker = _broker()

    assert broker._headers()["Authorization"] == "Bearer test"
