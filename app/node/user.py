from PasarGuardNodeBridge import create_proxy, create_user
from PasarGuardNodeBridge.common.service_pb2 import User as ProtoUser
from pydantic import ValidationError
from sqlalchemy import and_, func, or_, select

from app.db import AsyncSession
from app.db.models import (
    Admin,
    AdminRole,
    AdminStatus,
    Group,
    ProxyInbound,
    User,
    UserStatus,
    inbounds_groups_association,
    users_groups_association,
)
from app.fork.node_credentials import omit_empty_credentials
from app.models.protocol import ProxyProtocol
from app.models.proxy import VLESS_NODE_FLOWS, L2TPSettings, XTLSFlows
from app.utils.logger import get_logger

logger = get_logger("node-user")

_ALL_PROXY_PROTOCOLS = frozenset(ProxyProtocol)
_MAX_NODE_CONNECTION_CAP = 2**32 - 1


def _inbounds_from_loaded_groups(user: User) -> list[str] | None:
    loaded_groups = user.__dict__.get("groups")
    if loaded_groups is None:
        return None

    tags: set[str] = set()
    for group in loaded_groups:
        if group.is_disabled:
            continue

        loaded_inbounds = group.__dict__.get("inbounds")
        if loaded_inbounds is None:
            return None

        for inbound in loaded_inbounds:
            tags.add(inbound.tag)

    return list(tags)


async def serialize_user(user: User, allowed_protocols: frozenset[ProxyProtocol] | None = None) -> ProtoUser:
    user_settings = user.proxy_settings
    inbounds = None
    status = user.__dict__.get("status")
    if status is None:
        status = await user.awaitable_attrs.status

    if status in (UserStatus.active, UserStatus.on_hold):
        inbounds = _inbounds_from_loaded_groups(user)
        if inbounds is None:
            inbounds = await user.inbounds()

    return _serialize_or_revoke(user.id, user_settings, inbounds, allowed_protocols, user.hwid_limit)


def node_vless_flow(flow, user_id) -> str | None:
    if flow == XTLSFlows.VISION_UDP443:
        return XTLSFlows.VISION.value
    if flow is None or (isinstance(flow, str) and flow in VLESS_NODE_FLOWS):
        return flow
    logger.warning(f"Dropping an unusable stored VLESS flow for user {user_id}")
    return None


def node_connection_cap(hwid_limit) -> int:
    if isinstance(hwid_limit, bool) or not isinstance(hwid_limit, int) or hwid_limit <= 0:
        return 0
    return min(hwid_limit, _MAX_NODE_CONNECTION_CAP)


def _serialize_or_revoke(
    id: int,
    user_settings: dict,
    inbounds: list[str] | None = None,
    allowed_protocols: frozenset[ProxyProtocol] | None = None,
    hwid_limit: int | None = None,
) -> ProtoUser:
    try:
        return _serialize_user_for_node(id, user_settings, inbounds, allowed_protocols, hwid_limit)
    except (TypeError, ValueError, AttributeError) as exc:
        logger.error(f"User {id} cannot be serialized for the nodes ({type(exc).__name__}); revoking it there instead")
        return create_user(str(id), omit_empty_credentials(create_proxy()), [])



def safe_l2tp_password(password, user_id) -> str | None:
    if password is None:
        return None
    try:
        return L2TPSettings(password=password).password
    except ValidationError:
        logger.warning(f"Dropping an unusable stored L2TP password for user {user_id}")
        return None


async def node_payload_signature(user: User) -> tuple[str, bytes, tuple[str, ...]]:
    """What a node would receive for this user, in a form that can be compared before and after a change.

    Uses the same serialization as delivery (credentials, inbounds derived from
    enabled groups, and status), so equal signatures mean no node needs an update.
    """
    proto = await serialize_user(user)
    return proto.email, proto.proxies.SerializeToString(deterministic=True), tuple(sorted(proto.inbounds))


