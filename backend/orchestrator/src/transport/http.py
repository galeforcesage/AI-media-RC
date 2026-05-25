"""
http.py
FastAPI HTTP transport layer for the orchestrator.
Exposes REST endpoints for query, playback, metadata, system, and search.
All endpoints accept/return JSON and route through the orchestrator.
"""

from __future__ import annotations
import asyncio
import json
import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException, UploadFile, File, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

logger = logging.getLogger(__name__)

router = APIRouter()

# The orchestrator instance is injected at startup.
_orchestrator: Any = None


def init_http_transport(orchestrator: Any) -> None:
    """Bind the orchestrator instance used by all route handlers."""
    global _orchestrator
    _orchestrator = orchestrator
    logger.info("HTTP transport initialized")


def _require_orchestrator() -> Any:
    if _orchestrator is None:
        raise HTTPException(status_code=503, detail="Orchestrator not initialized")
    return _orchestrator


# ------------------------------------------------------------------
# Request / Response models
# ------------------------------------------------------------------

class QueryRequest(BaseModel):
    """Request body for /query."""
    prompt: str
    synthesize: bool = True
    metadata: Optional[Dict[str, Any]] = None
    systems: Optional[list[str]] = None


class PlaybackRequest(BaseModel):
    """Request body for /playback."""
    action: str  # play, pause, stop, seek, status
    target: str = "sagetv"
    device_id: Optional[str] = None
    payload: Optional[Dict[str, Any]] = None


class MetadataRequest(BaseModel):
    """Request body for /metadata."""
    target: str = "sagetv"
    program_id: Optional[str] = None
    query: Optional[str] = None


class SystemRequest(BaseModel):
    """Request body for /system."""
    action: str  # info, diagnostics, volume, reboot, shutdown
    payload: Optional[Dict[str, Any]] = None


# ------------------------------------------------------------------
# Centralized error wrapper
# ------------------------------------------------------------------

async def _safe_execute(coro):
    """Await a coroutine and convert errors to HTTPException."""
    try:
        result = await coro
        if isinstance(result, dict) and "error" in result:
            raise HTTPException(status_code=422, detail=result["error"])
        return result
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Unhandled transport error")
        raise HTTPException(status_code=500, detail=str(exc))


# ------------------------------------------------------------------
# Endpoints
# ------------------------------------------------------------------

@router.post("/query")
async def query(request: QueryRequest):
    """
    Run a text or voice query through the LLM pipeline.
    Returns the LLM response and optionally a TTS audio path.
    """
    orch = _require_orchestrator()
    return await _safe_execute(
        orch.run_query(request.prompt, synthesize=request.synthesize, metadata=request.metadata, systems=request.systems)
    )


@router.post("/query/stream")
async def query_stream(request: QueryRequest):
    """
    SSE streaming variant of /query.
    Emits status events as the agent works, token events as the LLM
    generates, then a final result event.
    """
    orch = _require_orchestrator()
    # Unified queue: status messages (str), token chunks (dict), sentinel (None)
    event_queue: asyncio.Queue = asyncio.Queue()

    async def status_callback(msg: str) -> None:
        await event_queue.put({"type": "status", "message": msg})

    async def token_callback(token: str) -> None:
        event_queue.put_nowait({"type": "token", "token": token})

    async def run_and_finish():
        result = {"error": "Cancelled"}
        try:
            result = await orch.run_query(
                request.prompt,
                synthesize=request.synthesize,
                metadata=request.metadata,
                systems=request.systems,
                status_callback=status_callback,
                token_callback=token_callback,
            )
        except asyncio.CancelledError:
            logger.info("Query cancelled by client disconnect")
        except Exception as exc:
            result = {"error": str(exc)}
        finally:
            event_queue.put_nowait(None)  # sentinel
        return result

    task = asyncio.create_task(run_and_finish())

    async def event_generator():
        try:
            while True:
                evt = await event_queue.get()
                if evt is None:
                    break
                yield f"data: {json.dumps(evt)}\n\n"
            result = await task
            yield f"data: {json.dumps({'type': 'result', 'data': result})}\n\n"
        finally:
            if not task.done():
                task.cancel()

    return StreamingResponse(event_generator(), media_type="text/event-stream")


