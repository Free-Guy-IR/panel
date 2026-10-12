from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.db.crud import user_template as crud
from app.db.models import DataLimitResetStrategy
from app.models.user_template import UserTemplateModify


@pytest.fixture
def stored_template(monkeypatch):
    monkeypatch.setattr(crud, "load_user_template_attrs", AsyncMock())
    return SimpleNamespace(name="monthly", data_limit_reset_strategy=DataLimitResetStrategy.month)


@pytest.fixture
def db():
    return SimpleNamespace(commit=AsyncMock(), refresh=AsyncMock())


async def test_partial_update_without_reset_strategy_keeps_stored_cycle(db, stored_template):
    await crud.modify_user_template(db, stored_template, UserTemplateModify(name="renamed"))

    assert stored_template.name == "renamed"
    assert stored_template.data_limit_reset_strategy == DataLimitResetStrategy.month


async def test_explicit_reset_strategy_still_replaces_stored_cycle(db, stored_template):
    await crud.modify_user_template(
        db, stored_template, UserTemplateModify(data_limit_reset_strategy=DataLimitResetStrategy.no_reset)
    )

    assert stored_template.data_limit_reset_strategy == DataLimitResetStrategy.no_reset
