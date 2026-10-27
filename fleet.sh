#!/bin/bash
# Memesnipe fleet supervisor — one child per line, each restarted if it dies.
# Used by memesnipe-fleet.service (systemd user unit) so session teardowns
# and individual crashes don't take the fleet down.
export PATH="/home/agentuser/.local/bin:$PATH"
cd /home/agentuser/Memesnipe

run() {  # run <name> <cmd...> — restart loop with 10s backoff
  local name="$1"; shift
  while true; do
    "$@" >> "logs/${name}.log" 2>&1
    echo "[$(date '+%F %T')] ${name} exited code=$? — restarting in 10s" >> logs/fleet.log
    sleep 10
  done
}

run pumpapi_sidecar uv run python -u tools/pumpapi_sidecar.py &
run graduation_monitor uv run python -u tools/graduation_monitor.py &
run paper_copy uv run python -u tools/paper_copy.py &
run bot bash watchdog.sh &
while true; do  # rug-memory ingest every 30 min
  uv run python tools/rug_memory.py --ingest-shadow --ingest-grads >> logs/rug_memory.log 2>&1
  sleep 1800
done &
wait -n
