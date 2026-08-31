#!/bin/bash
# watchdog.sh â€” Auto-restart wrapper for AI-media-RC services.
#
# Restarts a service when it crashes, with crash-loop protection:
# - If 3+ crashes happen within 120 seconds, stop restarting and log an alert.
# - Logs all restarts to /tmp/<service>-watchdog.log
#
# Usage:
#   watchdog.sh <service-name>
#   watchdog.sh all            # start all services with watchdogs
#   watchdog.sh stop           # stop all watchdog-managed services
#
# Services: orchestrator, mcp-sagetv, mcp-channels, mcp-linux, transcription, session-manager

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
ORCH_VENV="$ROOT/backend/orchestrator/.venv/bin/python"
TRANS_VENV="$ROOT/backend/transcription/.venv/bin/python"
ORCH_CONFIG="$ROOT/backend/orchestrator/config.json"
OLLAMA_BIN="$(command -v ollama 2>/dev/null || true)"
OLLAMA_SYNC_LOG="/tmp/ollama-model-sync.log"

# Crash-loop thresholds
MAX_CRASHES=3
CRASH_WINDOW=120  # seconds

# Log rotation: max size per log file, keep 1 backup
MAX_LOG_BYTES=$((10 * 1024 * 1024))  # 10 MB

declare -A SVC_DIR SVC_CMD SVC_LOG SVC_PORT

SVC_DIR[orchestrator]="$ROOT/backend/orchestrator"
SVC_CMD[orchestrator]="env PYTHONPATH=src nice -n 10 $ORCH_VENV -m src.main --debug"
SVC_LOG[orchestrator]="/tmp/orchestrator.log"
SVC_PORT[orchestrator]=8000

SVC_DIR[mcp-sagetv]="$ROOT/backend/mcp-sagetv"
SVC_CMD[mcp-sagetv]="$ORCH_VENV main.py --debug"
SVC_LOG[mcp-sagetv]="/tmp/mcp-sagetv.log"
SVC_PORT[mcp-sagetv]=8766

SVC_DIR[mcp-channels]="$ROOT/backend/mcp-channels"
SVC_CMD[mcp-channels]="$ORCH_VENV main.py --host 127.0.0.1 --port 8767 --bridge-port 8771 --debug"
SVC_LOG[mcp-channels]="/tmp/mcp-channels.log"
SVC_PORT[mcp-channels]=8767

SVC_DIR[mcp-linux]="$ROOT/backend/mcp-linux"
SVC_CMD[mcp-linux]="$ORCH_VENV main.py --debug"
SVC_LOG[mcp-linux]="/tmp/mcp-linux.log"
SVC_PORT[mcp-linux]=8768

SVC_DIR[session-manager]="$ROOT/backend/session-manager"
SVC_CMD[session-manager]="$ORCH_VENV main.py --debug"
SVC_LOG[session-manager]="/tmp/session-manager.log"
SVC_PORT[session-manager]=8769

SVC_DIR[transcription]="$ROOT/backend/transcription"
# Auto-detect CPU cores; use 25% for transcription (minimum 1), lowest scheduling priority
_NCPU=$(nproc 2>/dev/null || echo 4)
_WHISPER_THREADS=$(( _NCPU / 4 > 0 ? _NCPU / 4 : 1 ))
_FFMPEG_THREADS=$(( _WHISPER_THREADS / 2 > 0 ? _WHISPER_THREADS / 2 : 1 ))
SVC_CMD[transcription]="env OMP_NUM_THREADS=$_WHISPER_THREADS MKL_NUM_THREADS=$_WHISPER_THREADS OPENBLAS_NUM_THREADS=$_WHISPER_THREADS TORCH_NUM_THREADS=$_WHISPER_THREADS nice -n 19 $TRANS_VENV main.py --debug --whisper-threads $_WHISPER_THREADS --ffmpeg-threads $_FFMPEG_THREADS --channels-dir /media/sagetv/ChannelsDVR8TB/ChannelsDVR/TV /media/sagetv/ChannelsDVR8TB/ChannelsDVR/PlayOn /media/sagetv/ChannelsDVR8TB/ChannelsDVR/Movies"
SVC_LOG[transcription]="/tmp/transcription.log"
SVC_PORT[transcription]=8770

