#!/usr/bin/env bash
# Start custom-agent (REST API, port 8000) and price-and-history (port 5000) together.
# Ctrl-C stops both. Ollama must already be running; this script only warns if it isn't.
#
#   ./start.sh
#
# Optional environment:
#   CUSTOM_AGENT_DIR  path to the custom-agent checkout (default: ../custom-agent)
#   API_KEY           shared key for both apps (default: random for this run)
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
AGENT_DIR="${CUSTOM_AGENT_DIR:-$HERE/../custom-agent}"

if [[ ! -d "$AGENT_DIR" ]]; then
  echo "custom-agent not found at $AGENT_DIR (set CUSTOM_AGENT_DIR)" >&2
  exit 1
fi

# One key shared by both sides. The agent reads API_KEY; this app reads CUSTOM_AGENT_API_KEY.
export API_KEY="${API_KEY:-$(openssl rand -hex 16)}"
export CUSTOM_AGENT_API_KEY="$API_KEY"
export CUSTOM_AGENT_URL="http://127.0.0.1:8000"

if ! curl -fsS --max-time 2 http://127.0.0.1:11434/api/tags >/dev/null 2>&1; then
  echo "warning: Ollama isn't answering on 127.0.0.1:11434; news jobs will pause until it is." >&2
fi

pids=()
cleanup() {
  trap - INT TERM EXIT
  for pid in "${pids[@]}"; do kill "$pid" 2>/dev/null || true; done
  wait 2>/dev/null || true
}
trap cleanup INT TERM EXIT

(cd "$AGENT_DIR" && exec uv run uvicorn server:get_app --factory --host 127.0.0.1 --port 8000) &
pids+=($!)

(cd "$HERE" && exec uv run backend/app.py) &
pids+=($!)

echo "custom-agent: http://127.0.0.1:8000   price-and-history: http://127.0.0.1:5000   (Ctrl-C to stop)"

# Exit as soon as either app dies, so a crashed agent doesn't go unnoticed.
while kill -0 "${pids[0]}" 2>/dev/null && kill -0 "${pids[1]}" 2>/dev/null; do
  sleep 1
done
echo "one of the apps exited; stopping the other" >&2