@router.post("/query/voice")
async def query_voice(audio: UploadFile = File(...)):
    """
    Run a voice query: upload audio → transcription → LLM → TTS.
    """
    import tempfile, os
    orch = _require_orchestrator()

    suffix = os.path.splitext(audio.filename or "audio.wav")[1] or ".wav"
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        content = await audio.read()
        tmp.write(content)
        tmp_path = tmp.name

    try:
        result = await orch.run_query_voice(tmp_path)
    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)

    if isinstance(result, dict) and "error" in result:
        raise HTTPException(status_code=422, detail=result["error"])
    return result


@router.post("/playback")
async def playback(request: PlaybackRequest):
    """
    Control playback: play, pause, stop, seek, or query status.
    If device_id is provided, the orchestrator resolves it to a session_id.
    """
    orch = _require_orchestrator()
    payload = dict(request.payload or {})
    if request.device_id:
        payload["device_id"] = request.device_id
    return await _safe_execute(
        orch.run_playback(request.action, request.target, payload)
    )


@router.post("/metadata")
async def metadata(request: MetadataRequest):
    """
    Fetch program metadata or search for programs.
    """
    orch = _require_orchestrator()
    if request.query:
        return await _safe_execute(
            orch.run_search(request.query, target=request.target)
        )
    if request.program_id:
        return await _safe_execute(
            orch.run_metadata(request.target, request.program_id)
        )
    raise HTTPException(status_code=400, detail="Provide 'program_id' or 'query'")


@router.get("/search")
async def search(
    q: str = Query(..., min_length=1, description="Search query"),
    target: str | None = Query(None, description="Backend to search"),
):
    """Search for programs across backends."""
    orch = _require_orchestrator()
    return await _safe_execute(orch.run_search(q, target=target))


@router.get("/transcript/search")
async def search_transcripts(
    q: str = Query(..., min_length=1, description="Search query"),
):
    """Search transcripts by title/episode using FTS5 cross-search index."""
    orch = _require_orchestrator()
    result = await orch.agent._call_transcription(
        "transcript_cross_search", {"query": q, "limit": 50}
    )
    # Normalize response for frontend: expects {results: [{recording_id, title, episode}]}
    if isinstance(result, dict) and result.get("success") and "data" in result:
        data = result["data"]
        raw_results = data.get("results", [])
        # Deduplicate by recording_id (cross_search returns per-chunk)
        seen = {}
        for r in raw_results:
            rid = r.get("recording_id", "")
            if rid and rid not in seen:
                seen[rid] = {
                    "recording_id": rid,
                    "title": r.get("title", ""),
                    "episode": r.get("episode_title", ""),
                    "snippet": r.get("snippet", ""),
                    "system": r.get("system", ""),
                }
        return {"results": list(seen.values()), "count": len(seen)}
    # Fallback: try legacy transcript_search
    fallback = await orch.agent._call_transcription(
        "transcript_search", {"query": q, "limit": 50}
    )
    return fallback or {"results": [], "count": 0}


@router.get("/transcript/{recording_id}")
async def get_transcript(recording_id: str):
    """Fetch transcript for a recording by ID."""
    orch = _require_orchestrator()
    result = await orch.agent._call_transcription(
        "transcript_get", {"recording_id": recording_id}
    )
    if isinstance(result, dict) and result.get("error"):
        raise HTTPException(status_code=404, detail=result["error"])
    # Unwrap {success, data} envelope from transcription MCP server
    if isinstance(result, dict) and "data" in result:
        return result["data"]
    return result


@router.post("/system")
async def system(request: SystemRequest):
    """
    Run system commands: info, diagnostics, volume, reboot, shutdown.
    """
    orch = _require_orchestrator()
    return await _safe_execute(
        orch.run_system(request.action, request.payload or {})
    )


@router.get("/health")
async def health():
    """Basic health check."""
    return {"status": "ok"}


