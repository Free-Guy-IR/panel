from sqlalchemy import select
from sqlalchemy.exc import DBAPIError

from app.db import AsyncSession
from app.db.crud.wireguard import lock_users_proxy_settings
from app.db.models import User
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
from app.fork.proxy_secrets.entitled import EntitledSecretField
from app.fork.proxy_secrets.locks import (
    LOCK_UNAVAILABLE_DETAIL,
    NamedLockMissing,
    acquire_proxy_secrets_lock,
    is_lock_unavailable,
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

    async def _lock_user_proxy_settings(self, db: AsyncSession, db_user: User) -> None:
        try:
            await acquire_proxy_secrets_lock(db)
            await lock_users_proxy_settings(db, [db_user])
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
