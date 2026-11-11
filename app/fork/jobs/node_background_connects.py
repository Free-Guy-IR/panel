from app import on_shutdown
from app.fork.operation.node_background_connects import cancel_background_connects


@on_shutdown
async def stop_background_connects():
    await cancel_background_connects()
