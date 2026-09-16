from app import on_shutdown, on_startup
from app.fork.traffic_log import collector
from config import runtime_settings


@on_startup
async def start_traffic_log_collector():
    if not runtime_settings.role.runs_node:
        await collector.load_settings()
        return
    await collector.start()


@on_shutdown
async def stop_traffic_log_collector():
    if not runtime_settings.role.runs_node:
        return
    await collector.stop()
