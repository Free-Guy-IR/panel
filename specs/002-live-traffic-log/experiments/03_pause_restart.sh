#!/usr/bin/env bash
set -uo pipefail

RESTART_CMD="${1:-bash /root/dev/tl_restart.sh}"
BASE="${PANEL_URL:-http://127.0.0.1:8001}"
TOKEN_FILE="${TOKEN_FILE:-/root/dev/.filter_token}"
NODE_ID="${NODE_ID:-5}"
KIDS_PORT="${KIDS_PORT:-10821}"
PROBE_HOST="${PROBE_HOST:-www.wikipedia.org}"
PYTHON_BIN="${PYTHON_BIN:-python3}"
LIVE_SECONDS=3
RAW_SECONDS=8
ENABLE_DEADLINE="${ENABLE_DEADLINE:-90}"
RESTART_DEADLINE=60
RESTART_POLL_DEADLINE=90
NO_REPORTS_DEADLINE="${NO_REPORTS_DEADLINE:-120}"
RECOVER_DEADLINE="${RECOVER_DEADLINE:-90}"
TOKEN="$(tr -d '[:space:]' <"$TOKEN_FILE")"
WORK="$(mktemp -d)"
FAILURES=()

trap 'rm -rf "$WORK"' EXIT

cat >"$WORK/read.py" <<'PY'
import json
import sys


def load(path):
    try:
        with open(path) as handle:
            payload = json.load(handle)
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


payload = load(sys.argv[1])
mode = sys.argv[2]
nodes = payload.get("nodes") or []
if mode == "states":
    print(",".join(sorted({str(node.get("state")) for node in nodes})) or "no-nodes")
elif mode == "node-state":
    wanted = [node for node in nodes if node.get("node_id") == int(sys.argv[3])]
    print(wanted[0].get("state") if wanted else "absent")
elif mode == "node-field":
    wanted = [node for node in nodes if node.get("node_id") == int(sys.argv[3])]
    value = wanted[0].get(sys.argv[4]) if wanted else None
    print(value if isinstance(value, str) else json.dumps(value))
else:
    value = payload.get(mode)
    print(value if isinstance(value, str) else json.dumps(value))
PY

check() {
    if [ "$2" = "$3" ]; then
        printf "  %-62s OK\n" "$1"
    else
        printf "  %-62s FAIL got=%s want=%s\n" "$1" "$2" "$3"
        FAILURES+=("$1")
    fi
}

finish() {
    echo
    if [ "${#FAILURES[@]}" -gt 0 ]; then
        printf "FAILED %d check(s): %s\n" "${#FAILURES[@]}" "${FAILURES[*]}"
        exit 1
    fi
    echo "ALL pause-restart CHECKS PASSED"
    exit 0
}

field() {
    "$PYTHON_BIN" "$WORK/read.py" "$1" "$2" "${3:-0}"
}

node_state() {
    field "$1" node-state "$NODE_ID"
}

node_field() {
    "$PYTHON_BIN" "$WORK/read.py" "$1" node-field "$NODE_ID" "$2"
}

fetch_status() {
    curl -s -o "$1" -w '%{http_code}' -H "Authorization: Bearer $TOKEN" "$BASE/api/traffic-log/status"
}

put_settings() {
    curl -s -o "$1" -w '%{http_code}' -X PUT \
        -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
        -d "$2" "$BASE/api/traffic-log/settings"
}

probe_once() {
    curl -s -o /dev/null --socks5-hostname "127.0.0.1:$KIDS_PORT" --max-time 12 "https://$PROBE_HOST" || true
}

wait_for_collecting() {
    local deadline="$1" started now
    started="$(date +%s)"
    while :; do
        probe_once
        if [ "$(fetch_status "$WORK/poll.json")" = "200" ] && [ "$(node_state "$WORK/poll.json")" = "collecting" ]; then
            echo $(($(date +%s) - started))
            return 0
        fi
        now="$(date +%s)"
        if [ $((now - started)) -ge "$deadline" ]; then
            echo $((now - started))
            return 1
        fi
        sleep 2
    done
}

wait_for_node_state() {
    local wanted="$1" deadline="$2" started now
    started="$(date +%s)"
    while :; do
        if [ "$(fetch_status "$WORK/poll.json")" = "200" ] && [ "$(node_state "$WORK/poll.json")" = "$wanted" ]; then
            echo $(($(date +%s) - started))
            return 0
        fi
        now="$(date +%s)"
        if [ $((now - started)) -ge "$deadline" ]; then
            echo $((now - started))
            return 1
        fi
        sleep 5
    done
}

yes_no() {
    if [ "$1" -eq 0 ]; then echo yes; else echo no; fi
}

echo "=== preconditions ==="
code="$(fetch_status "$WORK/before.json")"
check "GET /api/traffic-log/status -> 200" "$code" "200"
if [ "$code" != "200" ]; then
    finish
fi
echo "    node $NODE_ID state before the run: $(node_state "$WORK/before.json") (enabled=$(field "$WORK/before.json" enabled))"
check "collector available" "$(field "$WORK/before.json" available)" "true"
if [ "$(field "$WORK/before.json" available)" != "true" ]; then
    finish
fi

echo
echo "=== stage 1: pause collection ==="
code="$(put_settings "$WORK/paused.json" '{"enabled":false}')"
check 'PUT /settings {"enabled":false} -> 200' "$code" "200"
check "paused status reports enabled false" "$(field "$WORK/paused.json" enabled)" "false"
check "every node state is paused" "$(field "$WORK/paused.json" states)" "paused"

