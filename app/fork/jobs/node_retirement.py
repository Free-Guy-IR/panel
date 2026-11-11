from app import scheduler
from app.fork.operation.node_retirement import DRAIN_INTERVAL_SECONDS, drain_node_retirements
from config import runtime_settings

if runtime_settings.role.runs_node:
    scheduler.add_job(
        drain_node_retirements,
        "interval",
        seconds=DRAIN_INTERVAL_SECONDS,
        max_instances=1,
        coalesce=True,
        id="node_retirement_drain",
        replace_existing=True,
    )