@router.get("/bridge/devices")
async def bridge_devices():
    """List connected Channels playback devices (bridge + direct)."""
    orch = _require_orchestrator()
    result = await orch.execute("channels.get_bridge_devices", {})
    if isinstance(result, dict) and result.get("success"):
        return {"devices": result.get("data", [])}
    return {"devices": [], "error": result.get("message", "Could not fetch devices")}


@router.get("/bridge/status")
async def bridge_status(device: str = Query(..., description="Device name")):
    """Get playback status from a Channels bridge device."""
    orch = _require_orchestrator()
    result = await orch.execute("channels.get_playback_status", {"device": device})
    return result


@router.get("/services")
async def services():
    """
    Health-check all AI-media-RC services.
    Returns a dict of service_id → { name, port, status, latency_ms }.
    """
    import aiohttp
    import time

    checks = {
        "orchestrator": {"name": "Orchestrator", "port": 8000, "url": "http://127.0.0.1:8000/api/health"},
        "mcp_sagetv":   {"name": "MCP SageTV",   "port": 8766, "url": None},
        "mcp_channels": {"name": "MCP Channels",  "port": 8767, "url": None},
        "mcp_linux":    {"name": "MCP Linux",     "port": 8768, "url": None},
        "session_mgr":  {"name": "Session Manager","port": 8769, "url": "http://127.0.0.1:8769/health"},
        "transcription":{"name": "Transcription",  "port": 8770, "url": None},
    }

    # DVR backend APIs (external servers the MCP layer talks to)
    cfg = _orchestrator.config if _orchestrator else {}
    channels_url = cfg.get("channels_dvr_url", "http://localhost:8089")
    sagetv_url = cfg.get("sagetv_url", "http://localhost:8080")
    sagetv_user = cfg.get("sagetv_user", "sage")
    sagetv_pass = cfg.get("sagetv_pass", "")
    dvr_checks = {
        "channels_dvr": {"name": "Channels DVR", "port": 8089, "url": f"{channels_url}/status",
                         "group": "dvr"},
        "sagetv_server": {"name": "SageTV Server", "port": 8080,
                          "url": f"{sagetv_url}/sagex/api?c=MediaPlayerAPI.GetCurrentMediaFile&encoder=json",
                          "auth": aiohttp.BasicAuth(sagetv_user, sagetv_pass),
                          "group": "dvr"},
    }

    results = {}
    timeout = aiohttp.ClientTimeout(total=3)

    async def check_http(sid, info):
        t0 = time.monotonic()
        try:
            headers = {}
            auth = info.get("auth")
            if auth:
                import base64
                cred = base64.b64encode(f"{auth.login}:{auth.password}".encode()).decode()
                headers["Authorization"] = f"Basic {cred}"
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(info["url"], headers=headers) as resp:
                    ms = round((time.monotonic() - t0) * 1000)
                    status = "up" if resp.status == 200 else "degraded"
                    if status == "degraded":
                        logger.warning("Health check %s: HTTP %d from %s", sid, resp.status, info["url"])
                    results[sid] = {
                        "name": info["name"],
                        "port": info["port"],
                        "status": status,
                        "latency_ms": ms,
                    }
        except Exception as exc:
            ms = round((time.monotonic() - t0) * 1000)
            logger.warning("Health check %s failed: %s", sid, exc)
            results[sid] = {"name": info["name"], "port": info["port"], "status": "down", "latency_ms": ms}

    async def check_tcp(sid, info):
        """TCP connect check for MCP servers (JSON-RPC, no HTTP health route)."""
        import asyncio
        t0 = time.monotonic()
        try:
            _, writer = await asyncio.wait_for(
                asyncio.open_connection("127.0.0.1", info["port"]), timeout=2
            )
            writer.close()
            await writer.wait_closed()
            ms = round((time.monotonic() - t0) * 1000)
            results[sid] = {"name": info["name"], "port": info["port"], "status": "up", "latency_ms": ms}
        except Exception:
            ms = round((time.monotonic() - t0) * 1000)
            results[sid] = {"name": info["name"], "port": info["port"], "status": "down", "latency_ms": ms}

    import asyncio
    tasks = []
    for sid, info in checks.items():
        if sid == "orchestrator":
            # We're obviously up if serving this request
            results[sid] = {"name": info["name"], "port": info["port"], "status": "up", "latency_ms": 0}
            continue
        if info["url"]:
            tasks.append(check_http(sid, info))
        else:
            tasks.append(check_tcp(sid, info))

    # DVR backend checks (all HTTP)
    for sid, info in dvr_checks.items():
        tasks.append(check_http(sid, info))

    await asyncio.gather(*tasks)

    # Enrich transcription service with diarization/stats info
    if "transcription" in results and results["transcription"]["status"] == "up":
        try:
            orch = _require_orchestrator()
            stats = await orch.agent._call_transcription("transcript_stats", {})
            if isinstance(stats, dict) and "data" in stats:
                stats = stats["data"]
            diar = stats.get("diarization", {})
            results["transcription"]["diarization"] = diar.get("available", False)
            results["transcription"]["queue"] = stats.get("queue", {})
            results["transcription"]["total_transcripts"] = stats.get("total_transcripts", 0)
            results["transcription"]["named_shows"] = stats.get("named_shows", 0)
            results["transcription"]["diarized_shows"] = stats.get("diarized_shows", 0)
            results["transcription"]["cc_only_count"] = stats.get("cc_only_count", 0)
            results["transcription"]["stt_full_count"] = stats.get("stt_full_count", 0)
            results["transcription"]["mixed_count"] = stats.get("mixed_count", 0)
        except Exception:
            pass

    # Split results into services vs dvr_backends
    dvr_backends = {}
    for sid in list(dvr_checks.keys()):
        if sid in results:
            dvr_backends[sid] = results.pop(sid)

    return {"services": results, "dvr_backends": dvr_backends}


