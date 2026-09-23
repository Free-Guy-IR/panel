from sqlalchemy import false, select

from app.db.models import users_groups_association


def user_in_allowed_groups(user_id_column, allowed_group_ids: list[int]):
    if not allowed_group_ids:
        return false()
    return user_id_column.in_(
        select(users_groups_association.c.user_id).where(users_groups_association.c.groups_id.in_(allowed_group_ids))
    )
