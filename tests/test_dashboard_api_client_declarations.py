import re
from collections import Counter
from pathlib import Path

API_CLIENT = Path(__file__).resolve().parents[1] / "dashboard" / "src" / "service" / "api" / "index.ts"

DECLARATION = re.compile(r"^export (const|type|interface) ([A-Za-z_$][\w$]*)\b", re.MULTILINE)


def _declarations(kinds):
    return Counter(name for kind, name in DECLARATION.findall(API_CLIENT.read_text()) if kind in kinds)


def test_generated_api_client_declares_each_value_once():
    duplicates = sorted(name for name, count in _declarations({"const"}).items() if count > 1)
    assert duplicates == []


def test_generated_api_client_declares_each_type_once():
    duplicates = sorted(name for name, count in _declarations({"type", "interface"}).items() if count > 1)
    assert duplicates == []


def test_generated_api_client_carries_the_outbound_usage_client():
    text = API_CLIENT.read_text()
    for symbol in (
        "export interface NodeOutboundUsageStatsList ",
        "export type GetOutboundUsageParams = ",
        "export const getOutboundUsage = ",
        "export function useGetOutboundUsage<",
        "  record_outbound_usage?: boolean;",
        "  node_outbound_usages: 'node_outbound_usages',",
    ):
        assert symbol in text
