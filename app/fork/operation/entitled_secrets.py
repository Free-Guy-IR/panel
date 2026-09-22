from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field as dataclass_field

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from app.db import AsyncSession
from app.db.crud.user import get_users_by_ids
from app.db.crud.wireguard import get_users_accessible_tags_by_inbound_tags
from app.db.models import User
from app.fork.proxy_secrets import ProxySecretUniquenessError, ReservedSecrets, write_stored_secret
from app.fork.proxy_secrets.entitled import (
    ENTITLED_SPEC_BY_FIELD,
    EntitledSecretField,
    entitled_core_tags,
    issue_unique_secrets,
    spec_for_core_type,
    stored_secret,
)
from app.node.sync import sync_users
from app.utils.logger import get_logger

logger = get_logger("entitled-secrets")

ACTIVATION_CHUNK = 500

SyncUsers = Callable[[list[User]], Awaitable[None]]
LoadUsers = Callable[[AsyncSession, list[int]], Awaitable[list[User]]]


@dataclass
class EntitledSecretRun:
    field: EntitledSecretField
    entitled: int = 0
    missing: int = 0
    granted: int = 0
    unreadable: list[int] = dataclass_field(default_factory=list)


async def load_users_for_sync(db: AsyncSession, user_ids: list[int]) -> list[User]:
    return await get_users_by_ids(db, user_ids, load_admin_role=True)


def _chunks(values: list[int], size: int) -> Iterable[list[int]]:
    for start in range(0, len(values), size):
        yield values[start : start + size]


async def _ids_without_secret(db: AsyncSession, spec, user_ids: list[int], run: EntitledSecretRun) -> list[int]:
    rows = (await db.execute(select(User.id, User.proxy_settings).where(User.id.in_(user_ids)))).all()
    missing = []
    for user_id, proxy_settings in rows:
        try:
            if stored_secret(proxy_settings, spec) is None:
                missing.append(user_id)
        except ValidationError:
            run.unreadable.append(user_id)
    return sorted(missing)


async def issue_missing_entitled_secrets(
    db: AsyncSession,
    secret_field: EntitledSecretField,
    *,
    load_users: LoadUsers,
    sync_users: SyncUsers,
    candidate_ids: Iterable[int] | None = None,
    scan_tags: Iterable[str] | None = None,
    dry_run: bool = False,
    run: EntitledSecretRun | None = None,
    chunk_size: int = ACTIVATION_CHUNK,
) -> EntitledSecretRun:
    spec = ENTITLED_SPEC_BY_FIELD[secret_field]
    run = run if run is not None else EntitledSecretRun(field=secret_field)

    entitled_tags = (await entitled_core_tags(db, [spec]))[spec.field]
    if scan_tags is not None:
        entitled_tags = entitled_tags & set(scan_tags)
    if not entitled_tags:
        return run

    reachable = await get_users_accessible_tags_by_inbound_tags(db, entitled_tags)
    user_ids = set(reachable)
    if candidate_ids is not None:
        user_ids &= set(candidate_ids)
    ordered_ids = sorted(user_ids)
    run.entitled = len(ordered_ids)

    reserved: ReservedSecrets = {}
    for chunk in _chunks(ordered_ids, chunk_size):
        missing_ids = await _ids_without_secret(db, spec, chunk, run)
        if dry_run:
            run.missing += len(missing_ids)
            continue
        if not missing_ids:
            continue

        users = [user for user in await load_users(db, missing_ids) if stored_secret(user.proxy_settings, spec) is None]
        run.missing += len(users)
        if not users:
            continue

        values = await issue_unique_secrets(db, spec, len(users), reserved)
        for user, value in zip(users, values, strict=True):
            user.proxy_settings = write_stored_secret(user.proxy_settings, spec, value)
        await db.commit()
        run.granted += len(users)
        await sync_users(users)
        for user in users:
            db.expunge(user)

    if run.unreadable:
        logger.warning(
            f"{spec.field.value}: left {len(run.unreadable)} unreadable stored value(s) untouched: {run.unreadable[:20]}"
        )
    return run


async def grant_entitled_secrets_for_core(operation, db: AsyncSession, core) -> EntitledSecretRun | None:
    spec = spec_for_core_type(core.type)
    if spec is None:
        return None

    run = EntitledSecretRun(field=spec.field)
    try:
        await issue_missing_entitled_secrets(
            db,
            spec.field,
            load_users=load_users_for_sync,
            sync_users=sync_users,
            scan_tags=spec.core_tags([core]),
            run=run,
        )
    except (ProxySecretUniquenessError, SQLAlchemyError) as exc:
        logger.error(
            f"core {core.id} ({spec.label}): issued {run.granted} of the {run.missing} missing {spec.field.value} "
            f"value(s) found so far among {run.entitled} entitled user(s), then failed: {exc}"
        )
        await operation.raise_error(
            message=(
                f"Core saved, but {spec.label} credentials could not be issued to every entitled user "
                f"({run.granted} issued before the failure); run the {spec.label} activation to finish: {exc}"
            ),
            code=500,
            db=db,
        )
    if run.granted:
        logger.info(
            f"core {core.id} ({spec.label}): issued {run.granted} missing {spec.field.value} value(s) to entitled users"
        )
    return run