@router.get("/gpu")
async def gpu_status():
    """
    Report GPU/CUDA availability and Ollama model placement.
    Returns:
        {
          "cuda_available": bool,
          "driver_version": str | None,
          "gpus": [{name, memory_total_mb, memory_used_mb, utilization_pct}],
          "error": str | None,
          "ollama_models": [{name, size, processor, context}],
        }
    """
    import asyncio
    out: Dict[str, Any] = {
        "cuda_available": False,
        "driver_version": None,
        "gpus": [],
        "error": None,
        "ollama_models": [],
    }

    async def _run(cmd: list[str]) -> tuple[int, str, str]:
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=4)
            return proc.returncode or 0, stdout.decode(errors="ignore"), stderr.decode(errors="ignore")
        except FileNotFoundError:
            return 127, "", "command not found"
        except asyncio.TimeoutError:
            return 124, "", "timeout"
        except Exception as exc:  # noqa: BLE001
            return 1, "", str(exc)

    # nvidia-smi: GPU info
    rc, stdout, stderr = await _run([
        "nvidia-smi",
        "--query-gpu=name,driver_version,memory.total,memory.used,utilization.gpu",
        "--format=csv,noheader,nounits",
    ])
    if rc == 0 and stdout.strip():
        out["cuda_available"] = True
        for line in stdout.strip().splitlines():
            parts = [p.strip() for p in line.split(",")]
            if len(parts) >= 5:
                if not out["driver_version"]:
                    out["driver_version"] = parts[1]
                try:
                    out["gpus"].append({
                        "name": parts[0],
                        "memory_total_mb": int(parts[2]),
                        "memory_used_mb": int(parts[3]),
                        "utilization_pct": int(parts[4]),
                    })
                except ValueError:
                    out["gpus"].append({"name": parts[0]})
    else:
        out["error"] = (stderr or stdout or "nvidia-smi unavailable").strip().splitlines()[0][:200]

    # ollama ps: which models are loaded and where (CPU/GPU)
    rc, stdout, _ = await _run(["ollama", "ps"])
    if rc == 0 and stdout.strip():
        lines = stdout.strip().splitlines()
        # Skip header line
        for line in lines[1:]:
            # Header columns: NAME ID SIZE PROCESSOR CONTEXT UNTIL
            # Split into max 6 chunks; PROCESSOR may be e.g. "100% CPU" or "100% GPU" or "50%/50% CPU/GPU"
            cols = line.split()
            if len(cols) < 6:
                continue
            # Reconstruct: name, id, size+unit (2 tokens), processor (2 tokens like "100% CPU"), context, until...
            try:
                name = cols[0]
                size = f"{cols[2]} {cols[3]}"
                processor = f"{cols[4]} {cols[5]}"
                context = cols[6] if len(cols) > 6 else ""
                out["ollama_models"].append({
                    "name": name,
                    "size": size,
                    "processor": processor,
                    "context": context,
                })
            except IndexError:
                continue

    return out


