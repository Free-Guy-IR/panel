from app.notification.client import send_discord_webhook
from config import DISCORD_WEBHOOK_URL
from app.models.node import NodeResponse


async def create_node(node: NodeResponse, by: str):
    data = {
        "content": "",
        "embeds": [
            {
                "title": "Create Node",
                "description": f"**Name:** {node.name}\n" + f"**Address:** {node.address}\n" + f"**Port:** {node.port}",
                "color": 0x00FF00,
                "footer": {"text": f"ID: {node.id}\nBy: {by}"},
            }
        ],
    }
    if DISCORD_WEBHOOK_URL:
        await send_discord_webhook(data, DISCORD_WEBHOOK_URL)


async def modify_node(node: NodeResponse, by: str):
    data = {
        "content": "",
        "embeds": [
            {
                "title": "Modify Node",
                "description": f"**Name:** {node.name}\n" + f"**Address:** {node.address}\n" + f"**Port:** {node.port}",
                "color": 0xFFFF00,
                "footer": {"text": f"ID: {node.id}\nBy: {by}"},
            }
        ],
    }
    if DISCORD_WEBHOOK_URL:
        await send_discord_webhook(data, DISCORD_WEBHOOK_URL)


async def remove_node(node: NodeResponse, by: str):
    data = {
        "content": "",
        "embeds": [
            {
                "title": "Remove Node",
                "description": f"**Name:** {node.name}\n" + f"**Address:** {node.address}\n" + f"**Port:** {node.port}",
                "color": 0xFF0000,
                "footer": {"text": f"ID: {node.id}\nBy: {by}"},
            }
        ],
    }
    if DISCORD_WEBHOOK_URL:
        await send_discord_webhook(data, DISCORD_WEBHOOK_URL)