WATCHDOG_LOG_DIR="/tmp"
PIDFILE_DIR="/tmp/ai-media-rc"
ALERTS_FILE="$PIDFILE_DIR/alerts.jsonl"
ALERTS_MAX_LINES=500
mkdir -p "$PIDFILE_DIR"

ensure_orchestrator_config() {
    [[ -f "$ORCH_CONFIG" ]] || return 0

    python3 - "$ORCH_CONFIG" <<'PY'
import json
import sys
from pathlib import Path

config_path = Path(sys.argv[1])
config = json.loads(config_path.read_text(encoding="utf-8"))
llm = config.setdefault("llm", {})

# "auto" defers model choice to the GPU arbiter (gpu_arbiter.py), which picks
# the biggest/most-accurate model that fits the room VSR leaves on the card:
# qwen3:14b when the card is free, hermes3:8b as the floor when VSR is live.
llm["model"] = "auto"
llm["auto_select_model"] = True
llm["fallback_model"] = "hermes3:8b"
# Static fallback ladder, used only if the arbiter is unavailable. Ordered to
# match the arbiter ladder below.
llm["gpu_memory_profiles_mb"] = [
    {"min_mb": 12800, "model": "qwen3:14b"},
    {"min_mb": 9400, "model": "mistral-nemo:latest"},
    {"min_mb": 6144, "model": "hermes3:8b"},
]
llm["routing"] = {
    "default_model": "qwen3:14b",
    "complex_model": "qwen3:14b",
    "fast_model": "hermes3:8b",
    "premium_model": "qwen3:14b",
    "complex_min_gpu_mb": 12800,
    "premium_min_gpu_mb": 12800,
}

# GPU arbiter tiering. setdefault so hand-tuned overrides in config.json survive
# a restart, but a fresh/scrubbed config always gets the intended ladder.
# qwen2.5:32b is deliberately excluded: it spills to CPU (~7 tok/s) on a 16 GB card.
arbiter = config.setdefault("gpu_arbiter", {})
arbiter.setdefault("floor_model", "hermes3:8b")
arbiter.setdefault("model_ladder", [
    {"model": "qwen3:14b", "vram_mb": 12800, "num_ctx": 32768},
    {"model": "mistral-nemo:latest", "vram_mb": 9400, "num_ctx": 16384},
    {"model": "hermes3:8b", "vram_mb": 6800, "num_ctx": 16384},
])

config_path.write_text(json.dumps(config, indent=4) + "\n", encoding="utf-8")
PY
}

ensure_ollama_models() {
    [[ -f "$ORCH_CONFIG" ]] || return 0
    [[ -n "$OLLAMA_BIN" ]] || return 0

    python3 - "$ORCH_CONFIG" <<'PY' | while IFS= read -r model_name; do
import json
import subprocess
import sys
from pathlib import Path

config_path = Path(sys.argv[1])
config = json.loads(config_path.read_text(encoding="utf-8"))
llm = config.get("llm", {})
routing = llm.get("routing", {})


def detect_gpu_memory_mb() -> float:
    try:
        proc = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=memory.total",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        return 0.0

    if proc.returncode != 0:
        return 0.0

    values = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            values.append(float(line))
        except ValueError:
            continue
    return max(values) if values else 0.0

gpu_memory_mb = detect_gpu_memory_mb()
complex_min_gpu_mb = float(routing.get("complex_min_gpu_mb", 12800))

required = []
# The floor model is always required: the arbiter falls back to it whenever VSR
# is live, so it must be present even on a small card.
if gpu_memory_mb >= complex_min_gpu_mb:
    desired = [
        routing.get("fast_model"),
        routing.get("default_model"),
        routing.get("complex_model"),
    ]
else:
    desired = [
        routing.get("fast_model"),
    ]

for model in desired:
    if model and model not in required:
        required.append(model)

for model in required:
    print(model)
PY
        [[ -n "$model_name" ]] || continue

        if "$OLLAMA_BIN" list 2>/dev/null | awk 'NR>1 {print $1}' | grep -Fxq "$model_name"; then
            continue
        fi

        if pgrep -af "ollama pull $model_name" >/dev/null 2>&1; then
            continue
        fi

        echo "[$(date '+%F %T')] Queueing missing Ollama model pull: $model_name" >> "$OLLAMA_SYNC_LOG"
        emit_alert "orchestrator" "info" "ollama_pull" "queueing missing Ollama model pull: $model_name"
        nohup "$OLLAMA_BIN" pull "$model_name" >> "$OLLAMA_SYNC_LOG" 2>&1 </dev/null &
    done
}

