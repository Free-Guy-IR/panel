set -uo pipefail
cd /root/dev/panel
T=$(cat /root/dev/.filter_token)
creds() {
  SUB=$(curl -s -H "Authorization: Bearer $T" --max-time 20 "http://127.0.0.1:8001/api/user/$1" | .venv/bin/python -c "import sys,json;print(json.load(sys.stdin)['subscription_url'])")
  curl -s --max-time 20 -H "User-Agent: v2rayNG/1.8.0" "http://127.0.0.1:8001$SUB" | .venv/bin/python -c "
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
K=$(creds demo-kids); O=$(creds demo-open)
mk "$(echo $K|cut -d'|' -f1)" "$(echo $K|cut -d'|' -f2)" "$(echo $K|cut -d'|' -f3)" 10821 /tmp/k.json "$(echo $K|cut -d'|' -f4)"
mk "$(echo $O|cut -d'|' -f1)" "$(echo $O|cut -d'|' -f2)" "$(echo $O|cut -d'|' -f3)" 10822 /tmp/o.json "$(echo $O|cut -d'|' -f4)"
docker cp /tmp/k.json node-xray-filtertest:/tmp/k.json >/dev/null
docker cp /tmp/o.json node-xray-filtertest:/tmp/o.json >/dev/null
docker exec -d node-xray-filtertest sh -c "XRAY_LOCATION_ASSET=/var/lib/pg-node/assets /usr/local/bin/xray -c /tmp/k.json >/dev/null 2>&1"
docker exec -d node-xray-filtertest sh -c "XRAY_LOCATION_ASSET=/var/lib/pg-node/assets /usr/local/bin/xray -c /tmp/o.json >/dev/null 2>&1"
sleep 4

fail=0
verdict() {
  # $1 site  $2 expected-on-restricted (allow|block)
  R=$(curl -s -o /dev/null --socks5-hostname 127.0.0.1:10821 --max-time 15 -w "%{http_code}" "https://$1" 2>/dev/null); R=${R:-000}
  O=$(curl -s -o /dev/null --socks5-hostname 127.0.0.1:10822 --max-time 15 -w "%{http_code}" "https://$1" 2>/dev/null); O=${O:-000}
  if [ "$O" = "000" ]; then
    status="INCONCLUSIVE (open endpoint also failed -> not a filter verdict)"; fail=1
  elif [ "$2" = "block" ]; then
    if [ "$R" = "000" ]; then status="OK   filtered (open=$O proves the tunnel works)"; else status="FAIL expected block, got $R"; fail=1; fi
  else
    if [ "$R" != "000" ]; then status="OK   allowed ($R)"; else status="FAIL expected allow, got 000"; fail=1; fi
  fi
  printf "  %-24s restricted=%-4s open=%-4s  %s\n" "$1" "$R" "$O" "$status"
}
printf "  %-24s %-15s %-9s %s\n" "site" "" "" "verdict"
verdict www.google.com        allow
verdict www.wikipedia.org     allow
verdict sub.khanacademy.org   allow
verdict www.pornhub.com       block
verdict www.instagram.com     block
verdict dns.google            block
verdict example.org           block

docker exec node-xray-filtertest sh -c "pkill -f /tmp/k.json; pkill -f /tmp/o.json; rm -f /tmp/k.json /tmp/o.json" 2>/dev/null
rm -f /tmp/k.json /tmp/o.json; sleep 1
echo "  stray test sockets: $(ss -tln 2>/dev/null | grep -cE '1082[12]')"
echo
[ "$fail" = 0 ] && echo "TRAFFIC MATRIX: ALL PASSED" || { echo "TRAFFIC MATRIX: FAILURES PRESENT"; exit 1; }
