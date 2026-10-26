#!/bin/bash
# Auto-restart loop for bot_runner.py with log-size protection.
# 2026-10-27: two duplicate watchdog instances ran simultaneously (each spawned a
# full bot) and bot logs grew to ~1GB each; log caps + size guards added.
cd /home/agentuser/Memesnipe

LOG_MAX_BYTES=$((512*1024*1024))  # 512MB per bot log

while true; do
  # refuse to start if disk is nearly full (bot would just OOM/kill the box)
  avail_kb=$(df --output=avail / | tail -1)
  if [ "$avail_kb" -lt 2000000 ]; then
    echo "[$(date '+%F %T')] disk low (${avail_kb}KB free) — waiting 60s"
    sleep 60
    continue
  fi
  echo "[$(date '+%F %T')] starting bot_runner"
  uv run src/bot_runner.py >> "logs/memesnipe_$(date +%Y%m%d_%H%M%S).log" 2>&1 &
  BOT_PID=$!
  echo "[$(date '+%F %T')] bot_runner pid=$BOT_PID"
  wait $BOT_PID
  echo "[$(date '+%F %T')] bot_runner exited code=$? — restarting in 10s"
  # cap today's log if the run ballooned
  LATEST=$(ls -t logs/memesnipe_*.log 2>/dev/null | head -1)
  if [ -n "$LATEST" ] && [ "$(stat -c%s "$LATEST")" -gt "$LOG_MAX_BYTES" ]; then
    tail -c 1000000 "$LATEST" > "$LATEST.tmp" && mv "$LATEST.tmp" "$LATEST"
    echo "[$(date '+%F %T')] truncated $LATEST to 1MB tail"
  fi
  # delete bot logs older than 2 days
  find logs -name 'memesnipe_*.log' -mtime +2 -delete 2>/dev/null
  sleep 10
done
