from fastapi import APIRouter, Depends, Header, Path, Request

from app.db import Session, get_db
from app.models.user import SubscriptionUserResponse
from app.operation import OperatorType
from app.operation.subscription import SubscriptionOperator
from config import XRAY_SUBSCRIPTION_PATH


router = APIRouter(tags=["Subscription"], prefix=f"/{XRAY_SUBSCRIPTION_PATH}")
subscription_operator = SubscriptionOperator(operator_type=OperatorType.API)


@router.get("/{token}/")
@router.get("/{token}", include_in_schema=False)
async def user_subscription(
    request: Request,
    token: str,
    db: Session = Depends(get_db),
    user_agent: str = Header(default=""),
):
    """Provides a subscription link based on the user agent (Clash, V2Ray, etc.)."""
    return await subscription_operator.user_subscription(
        db,
        token=token,
        accept_header=request.headers.get("Accept", ""),
        user_agent=user_agent,
        request_url=str(request.url),
    )


@router.get("/{token}/info", response_model=SubscriptionUserResponse)
async def user_subscription_info(token: str, db: Session = Depends(get_db)):
    """Retrieves detailed information about the user's subscription."""
    return await subscription_operator.user_subscription_info(db, token=token)


@router.get("/{token}/usage")
async def user_get_usage(token: str, start: str = "", end: str = "", db: Session = Depends(get_db)):
    """Fetches the usage statistics for the user within a specified date range."""
    return await subscription_operator.user_get_usage(db, token=token, start=start, end=end)


@router.get("/{token}/{client_type}")
async def user_subscription_with_client_type(
    request: Request,
    token: str,
    client_type: str = Path(..., regex="sing-box|clash-meta|clash|outline|links|links-base64|xray"),
    db: Session = Depends(get_db),
):
    """Provides a subscription link based on the specified client type (e.g., Clash, V2Ray)."""
    return await subscription_operator.user_subscription_with_client_type(
        db, token=token, client_type=client_type, request_url=str(request.url)
    )
