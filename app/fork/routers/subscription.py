from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from app.db import AsyncSession, get_db
from app.operation import OperatorType
from app.operation.subscription import SubscriptionOperation
from config import subscription_env_settings

router = APIRouter(tags=["Subscription"], prefix=f"/{subscription_env_settings.path}")
subscription_operator = SubscriptionOperation(operator_type=OperatorType.API)

SUBSCRIPTION_PING_DESCRIPTION = (
    """Live server latency/health (server-side TCP) of the user's config servers, for the subscription page."""
)


@router.get("/{token}/ping", description=SUBSCRIPTION_PING_DESCRIPTION)
async def user_subscription_ping(token: str, db: AsyncSession = Depends(get_db)):
    return JSONResponse(content=await subscription_operator.ping(db, token=token))
