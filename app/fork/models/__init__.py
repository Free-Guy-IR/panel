from app.fork.models.named_lock import NamedLock
from app.fork.models.node_retirement import NodeRetirement
from app.fork.models.node_stat_ack import NodeStatAck
from app.fork.models.pending_node_removal import PendingNodeRemoval
from app.fork.models.pending_user_resync import PendingUserResync
from app.fork.models.proxy_secret_claim import ProxySecretClaim

__all__ = [
    "ConnectionRestriction",
    "NamedLock",
    "NodeInboundUsage",
    "NodeRetirement",
    "NodeStatAck",
    "PendingNodeRemoval",
    "PendingUserResync",
    "ProxySecretClaim",
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
    if name in {"ConnectionRestriction", "UserConnectionLimit", "UserConnectionState"}:
        from app.fork.models.connection import ConnectionRestriction, UserConnectionLimit, UserConnectionState

        return {
            "ConnectionRestriction": ConnectionRestriction,
            "UserConnectionLimit": UserConnectionLimit,
            "UserConnectionState": UserConnectionState,
        }[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
