__all__ = [
    "ConnectionRestriction",
    "ContentFilterAssignment",
    "ContentFilterProfile",
    "ContentFilterSniffingOverride",
    "NodeInboundUsage",
    "UserConnectionLimit",
    "UserConnectionState",
    "node_additional_cores_association",
]


def __getattr__(name):
    if name == "node_additional_cores_association":
        from app.fork.models.node_additional_cores import node_additional_cores_association

        return node_additional_cores_association
    if name == "NodeInboundUsage":
        from app.fork.models.node_inbound_usage import NodeInboundUsage

        return NodeInboundUsage
    if name in {"ContentFilterProfile", "ContentFilterAssignment", "ContentFilterSniffingOverride"}:
        from app.fork.models.content_filter import (
            ContentFilterAssignment,
            ContentFilterProfile,
            ContentFilterSniffingOverride,
        )

        return {
            "ContentFilterProfile": ContentFilterProfile,
            "ContentFilterAssignment": ContentFilterAssignment,
            "ContentFilterSniffingOverride": ContentFilterSniffingOverride,
        }[name]
    if name in {"ConnectionRestriction", "UserConnectionLimit", "UserConnectionState"}:
        from app.fork.models.connection import ConnectionRestriction, UserConnectionLimit, UserConnectionState

        return {
            "ConnectionRestriction": ConnectionRestriction,
            "UserConnectionLimit": UserConnectionLimit,
            "UserConnectionState": UserConnectionState,
        }[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
