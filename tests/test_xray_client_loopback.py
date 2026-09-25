import asyncio
import importlib.util
import json
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy import select

from app.db.models import ClientTemplate
from tests.api import TestSession

MIGRATION = next(Path("app/db/migrations/versions").glob("*_bind_xray_client_inbounds_to_loopback.py"), None)


def _load_migration():
    assert MIGRATION is not None, "no migration binds the seeded Xray client inbounds to loopback"
    spec = importlib.util.spec_from_file_location("xray_client_loopback", MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_seeded_system_xray_template_listens_on_loopback_only():
    async def _load():
        async with TestSession() as session:
            rows = await session.execute(
                select(ClientTemplate.content).where(
                    ClientTemplate.template_type == "xray_subscription", ClientTemplate.is_system.is_(True)
                )
            )
            return rows.scalars().all()

    contents = asyncio.run(_load())
    assert contents
    for content in contents:
        listens = {inbound.get("listen") for inbound in json.loads(content)["inbounds"]}
        assert "0.0.0.0" not in listens
        assert listens <= {"127.0.0.1"}


def test_the_rewrite_touches_only_system_xray_inbounds_and_keeps_the_text(monkeypatch):
    module = _load_migration()
    canonical = '{"inbounds": [{"listen": "0.0.0.0"}]}'
    customized = (
        '{"inbounds":[{"listen":"0.0.0.0", "custom": true}], '
        '"outbounds": [{"listen": "0.0.0.0"}], "label": "keep 0.0.0.0"}'
    )
    already_safe = '{"inbounds": [{"listen": "127.0.0.1", "custom": true}]}'
    engine = sa.create_engine("sqlite://")
    metadata = sa.MetaData()
    templates = sa.Table(
        "client_templates",
        metadata,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("template_type", sa.String),
        sa.Column("content", sa.Text),
        sa.Column("is_system", sa.Boolean),
    )
    metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(
            templates.insert(),
            [
                {"id": 1, "template_type": "xray_subscription", "content": canonical, "is_system": True},
                {"id": 2, "template_type": "xray_subscription", "content": customized, "is_system": True},
                {"id": 3, "template_type": "xray_subscription", "content": customized, "is_system": False},
                {"id": 4, "template_type": "singbox_subscription", "content": customized, "is_system": True},
                {"id": 5, "template_type": "xray_subscription", "content": already_safe, "is_system": True},
            ],
        )
        monkeypatch.setattr(module.op, "get_bind", lambda: connection)
        module.upgrade()
        module.upgrade()
        contents = dict(connection.execute(sa.select(templates.c.id, templates.c.content)).all())

    assert contents[1] == '{"inbounds": [{"listen": "127.0.0.1"}]}'
    assert contents[2] == (
        '{"inbounds":[{"listen":"127.0.0.1", "custom": true}], '
        '"outbounds": [{"listen": "0.0.0.0"}], "label": "keep 0.0.0.0"}'
    )
    assert contents[3] == customized
    assert contents[4] == customized
    assert contents[5] == already_safe


def test_the_downgrade_does_not_reopen_the_listeners():
    module = _load_migration()
    assert module.downgrade() is None