# â”€â”€ Alert emission â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# Append a JSON line to alerts.jsonl. Each alert: {ts, svc, severity, code, message}
# Severities: info, warning, error, critical
emit_alert() {
    local svc="$1" severity="$2" code="$3" message="$4"
    local ts
    ts=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
    # JSON escape the message (basic: backslashes and double-quotes)
    local esc_msg=${message//\\/\\\\}
    esc_msg=${esc_msg//\"/\\\"}
    printf '{"ts":"%s","svc":"%s","severity":"%s","code":"%s","message":"%s"}\n' \
        "$ts" "$svc" "$severity" "$code" "$esc_msg" >> "$ALERTS_FILE"
    # Trim to ALERTS_MAX_LINES
    if [[ -f "$ALERTS_FILE" ]]; then
        local lines
        lines=$(wc -l < "$ALERTS_FILE" 2>/dev/null || echo 0)
        if (( lines > ALERTS_MAX_LINES )); then
            tail -n "$ALERTS_MAX_LINES" "$ALERTS_FILE" > "${ALERTS_FILE}.tmp" \
                && mv -f "${ALERTS_FILE}.tmp" "$ALERTS_FILE"
        fi
    fi
}

# â”€â”€ Log rotation â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

rotate_log() {
    local logfile="$1"
    if [[ ! -f "$logfile" ]]; then return; fi
    local size
    size=$(stat -c%s "$logfile" 2>/dev/null || echo 0)
    if (( size > MAX_LOG_BYTES )); then
        mv -f "$logfile" "${logfile}.1"
        : > "$logfile"
    fi
}

# Health check interval (seconds) â€” how often to verify the port is alive
HEALTH_CHECK_INTERVAL=30
# Consecutive health check failures before declaring dead
HEALTH_CHECK_FAILURES=3

# â”€â”€ Single service watchdog â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

run_watchdog() {
    local svc="$1"
    local dir="${SVC_DIR[$svc]}"
    local cmd="${SVC_CMD[$svc]}"
    local log="${SVC_LOG[$svc]}"
    local port="${SVC_PORT[$svc]}"
    local wlog="$WATCHDOG_LOG_DIR/${svc}-watchdog.log"
    local pidfile="$PIDFILE_DIR/${svc}.pid"

    # Track crash timestamps for crash-loop detection
    local -a crash_times=()

    echo "$BASHPID" > "$PIDFILE_DIR/${svc}-watchdog.pid"
    trap '' PIPE
    echo "[$(date '+%F %T')] Watchdog starting for $svc (port $port)" >> "$wlog"

    while true; do
        # Kill any leftover process on the port
        fuser -k "$port/tcp" 2>/dev/null || true
        sleep 1

        # Rotate logs if they've grown too large
        rotate_log "$log"
        rotate_log "$wlog"

        echo "[$(date '+%F %T')] Starting $svc ..." >> "$wlog"
        if [[ "$svc" == "orchestrator" ]]; then
            ensure_orchestrator_config
            ensure_ollama_models
        fi
        cd "$dir"

        # Start the service as a direct child
        $cmd >> "$log" 2>&1 &
        local child=$!
        echo "$child" > "$pidfile"
        echo "[$(date '+%F %T')] $svc started (pid=$child)" >> "$wlog"

        # â”€â”€ Monitor loop: check process + port health â”€â”€
        local health_fails=0
        local exit_code=""
        while true; do
            # Check if child is still alive
            if ! kill -0 "$child" 2>/dev/null; then
                wait "$child" 2>/dev/null
                exit_code=$?
                break
            fi
            # Port health check â€” verify the service is actually listening
            if ! ss -tlnH "sport = :$port" 2>/dev/null | grep -q "$port"; then
                health_fails=$((health_fails + 1))
                if (( health_fails >= HEALTH_CHECK_FAILURES )); then
                    echo "[$(date '+%F %T')] HEALTH CHECK FAILED: $svc port $port not listening after $health_fails checks â€” killing pid $child" >> "$wlog"
                    emit_alert "$svc" "warning" "health_check_failed" "port $port not listening after $health_fails checks; killing pid $child"
                    kill "$child" 2>/dev/null
                    sleep 2
                    kill -9 "$child" 2>/dev/null
                    wait "$child" 2>/dev/null
                    exit_code=1  # treat as crash
                    break
                fi
            else
                health_fails=0
            fi
            sleep "$HEALTH_CHECK_INTERVAL"
        done

        echo "[$(date '+%F %T')] $svc exited with code $exit_code" >> "$wlog"

        # Decide whether to restart based on how the process died.
        # Bash returns 128+signal when a child is killed by a signal:
        #   137 = SIGKILL (kill -9, or OOM killer)
        #   143 = SIGTERM (kill, systemctl stop)
        #   130 = SIGINT  (Ctrl-C)
        # These are almost always intentional â€” do NOT restart.
        # A real crash (segfault=139, abort=134) or non-zero app exit SHOULD restart.
        # Code 0 (clean exit) also restarts â€” services should run forever;
        # a clean exit is unexpected and likely a bug.
        case $exit_code in
            130)  # SIGINT
                echo "[$(date '+%F %T')] $svc killed by SIGINT (code 130), not restarting" >> "$wlog"
                rm -f "$pidfile"
                break
                ;;
            137)  # SIGKILL
                echo "[$(date '+%F %T')] $svc killed by SIGKILL (code 137) â€” manual kill or OOM, not restarting" >> "$wlog"
                rm -f "$pidfile"
                break
                ;;
            143)  # SIGTERM
                echo "[$(date '+%F %T')] $svc killed by SIGTERM (code 143), not restarting" >> "$wlog"
                rm -f "$pidfile"
                break
                ;;
            0)
                echo "[$(date '+%F %T')] $svc exited cleanly (code 0) â€” unexpected, will restart" >> "$wlog"
                ;;
        esac

        # All other non-zero exits are treated as crashes â†’ restart with backoff.

        # Record crash time
        local now
        now=$(date +%s)
        crash_times+=("$now")

        # Prune crashes older than the window
        local -a recent=()
        for t in "${crash_times[@]}"; do
            if (( now - t < CRASH_WINDOW )); then
                recent+=("$t")
            fi
        done
        crash_times=("${recent[@]}")

        # Check crash-loop
        if (( ${#crash_times[@]} >= MAX_CRASHES )); then
            echo "[$(date '+%F %T')] CRASH LOOP DETECTED: $svc crashed ${#crash_times[@]} times in ${CRASH_WINDOW}s â€” giving up" >> "$wlog"
            emit_alert "$svc" "critical" "crash_loop" "crashed ${#crash_times[@]} times in ${CRASH_WINDOW}s; watchdog giving up"
            rm -f "$pidfile"
            break
        fi

        echo "[$(date '+%F %T')] Restarting $svc in 5s (crash ${#crash_times[@]}/$MAX_CRASHES in window)" >> "$wlog"
        emit_alert "$svc" "warning" "restart" "restarting after crash (${#crash_times[@]}/$MAX_CRASHES in ${CRASH_WINDOW}s window, exit_code=$exit_code)"
        sleep 5
    done
}

# â”€â”€ Multi-service management â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

ALL_SERVICES=(mcp-sagetv mcp-channels mcp-linux session-manager transcription orchestrator)

start_all() {
    echo "Starting all services with watchdogs..."
    # Start MCP servers first, then orchestrator last (it depends on MCPs)
    for svc in "${ALL_SERVICES[@]}"; do
        if is_watchdog_running "$svc"; then
            echo "  $svc: already running (watchdog pid $(cat "$PIDFILE_DIR/${svc}-watchdog.pid"))"
        else
            run_watchdog "$svc" </dev/null >>"$WATCHDOG_LOG_DIR/${svc}-watchdog.log" 2>&1 &
            disown
            echo "  $svc: watchdog started"
            # Small delay between service starts
            sleep 2
        fi
    done
    echo "All services started. Check /tmp/*-watchdog.log for status."
}

stop_all() {
    echo "Stopping all watchdogs and services..."
    for svc in "${ALL_SERVICES[@]}"; do
        stop_service "$svc"
    done
    echo "All services stopped."
}

stop_service() {
    local svc="$1"
    local port="${SVC_PORT[$svc]}"

    # Kill watchdog first so it doesn't restart the service
    if [[ -f "$PIDFILE_DIR/${svc}-watchdog.pid" ]]; then
        local wpid
        wpid=$(cat "$PIDFILE_DIR/${svc}-watchdog.pid")
        kill "$wpid" 2>/dev/null && echo "  $svc: watchdog (pid $wpid) stopped"
        rm -f "$PIDFILE_DIR/${svc}-watchdog.pid"
    fi

    # Then kill the service
    if [[ -f "$PIDFILE_DIR/${svc}.pid" ]]; then
        local spid
        spid=$(cat "$PIDFILE_DIR/${svc}.pid")
        kill "$spid" 2>/dev/null && echo "  $svc: service (pid $spid) stopped"
        rm -f "$PIDFILE_DIR/${svc}.pid"
    fi

    # Belt-and-suspenders: kill anything on the port
    fuser -k "$port/tcp" 2>/dev/null || true
}

is_watchdog_running() {
    local svc="$1"
    local pidfile="$PIDFILE_DIR/${svc}-watchdog.pid"
    [[ -f "$pidfile" ]] && kill -0 "$(cat "$pidfile")" 2>/dev/null
}

status_all() {
    printf "%-18s %-8s %-8s %-6s\n" "SERVICE" "SVC-PID" "WD-PID" "PORT"
    printf "%-18s %-8s %-8s %-6s\n" "-------" "-------" "------" "----"
    for svc in "${ALL_SERVICES[@]}"; do
        local spid="-" wpid="-" port="${SVC_PORT[$svc]}"
        if [[ -f "$PIDFILE_DIR/${svc}.pid" ]]; then
            local p
            p=$(cat "$PIDFILE_DIR/${svc}.pid")
            kill -0 "$p" 2>/dev/null && spid="$p" || spid="dead"
        fi
        if [[ -f "$PIDFILE_DIR/${svc}-watchdog.pid" ]]; then
            local w
            w=$(cat "$PIDFILE_DIR/${svc}-watchdog.pid")
            kill -0 "$w" 2>/dev/null && wpid="$w" || wpid="dead"
        fi
        printf "%-18s %-8s %-8s %-6s\n" "$svc" "$spid" "$wpid" "$port"
    done
}

# â”€â”€ Main â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

case "${1:-}" in
    all)
        start_all
        ;;
    stop)
        svc="${2:-}"
        if [[ -z "$svc" ]]; then
            stop_all
        else
            if [[ -z "${SVC_DIR[$svc]+x}" ]]; then
                echo "Unknown service: $svc"
                echo "Available: ${ALL_SERVICES[*]}"
                exit 1
            fi
            stop_service "$svc"
        fi
        ;;
    status)
        status_all
        ;;
    restart)
        svc="${2:-}"
        if [[ -z "$svc" ]]; then
            stop_all
            sleep 2
            start_all
        else
            if [[ -z "${SVC_DIR[$svc]+x}" ]]; then
                echo "Unknown service: $svc"
                echo "Available: ${ALL_SERVICES[*]}"
                exit 1
            fi
            stop_service "$svc"
            sleep 2
            # Detach watchdog from parent stdio so ssh can return immediately
            run_watchdog "$svc" </dev/null >>"$WATCHDOG_LOG_DIR/${svc}-watchdog.log" 2>&1 &
            disown
            echo "$svc restarted with watchdog"
        fi
        ;;
    "")
        echo "Usage: $0 {all|stop|status|restart [service]|<service-name>}"
        echo "Services: ${ALL_SERVICES[*]}"
        exit 1
        ;;
    *)
        svc="$1"
        if [[ -z "${SVC_DIR[$svc]+x}" ]]; then
            echo "Unknown service: $svc"
            echo "Available: ${ALL_SERVICES[*]}"
            exit 1
        fi
        run_watchdog "$svc"
        ;;
esac
