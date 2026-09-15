import asyncio, json, sys
sys.path.insert(0, "/root/dev/panel")
from PasarGuardNodeBridge import create_node, NodeType
INBOUND = "Shadowsocks TCP"

def mk():
    return create_node(connection=NodeType.grpc, address="127.0.0.1", port=62950,
                       api_port=62951, server_ca=open("/var/lib/pg-node-xraytest/certs/ssl_cert.pem").read(),
                       api_key=open("/root/dev/.xraytest_key").read().strip(),
                       name="r1c", default_timeout=20, internal_timeout=25)

async def route(n, dom):
    try:
        r = await n.test_route(inbound_tag=INBOUND, network="tcp", target_domain=dom, target_port=443)
        return r.outbound_tag if r else "<none>"
    except Exception as e:
        d = str(e)
        return "UNMATCHED" if "not enough information" in d else "ERR %s" % d[:90]

async def tags(n):
    try:
        r = await n.list_routing_rules()
        return [x.rule_tag or "<untagged>" for x in r.rules] if r else []
    except Exception as e:
        return ["ERR %s" % str(e)[:90]]

async def main():
    n = mk()
    print("  tags now      : %s" % await tags(n))
    print("  example.com   : %s" % await route(n, "example.com"))
    await n.stop()

asyncio.run(main())
