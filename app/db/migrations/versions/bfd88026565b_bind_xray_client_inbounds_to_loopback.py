"""bind the seeded Xray client inbounds to loopback

Revision ID: bfd88026565b
Revises: fd33c6013baa
Create Date: 2026-09-25 10:30:00.000000

"""

import re

import sqlalchemy as sa
from alembic import op

revision = "bfd88026565b"
down_revision = "fd33c6013baa"
branch_labels = None
depends_on = None


client_templates = sa.table(
    "client_templates",
    sa.column("id", sa.Integer()),
    sa.column("template_type", sa.String()),
    sa.column("content", sa.Text()),
    sa.column("is_system", sa.Boolean()),
)

EXPOSED_LISTENER = re.compile(r'("listen"\s*:\s*)"0\.0\.0\.0"')
INBOUNDS_ARRAY = re.compile(r'"inbounds"\s*:\s*\[')


def _json_array_end(content: str, start: int) -> int | None:
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(content)):
        character = content[index]
        if in_string:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
            continue
        if character == '"':
            in_string = True
        elif character == "[":
            depth += 1
        elif character == "]":
            depth -= 1
            if depth == 0:
                return index + 1
    return None


def bind_client_listeners_to_loopback(content: str) -> str:
    match = INBOUNDS_ARRAY.search(content)
    if not match:
        return content
    start = match.end() - 1
    end = _json_array_end(content, start)
    if end is None:
        return content
    return content[:start] + EXPOSED_LISTENER.sub(r'\1"127.0.0.1"', content[start:end]) + content[end:]


def upgrade() -> None:
    connection = op.get_bind()
    rows = connection.execute(
        sa.select(client_templates.c.id, client_templates.c.content).where(
            client_templates.c.template_type == "xray_subscription",
            client_templates.c.is_system.is_(True),
        )
    ).all()
    for template_id, content in rows:
        updated = bind_client_listeners_to_loopback(content)
        if updated != content:
            connection.execute(
                client_templates.update().where(client_templates.c.id == template_id).values(content=updated)
            )


def downgrade() -> None:
    pass
