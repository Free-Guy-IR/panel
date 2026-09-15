import asyncio, json, sys
sys.path.insert(0, "/root/dev/panel")

from PasarGuardNodeBridge import create_node, NodeType

ADDR = "127.0.0.1"
PORT = 62950
CERT = "/var/lib/pg-node-xraytest/certs/ssl_cert.pem"
KEYF = "/root/dev/.xraytest_key"

BLOCKED_DOMAIN = "www.pornhub.com"
NEUTRAL_DOMAIN = "www.wikipedia.org"
INBOUND = "Shadowsocks TCP"


def show(label, r):
    print("  %-34s %s" % (label, r))


async def route(node, domain, tag=INBOUND):
    try:
        r = await node.test_route(inbound_tag=tag, network="tcp",
                                  target_domain=domain, target_port=443)
        return r.outbound_tag if r else "<none>"
    except Exception as e:
        return "ERR %s" % e


async def rules(node):
    try:
        r = await node.list_routing_rules()
        if not r:
            return []
        return [(x.rule_tag or "<untagged>", (x.outbound_tag or "")) for x in r.rules]
    except Exception as e:
        return ["ERR %s" % e]


async def main():
    key = open(KEYF).read().strip()
    node = create_node(connection=NodeType.grpc, address=ADDR, port=PORT,
                       server_ca=open(CERT).read(), api_key=key,
                       api_port=62951, name="r1", default_timeout=20, internal_timeout=25)

    print("=== 0. what backends are running ===")
    try:
        b = await node.list_backends()
        show("backends", [(x.type, x.running if hasattr(x, "running") else "?") for x in b.backends] if b else None)
    except Exception as e:
        show("list_backends", "ERR %s" % e)

    print("\n=== 1. baseline routing rules ===")
    base = await rules(node)
    for t, o in base if base and not isinstance(base[0], str) else []:
        print("    rule tag=%-22s -> %s" % (t, o))
    if base and isinstance(base[0], str):
        print("   ", base[0])
    print("    count: %d" % len(base))

    print("\n=== 2. baseline TestRoute (nothing added yet) ===")
    show("blocked-domain candidate", await route(node, BLOCKED_DOMAIN))
    show("neutral domain", await route(node, NEUTRAL_DOMAIN))

    print("\n=== 3. APPEND a block rule (should_reset=False) ===")
    rule = {
        "type": "field",
        "ruleTag": "flt-test-1",
        "inboundTag": [INBOUND],
        "domain": ["domain:pornhub.com"],
        "outboundTag": "BLOCK",
    }
    try:
        await node.add_routing_rule(rule=json.dumps(rule), should_reset=False)
        show("add_routing_rule", "OK")
    except Exception as e:
        show("add_routing_rule", "ERR %s" % e)

    print("\n=== 4. rules after append — WHERE did it land? ===")
    after = await rules(node)
    for i, (t, o) in enumerate(after if after and not isinstance(after[0], str) else []):
        print("    [%d] tag=%-22s -> %s" % (i, t, o))
    if after and isinstance(after[0], str):
        print("   ", after[0])

    print("\n=== 5. did it actually take effect, with NO restart? ===")
    show("blocked domain now routes to", await route(node, BLOCKED_DOMAIN))
    show("neutral domain still routes to", await route(node, NEUTRAL_DOMAIN))

    print("\n=== 6. remove it by tag ===")
    try:
        await node.remove_routing_rule(rule_tag="flt-test-1")
        show("remove_routing_rule", "OK")
    except Exception as e:
        show("remove_routing_rule", "ERR %s" % e)
    show("rule count after removal", len(await rules(node)))
    show("blocked domain routes to", await route(node, BLOCKED_DOMAIN))

    await node.stop()

asyncio.run(main())
