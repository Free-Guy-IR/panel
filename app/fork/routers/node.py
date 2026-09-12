from typing import Annotated

from fastapi import APIRouter, Depends, Query

from app.db import AsyncSession, get_db
from app.models.admin import AdminDetails
from app.models.node import InboundUsageQuery
from app.models.stats import InboundUsageStatsList, Period
from app.operation import OperatorType
from app.operation.node import NodeOperation
from app.routers.authentication import require_permission
from app.routers.dependencies._common import make_query_dependency
from app.utils import responses

node_operator = NodeOperation(operator_type=OperatorType.API)
router = APIRouter(tags=["Node"], prefix="/api/node", responses={401: responses._401, 403: responses._403})

INBOUNDS_USAGE_DESCRIPTION = """Retrieve per-inbound usage statistics within a specified date range."""

get_inbound_usage_query = make_query_dependency(
    InboundUsageQuery,
    field_overrides={
        "period": Query(Period.hour),
        "inbound_tag": Query(None),
        "node_id": Query(None),
        "start": Query(None, examples=["2024-01-01T00:00:00+03:30"]),
        "end": Query(None, examples=["2024-01-31T23:59:59+03:30"]),
    },
)


@router.get(
    "/inbounds/usage",
    response_model=InboundUsageStatsList,
    description=INBOUNDS_USAGE_DESCRIPTION,
)
async def get_inbounds_usage_stats(
    query: Annotated[InboundUsageQuery, Depends(get_inbound_usage_query)],
    db: AsyncSession = Depends(get_db),
    _: AdminDetails = Depends(require_permission("nodes", "stats")),
):
    return await node_operator.get_inbounds_usage(db=db, query=query)