echo
echo "=== stage 2: the live feed announces the pause (${LIVE_SECONDS}s) ==="
curl -sN --max-time "$LIVE_SECONDS" -H "Authorization: Bearer $TOKEN" "$BASE/api/traffic-log/live" >"$WORK/live.txt"
paused_line="$(grep '"control"' "$WORK/live.txt" | grep -m1 '"paused"')"
echo "    live line: ${paused_line:-<none>}"
check "live feed emits a paused control message" "$([ -n "$paused_line" ] && echo present || echo absent)" "present"

echo
echo "=== stage 3: the raw node log viewer keeps working while paused (${RAW_SECONDS}s) ==="
curl -sN --max-time "$RAW_SECONDS" -H "Authorization: Bearer $TOKEN" "$BASE/api/node/$NODE_ID/logs" >"$WORK/raw.txt" &
raw_pid=$!
sleep 1
curl -s -o /dev/null --socks5-hostname "127.0.0.1:$KIDS_PORT" --max-time 12 "https://$PROBE_HOST"
wait "$raw_pid"
lines="$(wc -l <"$WORK/raw.txt" | tr -d ' ')"
accepted="$(grep -c 'accepted tcp:' "$WORK/raw.txt")"
probe_line="$(grep -m1 "accepted tcp:$PROBE_HOST:" "$WORK/raw.txt")"
echo "    raw viewer produced $lines lines, $accepted of them access lines"
echo "    probe line: ${probe_line:-<none>}"
check "raw viewer still streams accepted tcp: lines while paused" "$([ "$accepted" -ge 1 ] && echo yes || echo no)" "yes"
check "raw viewer shows the connection made through $KIDS_PORT" "$([ -n "$probe_line" ] && echo present || echo absent)" "present"

echo
echo "=== stage 4: resume collection ==="
code="$(put_settings "$WORK/resumed.json" '{"enabled":true}')"
check 'PUT /settings {"enabled":true} -> 200' "$code" "200"
check "resumed status reports enabled true" "$(field "$WORK/resumed.json" enabled)" "true"
elapsed="$(wait_for_collecting "$ENABLE_DEADLINE")"
rc=$?
echo "    node $NODE_ID reached state $(node_state "$WORK/poll.json") after ${elapsed}s"
check "node $NODE_ID collecting within ${ENABLE_DEADLINE}s (${elapsed}s)" "$(yes_no "$rc")" "yes"

echo
echo "=== stage 5: panel restart ==="
echo "    running: $RESTART_CMD"
restart_started="$(date +%s)"
eval "$RESTART_CMD" >"$WORK/restart.log" 2>&1
restart_rc=$?
sed 's/^/    /' <"$WORK/restart.log" | tail -n 5
check "restart command exited 0" "$restart_rc" "0"
elapsed="$(wait_for_collecting "$RESTART_POLL_DEADLINE")"
rc=$?
total=$(($(date +%s) - restart_started))
echo "    node $NODE_ID state $(node_state "$WORK/poll.json"), ${elapsed}s of polling, ${total}s since the restart began"
check "node $NODE_ID collecting again after the restart" "$(yes_no "$rc")" "yes"
check "collection resumed within ${RESTART_DEADLINE}s (${total}s)" "$([ "$total" -le "$RESTART_DEADLINE" ] && echo yes || echo no)" "yes"

echo
echo "=== stage 6: a node that reports no destinations shows no_reports (FR-011) ==="
fetch_status "$WORK/idle0.json" >/dev/null
lines0="$(node_field "$WORK/idle0.json" lines)"
events0="$(node_field "$WORK/idle0.json" events)"
echo "    node $NODE_ID after the restart: state=$(node_state "$WORK/idle0.json") lines=$lines0 events=$events0"
if [ "${lines0:-0}" -eq 0 ]; then
    echo "    the node has produced no line yet; driving one connection so it has something to pass through"
    curl -s -o /dev/null --socks5-hostname "127.0.0.1:$KIDS_PORT" --max-time 12 "https://$PROBE_HOST"
    sleep 3
    fetch_status "$WORK/idle0.json" >/dev/null
    lines0="$(node_field "$WORK/idle0.json" lines)"
    echo "    lines after the single probe: $lines0"
fi
check "node $NODE_ID has seen at least one log line" "$([ "${lines0:-0}" -ge 1 ] && echo yes || echo no)" "yes"
echo "    leaving the node idle for up to ${NO_REPORTS_DEADLINE}s (no traffic is driven through it)"
elapsed="$(wait_for_node_state no_reports "$NO_REPORTS_DEADLINE")"
rc=$?
echo "    node $NODE_ID state $(node_state "$WORK/poll.json") after ${elapsed}s of idling"
check "idle node reports no_reports within ${NO_REPORTS_DEADLINE}s (${elapsed}s)" "$(yes_no "$rc")" "yes"

echo
echo "=== stage 7: one destination report puts the node back to collecting ==="
curl -s -o /dev/null --socks5-hostname "127.0.0.1:$KIDS_PORT" --max-time 12 "https://$PROBE_HOST"
elapsed="$(wait_for_node_state collecting "$RECOVER_DEADLINE")"
rc=$?
fetch_status "$WORK/after.json" >/dev/null
echo "    node $NODE_ID state $(node_state "$WORK/after.json"), events=$(node_field "$WORK/after.json" events)"
check "node $NODE_ID collecting again within ${RECOVER_DEADLINE}s (${elapsed}s)" "$(yes_no "$rc")" "yes"

finish
