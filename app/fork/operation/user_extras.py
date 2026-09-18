from pydantic import ValidationError

from app.db import AsyncSession
from app.db.models import User
from app.fork.proxy_secrets import (
    BulkRepairProxySecrets,
    duplicate_secret_values,
    enforce_unique_proxy_secrets,
    get_users_with_duplicate_secrets,
    read_stored_secret,
    specs_for,
    write_stored_secret,
)
from app.models.proxy import ProxyTable
from app.models.user import BulkOperationDryRunResponse, BulkUserFilter
from app.operation import OperatorType
from app.utils.l2tp import prepare_l2tp_password
from app.utils.logger import get_logger
from app.utils.mtproto import prepare_mtproto_secret
from app.utils.openvpn import prepare_openvpn_password

logger = get_logger("user-operation")


def _user_mod():
    from app.operation import user as user_mod

    return user_mod


async def prepare_fork_proxy_settings(
    db: AsyncSession,
    proxy_settings: ProxyTable,
    groups: list,
    *,
    exclude_user_id: int | None = None,
) -> ProxyTable:
    user_mod = _user_mod()
    prepare_openvpn = getattr(user_mod, "prepare_openvpn_password", prepare_openvpn_password)
    prepare_mtproto = getattr(user_mod, "prepare_mtproto_secret", prepare_mtproto_secret)
    prepare_l2tp = getattr(user_mod, "prepare_l2tp_password", prepare_l2tp_password)
    proxy_settings = await prepare_openvpn(db, proxy_settings, groups)
    proxy_settings = await prepare_mtproto(db, proxy_settings, groups)
    proxy_settings = await prepare_l2tp(db, proxy_settings, groups)
    return await enforce_unique_proxy_secrets(db, proxy_settings, exclude_user_id=exclude_user_id)


class UserExtrasMixin:
    async def bulk_activate_l2tp_passwords(self, db: AsyncSession, bulk_model: BulkUserFilter):
        user_mod = _user_mod()
        candidates = await user_mod.get_users_for_l2tp_activation(db, bulk_model)
        l2tp_tags = user_mod.l2tp_core_tags(await user_mod.get_l2tp_cores(db))

        to_update: list[tuple[User, str]] = []
        skipped: list[int] = []
        for user in candidates:
            try:
                current = ProxyTable.model_validate(user.proxy_settings)
                if current.l2tp.password:
                    continue
                if not l2tp_tags or not (l2tp_tags & await user_mod.tags_from_groups(user.groups)):
                    continue
            except ValidationError, ValueError:
                skipped.append(user.id)
                continue
            to_update.append((user, user_mod.generate_l2tp_password()))

        if skipped:
            logger.warning(
                f"L2TP activation skipped {len(skipped)} users with unreadable proxy settings: {skipped[:20]}"
            )

        if bulk_model.dry_run:
            return BulkOperationDryRunResponse(affected_users=len(to_update))

        if not to_update:
            if self.operator_type in (OperatorType.API, OperatorType.WEB):
                return {"detail": "operation has been successfuly done on 0 users"}
            return 0

        for user, password in to_update:
            settings = dict(user.proxy_settings or {})
            settings["l2tp"] = {"password": password}
            user.proxy_settings = settings
        await db.commit()

        updated_users = [user for user, _ in to_update]
        await user_mod.sync_users(updated_users)

        if self.operator_type in (OperatorType.API, OperatorType.WEB):
            return {"detail": f"operation has been successfuly done on {len(to_update)} users"}
        return len(to_update)

    async def bulk_repair_duplicate_proxy_secrets(self, db: AsyncSession, bulk_model: BulkRepairProxySecrets):
        user_mod = _user_mod()
        specs = specs_for(bulk_model.secret_fields)

        shared = {}
        for spec in specs:
            values = await duplicate_secret_values(db, spec)
            if values:
                shared[spec.field] = (spec, values)

        candidates = await get_users_with_duplicate_secrets(db, shared, bulk_model)

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

    async def bulk_activate_mtproto_secrets(self, db: AsyncSession, bulk_model: BulkUserFilter):
        user_mod = _user_mod()
        candidates = await user_mod.get_users_for_mtproto_activation(db, bulk_model)

        to_update: list[tuple[User, str]] = []
        skipped: list[int] = []
        for user in candidates:
            try:
                current = ProxyTable.model_validate(user.proxy_settings)
            except ValidationError, ValueError:
                skipped.append(user.id)
                continue
            if current.mtproto.secret:
                continue
            updated = await user_mod.prepare_mtproto_secret(db, current, user.groups)
            if not updated.mtproto.secret:
                continue
            to_update.append((user, updated.mtproto.secret))

        if skipped:
            logger.warning(
                f"MTProto activation skipped {len(skipped)} users with unreadable proxy settings: {skipped[:20]}"
            )

        if bulk_model.dry_run:
            return BulkOperationDryRunResponse(affected_users=len(to_update))

        if not to_update:
            if self.operator_type in (OperatorType.API, OperatorType.WEB):
                return {"detail": "operation has been successfuly done on 0 users"}
            return 0

        for user, secret in to_update:
            settings = dict(user.proxy_settings or {})
            settings["mtproto"] = {"secret": secret}
            user.proxy_settings = settings
        await db.commit()

        updated_users = [user for user, _ in to_update]
        await user_mod.sync_users(updated_users)

        if self.operator_type in (OperatorType.API, OperatorType.WEB):
            return {"detail": f"operation has been successfuly done on {len(updated_users)} users"}
        return len(updated_users)
