#!/usr/bin/env bash
# Start (or restart) the mock upstream + jellylook for end-to-end work.
#   tests/e2e/harness.sh start [datadir]
#   tests/e2e/harness.sh stop
set -u
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
RUN="${JELLYLOOK_E2E_DIR:-/tmp/jellylook-e2e}"
MOCK_PORT="${MOCK_PORT:-9099}"
APP_PORT="${APP_PORT:-9098}"

stop() {
  for f in "$RUN/mock.pid" "$RUN/app.pid"; do
    [ -f "$f" ] && kill "$(cat "$f")" 2>/dev/null
    rm -f "$f"
  done
  sleep 1
}

start() {
  stop
  rm -rf "$RUN"; mkdir -p "$RUN"
  cd "$ROOT"
  python3 -m tests.e2e.mock_upstream "$MOCK_PORT" > "$RUN/mock.log" 2>&1 &
  echo $! > "$RUN/mock.pid"
  python3 -m tests.e2e.run_app --mock-port "$MOCK_PORT" --app-port "$APP_PORT" \
      --data-dir "$RUN/data" > "$RUN/app.log" 2>&1 &
  echo $! > "$RUN/app.pid"
  for _ in $(seq 1 60); do
    curl -sf "http://127.0.0.1:$APP_PORT/health" >/dev/null && { echo "up on :$APP_PORT"; return 0; }
    sleep 0.5
  done
  echo "FAILED to start; logs:"; tail -30 "$RUN/app.log" "$RUN/mock.log"; return 1
}

case "${1:-start}" in
  start) start ;;
  stop)  stop; echo "stopped" ;;
  *)     echo "usage: $0 {start|stop}"; exit 2 ;;
esac
