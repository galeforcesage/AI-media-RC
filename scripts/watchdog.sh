#!/bin/bash
# watchdog.sh — Auto-restart wrapper for AI-media-RC services.
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

ROOT="/home/sagetv/AI-media-RC"
ORCH_VENV="$ROOT/backend/orchestrator/.venv/bin/python"
TRANS_VENV="$ROOT/backend/transcription/.venv/bin/python"

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
mkdir -p "$PIDFILE_DIR"

# ── Log rotation ─────────────────────────────────────────────────────

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

# Health check interval (seconds) — how often to verify the port is alive
HEALTH_CHECK_INTERVAL=30
# Consecutive health check failures before declaring dead
HEALTH_CHECK_FAILURES=3

# ── Single service watchdog ──────────────────────────────────────────

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
        cd "$dir"

        # Start the service as a direct child
        $cmd >> "$log" 2>&1 &
        local child=$!
        echo "$child" > "$pidfile"
        echo "[$(date '+%F %T')] $svc started (pid=$child)" >> "$wlog"

        # ── Monitor loop: check process + port health ──
        local health_fails=0
        local exit_code=""
        while true; do
            # Check if child is still alive
            if ! kill -0 "$child" 2>/dev/null; then
                wait "$child" 2>/dev/null
                exit_code=$?
                break
            fi
            # Port health check — verify the service is actually listening
            if ! ss -tlnH "sport = :$port" 2>/dev/null | grep -q "$port"; then
                health_fails=$((health_fails + 1))
                if (( health_fails >= HEALTH_CHECK_FAILURES )); then
                    echo "[$(date '+%F %T')] HEALTH CHECK FAILED: $svc port $port not listening after $health_fails checks — killing pid $child" >> "$wlog"
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
        # These are almost always intentional — do NOT restart.
        # A real crash (segfault=139, abort=134) or non-zero app exit SHOULD restart.
        # Code 0 (clean exit) also restarts — services should run forever;
        # a clean exit is unexpected and likely a bug.
        case $exit_code in
            130)  # SIGINT
                echo "[$(date '+%F %T')] $svc killed by SIGINT (code 130), not restarting" >> "$wlog"
                rm -f "$pidfile"
                break
                ;;
            137)  # SIGKILL
                echo "[$(date '+%F %T')] $svc killed by SIGKILL (code 137) — manual kill or OOM, not restarting" >> "$wlog"
                rm -f "$pidfile"
                break
                ;;
            143)  # SIGTERM
                echo "[$(date '+%F %T')] $svc killed by SIGTERM (code 143), not restarting" >> "$wlog"
                rm -f "$pidfile"
                break
                ;;
            0)
                echo "[$(date '+%F %T')] $svc exited cleanly (code 0) — unexpected, will restart" >> "$wlog"
                ;;
        esac

        # All other non-zero exits are treated as crashes → restart with backoff.

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
            echo "[$(date '+%F %T')] CRASH LOOP DETECTED: $svc crashed ${#crash_times[@]} times in ${CRASH_WINDOW}s — giving up" >> "$wlog"
            rm -f "$pidfile"
            break
        fi

        echo "[$(date '+%F %T')] Restarting $svc in 5s (crash ${#crash_times[@]}/$MAX_CRASHES in window)" >> "$wlog"
        sleep 5
    done
}

# ── Multi-service management ─────────────────────────────────────────

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

# ── Main ─────────────────────────────────────────────────────────────

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
