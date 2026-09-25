from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import and_, select
from sqlalchemy.exc import DBAPIError

from app.db import AsyncSession
from app.db.crud.wireguard import (
    _accessible_tags_stmt,
    get_wg_cores,
    lock_users_proxy_settings,
    tags_from_groups,
    wg_core_tags,
)
from app.db.models import Group, ProxyInbound, User, inbounds_groups_association
from app.fork.operation.entitled_secrets import issue_missing_entitled_secrets, load_users_for_sync
from app.fork.proxy_secrets import (
    BulkRepairProxySecrets,
    ReservedSecrets,
    duplicate_secret_values,
    enforce_unique_proxy_secrets,
    get_users_with_duplicate_secrets,
    read_stored_secret,
    specs_for,
    write_stored_secret,
)
from app.fork.proxy_secrets.entitled import ENTITLED_SECRET_SPECS, EntitledSecretField, entitled_core_tags
from app.fork.proxy_secrets.locks import (
    LOCK_UNAVAILABLE_DETAIL,
    NamedLockMissing,
    acquire_proxy_secrets_lock,
    is_lock_unavailable,
    read_committed_rows,
)
from app.fork.proxy_secrets.wireguard_scope import drop_unentitled_wireguard_keys
from app.models.proxy import ProxyTable
from app.models.user import BulkOperationDryRunResponse, BulkUserFilter
from app.operation import OperatorType
from app.operation.permissions import get_allowed_group_ids
from app.utils.l2tp import prepare_l2tp_password
from app.utils.logger import get_logger
from app.utils.mtproto import prepare_mtproto_secret
from app.utils.openvpn import prepare_openvpn_password

logger = get_logger("user-operation")


def _user_mod():
    from app.operation import user as user_mod

    return user_mod


def _allowed_group_ids(admin) -> list[int] | None:
    return None if admin is None else get_allowed_group_ids(admin)


@dataclass(frozen=True)
class CommittedInbound:
    tag: str


@dataclass(frozen=True)
class CommittedAccess:
    inbounds: tuple[CommittedInbound, ...]
    is_disabled: bool = False


async def committed_access(db: AsyncSession, user_id: int, groups: Sequence[Group] | None) -> list[CommittedAccess]:
    if groups is None:
        statement = _accessible_tags_stmt(user_ids=[user_id])
    else:
        statement = (
            select(inbounds_groups_association.c.group_id, ProxyInbound.tag)
            .join(ProxyInbound, ProxyInbound.id == inbounds_groups_association.c.inbound_id)
            .join(Group, and_(Group.id == inbounds_groups_association.c.group_id, Group.is_disabled.is_(False)))
            .where(Group.id.in_([group.id for group in groups]))
        )
    tags = sorted({tag for _, tag in await read_committed_rows(db, statement)})
    return [CommittedAccess(inbounds=tuple(CommittedInbound(tag) for tag in tags))]


async def _wireguard_key_held_by_another_user(db: AsyncSession, public_key: str, user_id: int) -> bool:
    column = User.proxy_settings["wireguard"]["public_key"].as_string()
    statement = select(User.id).where(column == public_key, User.id != user_id).limit(1)
    return bool(await read_committed_rows(db, statement))


async def _keep_stored_wireguard_keys(
    db: AsyncSession, incoming: ProxyTable, stored: ProxyTable, reachable: set[str], user_id: int
) -> None:
    wanted, kept = incoming.wireguard, stored.wireguard
    if wanted.private_key or wanted.public_key or not kept.private_key:
        return
    if not wg_core_tags(await get_wg_cores(db)) & reachable:
        return
    if kept.public_key and await _wireguard_key_held_by_another_user(db, kept.public_key, user_id):
        return
    wanted.private_key = kept.private_key
    wanted.public_key = kept.public_key


async def keep_stored_secrets(
    db: AsyncSession, incoming: ProxyTable, stored: ProxyTable, access: Sequence, user_id: int
) -> ProxyTable:
    reachable = await tags_from_groups(access)
    tags_by_field = await entitled_core_tags(db, ENTITLED_SECRET_SPECS)
    for spec in ENTITLED_SECRET_SPECS:
        if not tags_by_field.get(spec.field, set()) & reachable:
            continue
        section = getattr(incoming, spec.protocol)
        if getattr(section, spec.attribute):
            continue
        stored_value = getattr(getattr(stored, spec.protocol), spec.attribute)
        if stored_value:
            setattr(section, spec.attribute, stored_value)
    await _keep_stored_wireguard_keys(db, incoming, stored, reachable, user_id)
    return incoming


async def prepare_fork_proxy_settings(
    db: AsyncSession,
    proxy_settings: ProxyTable,
    groups: list,
    *,
    exclude_user_id: int | None = None,
    reserved: ReservedSecrets | None = None,
) -> ProxyTable:
    user_mod = _user_mod()
    prepare_openvpn = getattr(user_mod, "prepare_openvpn_password", prepare_openvpn_password)
    prepare_mtproto = getattr(user_mod, "prepare_mtproto_secret", prepare_mtproto_secret)
    prepare_l2tp = getattr(user_mod, "prepare_l2tp_password", prepare_l2tp_password)
    proxy_settings = await drop_unentitled_wireguard_keys(db, proxy_settings, groups, exclude_user_id=exclude_user_id)
    proxy_settings = await prepare_openvpn(db, proxy_settings, groups)
    proxy_settings = await prepare_mtproto(db, proxy_settings, groups)
    proxy_settings = await prepare_l2tp(db, proxy_settings, groups)
    return await enforce_unique_proxy_secrets(db, proxy_settings, exclude_user_id=exclude_user_id, reserved=reserved)


