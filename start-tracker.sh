#!/bin/bash
# Starts the local tracker if it is not already running, then opens it.
ROOT="$(cd "$(dirname "$0")" && pwd)"
URL="http://127.0.0.1:8765/"
LOG="$ROOT/tracker.log"
echo "$(date) launcher start root=$ROOT" >>"$LOG"

pick_python() {
  local candidate
  for candidate in \
    "/Library/Frameworks/Python.framework/Versions/Current/bin/python3" \
    "/opt/homebrew/bin/python3" \
    "/usr/local/bin/python3" \
    "$(command -v python3 2>/dev/null)" \
    "/usr/bin/python3"
  do
    if [ -n "$candidate" ] && [ -x "$candidate" ]; then
      if "$candidate" -c "import certifi" >/dev/null 2>&1; then
        echo "$candidate"
        return
      fi
    fi
  done
  command -v python3 2>/dev/null || echo "/usr/bin/python3"
}

if curl -sf -o /dev/null --max-time 1 "$URL"; then
  open "$URL"
  exit 0
fi

PYTHON="$(pick_python)"
cd "$ROOT" || exit 1
SKIP_BROWSER=1 nohup "$PYTHON" "$ROOT/app.py" >>"$LOG" 2>&1 &

for _ in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20; do
  if curl -sf -o /dev/null --max-time 1 "$URL"; then
    open "$URL"
    exit 0
  fi
  sleep 0.3
done

osascript -e "display alert \"Portfolio Tracker\" message \"The site did not start. The log is at $LOG\""
exit 1