def _serialize_user_for_node(
    id: int,
    user_settings: dict,
    inbounds: list[str] | None = None,
    allowed_protocols: frozenset[ProxyProtocol] | None = None,
    hwid_limit: int | None = None,
) -> ProtoUser:
    allowed_protocols = allowed_protocols or _ALL_PROXY_PROTOCOLS

    proxy_kwargs = {}
    if ProxyProtocol.vmess in allowed_protocols:
        proxy_kwargs["vmess_id"] = user_settings.get("vmess", {}).get("id")
    if ProxyProtocol.vless in allowed_protocols:
        vless_settings = user_settings.get("vless", {})
        proxy_kwargs["vless_id"] = vless_settings.get("id")
        proxy_kwargs["vless_flow"] = node_vless_flow(vless_settings.get("flow"), id)
    if ProxyProtocol.trojan in allowed_protocols:
        proxy_kwargs["trojan_password"] = user_settings.get("trojan", {}).get("password")
    if ProxyProtocol.shadowsocks in allowed_protocols:
        shadowsocks_settings = user_settings.get("shadowsocks", {})
        proxy_kwargs["shadowsocks_password"] = shadowsocks_settings.get("password")
        proxy_kwargs["shadowsocks_method"] = shadowsocks_settings.get("method")
    if ProxyProtocol.wireguard in allowed_protocols:
        wireguard_settings = user_settings.get("wireguard", {})
        proxy_kwargs["wireguard_public_key"] = wireguard_settings.get("public_key")
        proxy_kwargs["wireguard_peer_ips"] = wireguard_settings.get("peer_ips") or []
    if ProxyProtocol.hysteria in allowed_protocols:
        proxy_kwargs["hysteria_auth"] = user_settings.get("hysteria", {}).get("auth")
    if ProxyProtocol.hysteria2 in allowed_protocols:
        proxy_kwargs["hysteria2_password"] = user_settings.get("hysteria2", {}).get("password")
    if ProxyProtocol.openvpn in allowed_protocols:
        proxy_kwargs["openvpn_username"] = str(id)
        proxy_kwargs["openvpn_password"] = user_settings.get("openvpn", {}).get("password")
        # hwid_limit is reused here as a concurrent-connection cap (0/None = unlimited) -
        # OpenVPN has no device-fingerprint concept, so this is a different semantic
        # than hwid_limit's original meaning for Xray (distinct devices ever seen).
        proxy_kwargs["openvpn_max_concurrent_connections"] = node_connection_cap(hwid_limit)
    if ProxyProtocol.mtproto in allowed_protocols:
        proxy_kwargs["mtproto_username"] = str(id)
        proxy_kwargs["mtproto_secret"] = user_settings.get("mtproto", {}).get("secret")
    if ProxyProtocol.tuic in allowed_protocols:
        tuic_settings = user_settings.get("tuic", {})
        proxy_kwargs["tuic_uuid"] = tuic_settings.get("uuid")
        proxy_kwargs["tuic_password"] = tuic_settings.get("password")
    if ProxyProtocol.l2tp in allowed_protocols:
        proxy_kwargs["l2tp_username"] = str(id)
        proxy_kwargs["l2tp_password"] = safe_l2tp_password(user_settings.get("l2tp", {}).get("password"), id)

    return create_user(
        str(id),
        omit_empty_credentials(create_proxy(**proxy_kwargs)),
        inbounds,
    )


async def core_users(
    db: AsyncSession,
    inbound_tags: list[str] | set[str] | None = None,
    allowed_protocols: frozenset[ProxyProtocol] | None = None,
    user_ids: list[int] | set[int] | None = None,
):
    """Serialize the users a core should carry, optionally restricted to ``user_ids`` (one indexed query)."""
    if user_ids is not None and not user_ids:
        return []
    dialect = db.bind.dialect.name
    inbound_tags = list(dict.fromkeys(inbound_tags or []))

    # Use dialect-specific aggregation and grouping
    if dialect == "postgresql":
        inbound_agg = func.string_agg(ProxyInbound.tag.distinct(), ",").label("inbound_tags")
    else:
        # MySQL and SQLite use group_concat
        inbound_agg = func.group_concat(ProxyInbound.tag.distinct()).label("inbound_tags")

    stmt = (
        select(
            User.id,
            User.proxy_settings,
            User.hwid_limit,
            inbound_agg,
        )
        .outerjoin(users_groups_association, User.id == users_groups_association.c.user_id)
        .outerjoin(
            Group,
            and_(
                users_groups_association.c.groups_id == Group.id,
                Group.is_disabled.is_(False),
            ),
        )
        .outerjoin(inbounds_groups_association, Group.id == inbounds_groups_association.c.group_id)
        .outerjoin(
            ProxyInbound,
            and_(
                inbounds_groups_association.c.inbound_id == ProxyInbound.id,
                ProxyInbound.tag.in_(inbound_tags) if inbound_tags else True,
            ),
        )
        # Exclude users whose admin role blocks user sync for the admin's current status.
        .outerjoin(Admin, Admin.id == User.admin_id)
        .outerjoin(AdminRole, AdminRole.id == Admin.role_id)
        .where(User.status.in_([UserStatus.active, UserStatus.on_hold]))
        .where(
            or_(
                Admin.id.is_(None),
                Admin.status.not_in([AdminStatus.limited, AdminStatus.disabled]),
                and_(Admin.status == AdminStatus.limited, AdminRole.disconnect_users_when_limited.is_not(True)),
                and_(Admin.status == AdminStatus.disabled, AdminRole.disconnect_users_when_disabled.is_not(True)),
            )
        )
        .group_by(User.id)
    )
    if user_ids is not None:
        stmt = stmt.where(User.id.in_(list(user_ids)))

    results = (await db.execute(stmt)).all()
    bridge_users: list = []

    for row in results:
        inbound_tags = row.inbound_tags.split(",") if row.inbound_tags else []
        if not inbound_tags:
            continue
        try:
            bridge_users.append(
                _serialize_user_for_node(
                    row.id,
                    row.proxy_settings,
                    inbound_tags,
                    allowed_protocols,
                    row.hwid_limit,
                )
            )
        except (TypeError, ValueError, AttributeError) as exc:
            logger.error(f"User {row.id} cannot be serialized for the nodes ({type(exc).__name__}); leaving it out")
    return bridge_users


async def serialize_users_for_node(
    users: list[User],
    allowed_protocols: frozenset[ProxyProtocol] | None = None,
) -> list[ProtoUser]:
    """Serialize users for node dispatch."""
    bridge_users: list = []

    for user in users:
        inbounds_list = []
        if user.status in [UserStatus.active, UserStatus.on_hold]:
            loaded_inbounds = _inbounds_from_loaded_groups(user)
            if loaded_inbounds is None:
                inbounds_list = await user.inbounds()
            else:
                inbounds_list = loaded_inbounds

        bridge_users.append(
            _serialize_or_revoke(user.id, user.proxy_settings, inbounds_list, allowed_protocols, user.hwid_limit)
        )

    return bridge_users