class UserExtrasMixin:
    async def _raise_if_lock_unavailable(self, db: AsyncSession, exc: Exception) -> None:
        if not is_lock_unavailable(exc):
            raise exc
        await self.raise_error(message=LOCK_UNAVAILABLE_DETAIL, code=503, db=db)

    async def _lock_user_proxy_settings(
        self, db: AsyncSession, db_user: User, groups: Sequence[Group] | None = None
    ) -> list[CommittedAccess]:
        try:
            await acquire_proxy_secrets_lock(db)
            await lock_users_proxy_settings(db, [db_user])
            return await committed_access(db, db_user.id, groups)
        except (DBAPIError, NamedLockMissing) as exc:
            await self._raise_if_lock_unavailable(db, exc)

    async def bulk_activate_l2tp_passwords(self, db: AsyncSession, bulk_model: BulkUserFilter, admin=None):
        return await self._bulk_activate_entitled_secret(db, bulk_model, EntitledSecretField.l2tp_password, admin)

    async def bulk_repair_duplicate_proxy_secrets(
        self, db: AsyncSession, bulk_model: BulkRepairProxySecrets, admin=None
    ):
        user_mod = _user_mod()
        specs = specs_for(bulk_model.secret_fields)
        if not bulk_model.dry_run:
            try:
                await acquire_proxy_secrets_lock(db)
            except (DBAPIError, NamedLockMissing) as exc:
                await self._raise_if_lock_unavailable(db, exc)

        shared = {}
        for spec in specs:
            values = await duplicate_secret_values(db, spec)
            if values:
                shared[spec.field] = (spec, values)

        candidates = await get_users_with_duplicate_secrets(db, shared, bulk_model, _allowed_group_ids(admin))
        if not bulk_model.dry_run:
            candidates = await lock_users_proxy_settings(db, candidates)

        taken = {field: set(values) for field, (_, values) in shared.items()}
        to_update: list[tuple[User, dict]] = []
        for user in candidates:
            settings = user.proxy_settings
            changed = False
            for field, (spec, values) in shared.items():
                stored = read_stored_secret(settings, spec)
                if stored is None or stored not in values:
                    continue
                replacement = spec.generate()
                while str(replacement) in taken[field]:
                    replacement = spec.generate()
                taken[field].add(str(replacement))
                settings = write_stored_secret(settings, spec, replacement)
                changed = True
            if changed:
                to_update.append((user, settings))

        if bulk_model.dry_run:
            return BulkOperationDryRunResponse(affected_users=len(to_update))

        if not to_update:
            if self.operator_type in (OperatorType.API, OperatorType.WEB):
                return {"detail": "operation has been successfuly done on 0 users"}
            return 0

        logger.warning(
            f"repairing duplicate proxy secrets on {len(to_update)} users for fields: "
            f"{sorted(field.value for field in shared)}"
        )

        for user, settings in to_update:
            user.proxy_settings = settings
        await db.commit()

        updated_users = [user for user, _ in to_update]
        await user_mod.sync_users(updated_users)

        if self.operator_type in (OperatorType.API, OperatorType.WEB):
            return {"detail": f"operation has been successfuly done on {len(updated_users)} users"}
        return len(updated_users)

    async def _bulk_activate_entitled_secret(
        self, db: AsyncSession, bulk_model: BulkUserFilter, secret_field: EntitledSecretField, admin=None
    ):
        from app.db.crud.bulk import _create_final_filter

        user_mod = _user_mod()
        candidate_ids = (
            (await db.execute(select(User.id).where(_create_final_filter(bulk_model, _allowed_group_ids(admin)))))
            .scalars()
            .all()
        )
        try:
            run = await issue_missing_entitled_secrets(
                db,
                secret_field,
                load_users=load_users_for_sync,
                sync_users=user_mod.sync_users,
                candidate_ids=candidate_ids,
                dry_run=bulk_model.dry_run,
            )
        except (DBAPIError, NamedLockMissing) as exc:
            await self._raise_if_lock_unavailable(db, exc)

        if bulk_model.dry_run:
            return BulkOperationDryRunResponse(affected_users=run.missing)

        logger.info(
            f"{secret_field.value} activation: {run.granted} of {run.entitled} entitled user(s) in scope "
            "were missing a value and received one"
        )
        if self.operator_type in (OperatorType.API, OperatorType.WEB):
            return {"detail": f"operation has been successfuly done on {run.granted} users"}
        return run.granted

    async def bulk_activate_mtproto_secrets(self, db: AsyncSession, bulk_model: BulkUserFilter, admin=None):
        return await self._bulk_activate_entitled_secret(db, bulk_model, EntitledSecretField.mtproto_secret, admin)

    async def bulk_activate_openvpn_passwords(self, db: AsyncSession, bulk_model: BulkUserFilter, admin=None):
        return await self._bulk_activate_entitled_secret(db, bulk_model, EntitledSecretField.openvpn_password, admin)
