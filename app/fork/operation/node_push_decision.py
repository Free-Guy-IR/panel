from dataclasses import dataclass

from app.db import AsyncSession
from app.db.models import User, UserStatus
from app.fork.crud.user_locks import USER_STATE_KEYS, refresh_user_state_for_update
from app.node.user import node_payload_signature

NODE_STATE_KEYS = (*USER_STATE_KEYS, "hwid_limit")


@dataclass(frozen=True)
class LockedNodeState:
    signature: tuple
    status: UserStatus


async def locked_node_state(db: AsyncSession, db_user: User) -> LockedNodeState:
    await refresh_user_state_for_update(db, db_user, NODE_STATE_KEYS)
    return LockedNodeState(signature=await node_payload_signature(db_user), status=db_user.status)


async def node_state_changed(db_user: User, before: LockedNodeState) -> bool:
    return db_user.status != before.status or await node_payload_signature(db_user) != before.signature
