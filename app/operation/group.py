from app import backend
from app.db import Session, crud
from app.models.group import Group, GroupCreate, GroupModify, GroupsResponse
from app.operation import BaseOperator


class GroupOperation(BaseOperator):
    async def check_inbound_tags(self, tags: list[str]) -> None:
        for tag in tags:
            if tag not in backend.config.inbounds_by_tag:
                self.raise_error(f"{tag} not found", 400)

    async def add_group(self, db: Session, new_group: GroupCreate) -> Group:
        await self.check_inbound_tags(new_group.inbound_tags)
        return crud.create_group(db, new_group)

    async def get_all_groups(self, db: Session, offset: int, limit: int) -> GroupsResponse:
        dbgroups, count = crud.get_group(db, offset, limit)
        return {"groups": dbgroups, "total": count}

    async def modify_group(self, db: Session, group_id: int, modified_group: GroupModify) -> Group:
        dbgroup = await self.get_validated_group(db, group_id)
        if modified_group.inbound_tags:
            await self.check_inbound_tags(modified_group.inbound_tags)
        # TODO: add users to nodes
        return crud.update_group(db, dbgroup, modified_group)

    async def delete_group(self, db: Session, group_id: int) -> None:
        dbgroup = await self.get_validated_group(db, group_id)
        crud.remove_group(db, dbgroup)
        # TODO: remove users from nodes
