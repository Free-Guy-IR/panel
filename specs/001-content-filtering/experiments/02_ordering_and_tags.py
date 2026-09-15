import asyncio, json, sys
sys.path.insert(0, "/root/dev/panel")
from PasarGuardNodeBridge import create_node, NodeType

INBOUND = "Shadowsocks TCP"


def mk():
    return create_node(connection=NodeType.grpc, address="127.0.0.1", port=62950,
                       api_port=62951, server_ca=open("/var/lib/pg-node-xraytest/certs/ssl_cert.pem").read(),
                       api_key=open("/root/dev/.xraytest_key").read().strip(),
                       name="r1b", default_timeout=20, internal_timeout=25)


async def route(n, dom):
    try:
        r = await n.test_route(inbound_tag=INBOUND, network="tcp", target_domain=dom, target_port=443)
        return r.outbound_tag if r else "<none>"
    except Exception as e:
        d = str(e)
        return "UNMATCHED" if "not enough information" in d else "ERR %s" % d


async def tags(n):
    try:
        r = await n.list_routing_rules()
        return [x.rule_tag or "<untagged>" for x in r.rules] if r else []
    except Exception as e:
        return ["ERR %s" % e]


async def main():
    n = mk()

    print("=== A. ORDERING: does an appended rule lose to an earlier one? ===")
    first = {"type": "field", "ruleTag": "flt-allow-first", "inboundTag": [INBOUND],
             "domain": ["domain:example.com"], "outboundTag": "DIRECT"}
    second = {"type": "field", "ruleTag": "flt-block-second", "inboundTag": [INBOUND],
              "domain": ["domain:example.com"], "outboundTag": "BLOCK"}
    await n.add_routing_rule(rule=json.dumps(first), should_reset=False)
    await n.add_routing_rule(rule=json.dumps(second), should_reset=False)
    print("    order now: %s" % await tags(n))
    print("    example.com -> %s   (DIRECT proves first-match-wins, BLOCK would disprove it)" % await route(n, "example.com"))

    print("\n=== B. remove the earlier one, does the later now win? ===")
    await n.remove_routing_rule(rule_tag="flt-allow-first")
    print("    order now: %s" % await tags(n))
    print("    example.com -> %s" % await route(n, "example.com"))

    print("\n=== C. re-adding a tag that already exists ===")
    try:
        await n.add_routing_rule(rule=json.dumps(second), should_reset=False)
        print("    duplicate tag accepted; order: %s" % await tags(n))
    except Exception as e:
        print("    duplicate rejected: %s" % str(e)[:120])

    print("\n=== D. removing a tag that does not exist ===")
    try:
        await n.remove_routing_rule(rule_tag="flt-does-not-exist")
        print("    silently accepted (no error)")
    except Exception as e:
        print("    error: %s" % str(e)[:140])

    print("\n=== E. state before restart ===")
    print("    tags: %s" % await tags(n))
    print("    example.com -> %s" % await route(n, "example.com"))
    await n.stop()

asyncio.run(main())
