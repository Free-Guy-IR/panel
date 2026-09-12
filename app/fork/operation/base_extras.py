from app.operation.permissions import apply_group_access, get_allowed_group_ids


async def reject_disallowed_assigned_groups(op, admin, unique_ids, groups_by_id, existing_group_ids) -> None:
    if admin is None:
        return
    allowed_group_ids = get_allowed_group_ids(admin)
    if allowed_group_ids is None:
        return
    allowed = set(allowed_group_ids) | set(existing_group_ids or ())
    forbidden = [groups_by_id[gid].name for gid in unique_ids if gid not in allowed]
    if forbidden:
        await op.raise_error(f"You are not allowed to use these groups: {', '.join(sorted(forbidden))}", 403)


def restrict_users_query_by_groups(admin, query):
    if get_allowed_group_ids(admin) is None:
        return query, False
    group_ids = apply_group_access(admin, query.group_ids)
    if not group_ids:
        return query, True
    return query.model_copy(update={"group_ids": group_ids}), False
