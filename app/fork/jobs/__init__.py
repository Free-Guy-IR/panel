from importlib import import_module

from app.fork.jobs.inbound_usage import after_record_node_usages
from app.fork.jobs.node_extras import after_healthy_node_check
from app.fork.jobs.usage_coeff import apply_usage_value
from app.fork.jobs.webhook_signing import webhook_request

FORK_JOB_MODULES = (
    "cleanup_node_stats",
    "cleanup_node_user_usages",
    "connection_limiter",
    "content_filter_reconcile",
    "traffic_log_collector",
    "traffic_log_purge",
)

__all__ = [
    "FORK_JOB_MODULES",
    "after_healthy_node_check",
    "after_record_node_usages",
    "apply_usage_value",
    "register_fork_jobs",
    "webhook_request",
]


def register_fork_jobs() -> None:
    for name in FORK_JOB_MODULES:
        import_module(f"app.fork.jobs.{name}")