@router.get("/alerts")
async def alerts(
    limit: int = 50,
    severity: Optional[str] = None,
    since_ts: Optional[str] = None,
):
    """Return recent system alerts emitted by the watchdog.

    Each alert: ts, svc, severity (info/warning/error/critical), code, message.
    Sorted newest-first. Augmented with synthetic alerts for stuck transcription
    jobs and low-disk conditions.
    """
    orch = _require_orchestrator()
    out: list[dict] = []

    # 1) Real watchdog alerts via mcp-linux
    try:
        args: Dict[str, Any] = {"limit": limit}
        if severity:
            args["severity"] = severity
        if since_ts:
            args["since_ts"] = since_ts
        result = await orch._linux.call_tool("linux_get_alerts", args)
        if isinstance(result, dict) and result.get("success"):
            out.extend(result.get("data", {}).get("alerts", []) or [])
    except Exception as exc:
        logger.warning("alerts: linux_get_alerts failed: %s", exc)

    # 2) Synthetic: stuck transcription jobs
    try:
        import asyncio as _asyncio
        import json as _json
        reader, writer = await _asyncio.open_connection("127.0.0.1", 8770)
        req = _json.dumps({
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "transcript_stats", "arguments": {}},
        }) + "\n"
        writer.write(req.encode()); await writer.drain()
        line = await _asyncio.wait_for(reader.readline(), timeout=3)
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
        if line:
            resp = _json.loads(line.decode())
            content = resp.get("result", {}).get("content", [])
            if content and content[0].get("type") == "text":
                data = _json.loads(content[0]["text"])
                payload = data.get("data", data) if isinstance(data, dict) else {}
                stale = payload.get("stale_jobs") or []
                for j in stale:
                    mins = int((j.get("stuck_seconds") or 0) // 60)
                    rid = (j.get("recording_id") or "?")[:40]
                    jid = (j.get("job_id") or "?")[:12]
                    out.append({
                        "ts": "",
                        "svc": "transcription",
                        "severity": "warning",
                        "code": "stale_job",
                        "message": f"job {jid} ({rid}) stuck in {j.get('status')} for {mins} min",
                    })
    except Exception as exc:
        logger.debug("alerts: stale-job probe failed: %s", exc)

    # 3) Synthetic: low disk (>=90%)
    try:
        result = await orch._linux.call_tool("linux_disk_usage", {})
        if isinstance(result, dict) and result.get("success"):
            mounts = result.get("data", {}).get("mounts", []) or []
            for m in mounts:
                raw = m.get("use_percent") if "use_percent" in m else m.get("percent")
                try:
                    pct = float(str(raw).rstrip("%")) if raw is not None else 0.0
                except (TypeError, ValueError):
                    pct = 0.0
                if pct >= 90.0:
                    out.append({
                        "ts": "",
                        "svc": "system",
                        "severity": "error" if pct >= 95.0 else "warning",
                        "code": "low_disk",
                        "message": f"{m.get('mount','?')} is {pct:.0f}% full",
                    })
    except Exception as exc:
        logger.debug("alerts: disk probe failed: %s", exc)

    if severity:
        sev = severity.lower()
        out = [a for a in out if (a.get("severity") or "").lower() == sev]
    return {"alerts": out[:limit], "count": len(out[:limit])}


@router.post("/alerts/clear")
async def alerts_clear():
    """Clear the watchdog alerts file (OWNER scope)."""
    orch = _require_orchestrator()
    try:
        result = await orch._linux.call_tool("linux_clear_alerts", {})
        if isinstance(result, dict):
            return result
        return {"success": False, "error": "unexpected response"}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
