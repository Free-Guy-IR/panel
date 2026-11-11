import asyncio
from typing import Any

from fastapi import HTTPException, status

from app.operation import OperatorType
from app.operation.api_key import APIKeyOperation
from app.routers.authentication import get_admin_from_api_key
from tests.api import TestSession

OPERATION_DONE = status.HTTP_200_OK


def call_as_key(raw_key: str, operation: str, **kwargs) -> tuple[int, Any]:
    async def _run() -> tuple[int, Any]:
        async with TestSession() as db:
            admin = await get_admin_from_api_key(db, raw_key)
            assert admin is not None
            try:
                result = await getattr(APIKeyOperation(operator_type=OperatorType.API), operation)(
                    db, admin=admin, **kwargs
                )
            except HTTPException as exc:
                return exc.status_code, None
            return OPERATION_DONE, result

    return asyncio.run(_run())


def run_as_key(raw_key: str, operation: str, **kwargs) -> int:
    return call_as_key(raw_key, operation, **kwargs)[0]
