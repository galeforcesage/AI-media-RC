from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import unittest
import json
from contextlib import asynccontextmanager
from pathlib import Path

TRANSCRIPTION_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TRANSCRIPTION_ROOT))

from src.gpu_broker import BrokerConfig, BrokerError, GpuBrokerClient, GpuLease
from src.gpu_control import GpuExecutionController
from src.models import TranscriptionJob
from src.queue import TranscriptionQueue
from src.server import TranscriptionServer
from src.store import MetadataStore
from src.whisper_engine import WhisperEngine
from src.worker import TranscriptionWorker


class FakeEngine:
    def __init__(self):
        self.unload_calls = 0
        self.model_name = "fake"

    def unload(self):
        self.unload_calls += 1


class FakeBroker:
    def __init__(self):
        self.lease = GpuLease("lease-1", 7, "0", 10**12, 30)
        self.lost = False
        self.ensure_calls = 0
        self.release_calls = 0
        self.close_calls = 0

    @property
    def lease_lost(self):
        return self.lost

    @property
    def has_active_lease(self):
        return self.lease is not None and not self.lost

    async def start(self):
        return None

    async def ensure_lease(self):
        self.ensure_calls += 1
        if self.lease is None or self.lost:
            self.lease = GpuLease(
                f"lease-{self.ensure_calls}",
                7 + self.ensure_calls,
                "0",
                10**12,
                30,
            )
            self.lost = False
        return self.lease

    async def release(self):
        self.release_calls += 1
        self.lease = None
        self.lost = False

    async def close(self):
        self.close_calls += 1
        await self.release()

    def matches_lease(self, lease_id, generation):
        return (
            self.lease is not None
            and str(lease_id) == self.lease.lease_id
            and int(generation) == self.lease.generation
        )


def two_phase_payload(broker: FakeBroker, phase: str, reclamation_id: str = "r-1"):
    lease = broker.lease
    payload = {
        "phase": phase,
        "reclamation_id": reclamation_id,
        "triggering_request_id": "live-1",
        "victim_request_id": "batch-1",
        "lease_id": lease.lease_id,
        "lease_generation": lease.generation,
        "gpu_device_id": lease.gpu_device_id,
        "reason": "SageTV LIVE enhancement",
        "checkpoint_reference": None,
    }
    if phase == "COMMIT":
        payload["checkpoint_reference"] = f"transcription:{reclamation_id}"
    return payload


class GpuExecutionControllerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.broker = FakeBroker()
        self.engine = FakeEngine()
        self.controller = GpuExecutionController(
            self.broker,
            self.engine,
            commit_timeout_seconds=1,
        )
        self.controller._sample_gpu_utilization = self._zero_utilization

    @staticmethod
    async def _zero_utilization():
        return 0

    async def test_yield_waits_for_long_active_segment_then_unloads_and_releases(self):
        segment_started = asyncio.Event()
        finish_segment = asyncio.Event()

        async def run_segment():
            async with self.controller.gpu_segment():
                segment_started.set()
                await finish_segment.wait()

        segment_task = asyncio.create_task(run_segment())
        await segment_started.wait()
        ready = await self.controller.handle_callback(
            two_phase_payload(self.broker, "PREPARE")
        )
        self.assertEqual("READY", ready["status"])
        self.assertTrue(self.controller.yield_armed)

        commit_task = asyncio.create_task(
            self.controller.handle_callback(two_phase_payload(self.broker, "COMMIT"))
        )
        await asyncio.sleep(0)
        self.assertFalse(commit_task.done())
        finish_segment.set()
        await segment_task
        committed = await commit_task

        self.assertEqual("COMMITTED", committed["status"])
        self.assertEqual(0, committed["gpu_utilization_pct"])
        self.assertEqual(1, self.engine.unload_calls)
        self.assertEqual(1, self.broker.release_calls)

    async def test_yield_between_segments_is_immediate(self):
        await self.controller.handle_callback(two_phase_payload(self.broker, "PREPARE"))
        committed = await self.controller.handle_callback(
            two_phase_payload(self.broker, "COMMIT")
        )
        self.assertEqual("COMMITTED", committed["status"])
        self.assertFalse(self.controller.yield_armed)

    async def test_duplicate_phase_callbacks_are_idempotent(self):
        prepare = two_phase_payload(self.broker, "PREPARE", "duplicate")
        first_ready = await self.controller.handle_callback(prepare)
        second_ready = await self.controller.handle_callback(prepare)
        self.assertEqual(first_ready, second_ready)

        commit = two_phase_payload(self.broker, "COMMIT", "duplicate")
        first_commit = await self.controller.handle_callback(commit)
        second_commit = await self.controller.handle_callback(commit)
        self.assertEqual(first_commit, second_commit)
        self.assertEqual(1, self.broker.release_calls)

    async def test_old_one_shot_callback_remains_compatible(self):
        response = await self.controller.handle_callback({
            "action": "checkpoint",
            "idempotency_key": "legacy-1",
            "deadline_ms": 1000,
        })
        duplicate = await self.controller.handle_callback({
            "action": "checkpoint",
            "idempotency_key": "legacy-1",
            "deadline_ms": 1000,
        })
        self.assertTrue(response["safe_point_reached"])
        self.assertEqual(response, duplicate)
        self.assertEqual(1, self.broker.release_calls)

    async def test_lease_loss_unloads_before_reacquiring(self):
        async with self.controller.gpu_segment():
            self.broker.lost = True
        self.assertEqual(1, self.engine.unload_calls)
        self.assertEqual(1, self.broker.release_calls)

        async with self.controller.gpu_segment():
            pass
        self.assertGreaterEqual(self.broker.ensure_calls, 2)

    async def test_shutdown_waits_for_segment_and_cleans_up(self):
        started = asyncio.Event()
        finish = asyncio.Event()

        async def run_segment():
            async with self.controller.gpu_segment():
                started.set()
                await finish.wait()

        segment = asyncio.create_task(run_segment())
        await started.wait()
        shutdown = asyncio.create_task(self.controller.shutdown())
        await asyncio.sleep(0)
        self.assertFalse(shutdown.done())
        finish.set()
        await segment
        await shutdown
        self.assertEqual(1, self.engine.unload_calls)
        self.assertEqual(1, self.broker.close_calls)


class RetryBroker(GpuBrokerClient):
    def __init__(self, failures: int):
        super().__init__(BrokerConfig(
            base_url="http://127.0.0.1:7235",
            application_id="test-transcription",
            callback_url="http://127.0.0.1:8770/gpu/reclamation",
            peak_vram_mb=4096,
            retry_initial_seconds=0.001,
            retry_max_seconds=0.002,
        ))
        self.failures = failures
        self.requests = []

    async def _request(self, method, path, payload, headers=None):
        self.requests.append((method, path, payload, headers))
        if path == "/v1/applications/register":
            return {}
        if path == "/v1/requests":
            if self.failures:
                self.failures -= 1
                raise BrokerError("temporary outage")
            return {
                "lease_id": "lease-retry",
                "lease_generation": 3,
                "gpu_device_id": "0",
                "ttl_seconds": 60,
            }
        return {}


class BrokerClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_broker_outage_retries_same_idempotent_request(self):
        broker = RetryBroker(failures=2)
        await broker.start()
        lease = await broker.ensure_lease()
        request_calls = [call for call in broker.requests if call[1] == "/v1/requests"]
        keys = {call[2]["idempotency_key"] for call in request_calls}
        self.assertEqual(3, len(request_calls))
        self.assertEqual(1, len(keys))
        self.assertEqual("lease-retry", lease.lease_id)
        registration = broker.requests[0][2]
        self.assertEqual("BATCH", registration["service_class"])
        self.assertEqual("CHECKPOINTABLE", registration["durability"])
        self.assertEqual("CHECKPOINTED", registration["active_gpu_mode_transition"])
        self.assertEqual("MANAGED_GPU", registration["resource_type"])
        await broker.close()


class FakeExtractor:
    def __init__(self):
        self.regions = []

    async def get_duration(self, _path):
        return 20.0

    async def extract_region(self, _path, start, end):
        self.regions.append((start, end))
        return f"/tmp/fake-{start}-{end}.wav"

    def cleanup(self, _path):
        return None


class FakeWorkerEngine(FakeEngine):
    def transcribe_clip(self, _path, offset):
        return [{"start": offset, "end": offset + 10, "text": "new"}]


