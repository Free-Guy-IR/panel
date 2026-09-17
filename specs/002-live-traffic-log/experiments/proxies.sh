#!/usr/bin/env bash
set -uo pipefail

ACTION="${1:-status}"
PANEL_ROOT="${PANEL_ROOT:-/root/dev/panel}"
BASE="${PANEL_URL:-http://127.0.0.1:8001}"
TOKEN_FILE="${TOKEN_FILE:-/root/dev/.filter_token}"
CONTAINER="${NODE_CONTAINER:-node-xray-filtertest}"
PY="${PANEL_PYTHON:-$PANEL_ROOT/.venv/bin/python}"
KIDS_PORT="${KIDS_PORT:-10821}"
OPEN_PORT="${OPEN_PORT:-10822}"
KIDS_CONF=/tmp/tl_kids.json
OPEN_CONF=/tmp/tl_open.json
LEGACY_CONFS="/tmp/k.json /tmp/o.json"
T="$(tr -d '[:space:]' <"$TOKEN_FILE")"

creds() {
  SUB=$(curl -s -H "Authorization: Bearer $T" --max-time 20 "$BASE/api/user/$1" | "$PY" -c "import sys,json;print(json.load(sys.stdin)['subscription_url'])")
  curl -s --max-time 20 -H "User-Agent: v2rayNG/1.8.0" "$BASE$SUB" | "$PY" -c "
import sys, base64
raw = sys.stdin.read().strip()
try: raw = base64.b64decode(raw + '='*(-len(raw)%4)).decode()
except Exception: pass
for line in raw.splitlines():
    if line.startswith('ss://'):
        b = line[5:].split('#')[0]; ui,_,hp = b.partition('@')
        ui = base64.b64decode(ui + '='*(-len(ui)%4)).decode()
        m,_,p = ui.partition(':'); h,_,pt = hp.partition(':')
        print('%s|%s|%s|%s' % (m,p,h,pt)); break"
}

mk() { cat > "$5" <<JSON
{"log":{"loglevel":"warning"},
 "inbounds":[{"tag":"sk","port":$4,"listen":"127.0.0.1","protocol":"socks","settings":{"udp":true}}],
 "outbounds":[{"protocol":"shadowsocks","settings":{"servers":[{"address":"$3","port":$6,"method":"$1","password":"$2"}]}}]}
JSON
}

listening() {
  ss -tln 2>/dev/null | grep -oE "127\.0\.0\.1:($KIDS_PORT|$OPEN_PORT)\b" | sort -u | grep -c .
}

holders() {
  ss -tlnp 2>/dev/null | grep -E "127\.0\.0\.1:($KIDS_PORT|$OPEN_PORT)\b" | grep -oE "pid=[0-9]+" | sort -u | grep -c .
}

stop_proxies() {
  local signal conf
  for signal in TERM TERM KILL KILL; do
    [ "$(holders)" -eq 0 ] && break
    for conf in "$KIDS_CONF" "$OPEN_CONF" $LEGACY_CONFS; do
      docker exec -e "PG_PROXY_PATTERN=xray -c $conf" -e "PG_PROXY_SIGNAL=$signal" "$CONTAINER" \
        sh -c 'pkill -"$PG_PROXY_SIGNAL" -f "$PG_PROXY_PATTERN"' 2>/dev/null
    done
    sleep 2
  done
  docker exec "$CONTAINER" sh -c "rm -f $KIDS_CONF $OPEN_CONF" 2>/dev/null
  rm -f "$KIDS_CONF" "$OPEN_CONF"
}

case "$ACTION" in
  up)
    stop_proxies
    if [ "$(holders)" -ne 0 ]; then
      echo "refusing to start: $(holders) process(es) still hold $KIDS_PORT/$OPEN_PORT"
      exit 1
    fi
    K=$(creds demo-kids); O=$(creds demo-open)
    if [ -z "$K" ] || [ -z "$O" ]; then
      echo "could not read the demo subscriptions; is the panel up and the token valid?"
      exit 1
    fi
    mk "$(echo "$K"|cut -d'|' -f1)" "$(echo "$K"|cut -d'|' -f2)" "$(echo "$K"|cut -d'|' -f3)" "$KIDS_PORT" "$KIDS_CONF" "$(echo "$K"|cut -d'|' -f4)"
    mk "$(echo "$O"|cut -d'|' -f1)" "$(echo "$O"|cut -d'|' -f2)" "$(echo "$O"|cut -d'|' -f3)" "$OPEN_PORT" "$OPEN_CONF" "$(echo "$O"|cut -d'|' -f4)"
    docker cp "$KIDS_CONF" "$CONTAINER:$KIDS_CONF" >/dev/null
    docker cp "$OPEN_CONF" "$CONTAINER:$OPEN_CONF" >/dev/null
    docker exec -d "$CONTAINER" sh -c "XRAY_LOCATION_ASSET=/var/lib/pg-node/assets /usr/local/bin/xray -c $KIDS_CONF >/dev/null 2>&1"
    docker exec -d "$CONTAINER" sh -c "XRAY_LOCATION_ASSET=/var/lib/pg-node/assets /usr/local/bin/xray -c $OPEN_CONF >/dev/null 2>&1"
    sleep 4
    started="$(holders)"
    echo "demo socks proxies listening: $(listening)/2 on $KIDS_PORT demo-kids and $OPEN_PORT demo-open, held by $started process(es)"
    [ "$(listening)" -eq 2 ] && [ "$started" -eq 2 ]
    ;;
  down)
    stop_proxies
    left="$(holders)"
    echo "demo socks proxies still listening: $(listening), processes still holding the ports: $left"
    [ "$left" -eq 0 ]
    ;;
  status)
    echo "demo socks proxies listening: $(listening)/2"
    [ "$(listening)" -ge 2 ]
    ;;
  *)
    echo "usage: $0 up|down|status"
    exit 2
    ;;
esac
