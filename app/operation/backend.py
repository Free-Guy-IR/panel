from app.db import AsyncSession
from app.db.models import BackendConfig
from app.db.crud import create_backend_config, modify_backend_config, remove_backend_config, get_backend_configs
from app.models.admin import AdminDetails
from app.models.backend import BackendCreate, BackendResponseList, BackendResponse
from app.backend import backend_manager
from app.operation import BaseOperator
from app.utils.logger import get_logger


logger = get_logger("backend-operator")


class BackendOperation(BaseOperator):
    async def add_backend(self, db: AsyncSession, new_backend: BackendCreate, admin: AdminDetails) -> BackendResponse:
        try:
            db_backend = await create_backend_config(db, new_backend)
            await backend_manager.update_backend(db_backend)
        except Exception as e:
            await db.rollback()
            self.raise_error(message=e, code=400)

        logger.info(f'Backend config "{db_backend.id}" created by admin "{admin.username}"')

        return BackendResponse.model_validate(db_backend)

    async def get_all_backends(self, db: AsyncSession, offset: int, limit: int) -> BackendResponseList:
        db_backends, count = await get_backend_configs(db, offset, limit)
        return BackendResponseList(backends=db_backends, count=count)

    async def modify_backend(
        self, db: AsyncSession, backend_id: int, modified_backend: BackendCreate, admin: AdminDetails
    ) -> BackendConfig:
        db_backend = await self.get_validated_backend_config(db, backend_id)
        db_backend = await modify_backend_config(db, db_backend, modified_backend)
        try:
            db_backend = await modify_backend_config(db, db_backend, modified_backend)
            await backend_manager.update_backend(db_backend)
        except Exception as e:
            await db.rollback()
            self.raise_error(message=e, code=400)

        logger.info(f'Backend config "{db_backend.name}" modified by admin "{admin.username}"')
        return db_backend

    async def delete_backend(self, db: AsyncSession, backend_id: int, admin: AdminDetails) -> None:
        if backend_id == 1:
            self.raise_error(message="Cannot delete default backend config", code=403)

        db_backend = await self.get_validated_backend_config(db, backend_id)

        await remove_backend_config(db, db_backend)

        logger.info(f'Backend config "{db_backend.name}" deleted by admin "{admin.username}"')