class FakeGpu:
    @asynccontextmanager
    async def gpu_segment(self):
        yield

    async def release_idle(self):
        return None


class WorkerCheckpointTests(unittest.IsolatedAsyncioTestCase):
    async def test_resume_preserves_completed_work_without_duplicate_clip(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = os.path.join(temp_dir, "transcription.db")
            queue = TranscriptionQueue(db_path)
            queue.open()
            job = TranscriptionJob(
                job_id="job-1",
                system="sagetv",
                recording_id="recording-1",
                file_path="/tmp/recording.ts",
            )
            queue.enqueue(job)
            queue.save_checkpoint(
                job.job_id,
                completed_seconds=10.0,
                audio_duration=20.0,
                segments=[{"start": 0, "end": 10, "text": "completed"}],
            )
            extractor = FakeExtractor()
            engine = FakeWorkerEngine()
            worker = TranscriptionWorker(
                queue=queue,
                store=None,
                extractor=extractor,
                engine=engine,
                gpu=FakeGpu(),
                gpu_segment_seconds=10,
            )
            text, segments, _info = await worker._transcribe_segmented(
                job,
                "/tmp/audio.wav",
                asyncio.get_running_loop(),
            )
            self.assertEqual([(10.0, 20.0)], extractor.regions)
            self.assertEqual("completed new", text)
            self.assertEqual(2, len(segments))
            checkpoint = queue.load_checkpoint(job.job_id)
            self.assertEqual(20.0, checkpoint["completed_seconds"])
            queue.close()


class WhisperUnloadTests(unittest.TestCase):
    def test_model_unload_clears_loaded_model(self):
        engine = WhisperEngine(model_name="tiny", device="cpu")
        engine._model = object()
        engine.unload()
        self.assertIsNone(engine._model)


class FakeCallbackController:
    def __init__(self):
        self.payloads = []

    async def handle_callback(self, payload):
        self.payloads.append(payload)
        return {
            "safe_point_reached": True,
            "checkpoint_token": "legacy-token",
            "remaining_work": "queued",
            "detail": "ok",
        }


class ListenerMultiplexTests(unittest.IsolatedAsyncioTestCase):
    async def test_existing_json_rpc_and_http_callback_share_listener(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = os.path.join(temp_dir, "transcription.db")
            queue = TranscriptionQueue(db_path)
            store = MetadataStore(db_path)
            queue.open()
            store.open()
            controller = FakeCallbackController()
            server = TranscriptionServer(
                {
                    "mcp_host": "127.0.0.1",
                    "mcp_port": 0,
                    "index_db": os.path.join(temp_dir, "index.db"),
                    "sidecar_dir": os.path.join(temp_dir, "sidecars"),
                },
                queue,
                store,
                controller,
            )
            await server.start()
            port = server._server.sockets[0].getsockname()[1]
            try:
                reader, writer = await asyncio.open_connection("127.0.0.1", port)
                writer.write(b'{"jsonrpc":"2.0","id":1,"method":"ping","params":{}}\n')
                await writer.drain()
                rpc = json.loads((await reader.readline()).decode())
                self.assertEqual({}, rpc["result"])
                writer.close()
                await writer.wait_closed()

                payload = json.dumps({
                    "action": "checkpoint",
                    "idempotency_key": "old-contract",
                    "deadline_ms": 1000,
                }).encode()
                reader, writer = await asyncio.open_connection("127.0.0.1", port)
                writer.write(
                    (
                        "POST /checkpoint HTTP/1.1\r\n"
                        "Host: 127.0.0.1\r\n"
                        "Content-Type: application/json\r\n"
                        f"Content-Length: {len(payload)}\r\n\r\n"
                    ).encode()
                    + payload
                )
                await writer.drain()
                raw = await reader.read()
                self.assertIn(b"HTTP/1.1 200 OK", raw)
                self.assertIn(b'"safe_point_reached": true', raw)
                self.assertEqual("old-contract", controller.payloads[0]["idempotency_key"])
                writer.close()
                await writer.wait_closed()
            finally:
                await server.stop()
                queue.close()
                store.close()


if __name__ == "__main__":
    unittest.main()
