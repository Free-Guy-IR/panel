from app import scheduler
from app.fork.proxy_secrets.group_repair import drain_pending_resyncs
from config import runtime_settings

DRAIN_INTERVAL_SECONDS = 60

if runtime_settings.role.runs_scheduler:
    scheduler.add_job(
        drain_pending_resyncs,
        "interval",
        seconds=DRAIN_INTERVAL_SECONDS,
        max_instances=1,
        coalesce=True,
        id="pending_user_resync_drain",
        replace_existing=True,
    )
