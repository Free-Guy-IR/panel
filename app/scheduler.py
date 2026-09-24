from apscheduler.schedulers.asyncio import AsyncIOScheduler

scheduler = AsyncIOScheduler(
    job_defaults={"max_instances": 1, "coalesce": True, "misfire_grace_time": 30}, timezone="UTC"
)


def cancel_running_jobs(target: AsyncIOScheduler | None = None) -> int:
    cancelled = 0
    for executor in list((target or scheduler)._executors.values()):
        for future in list(getattr(executor, "_pending_futures", ())):
            if not future.done():
                future.cancel()
                cancelled += 1
    return cancelled
