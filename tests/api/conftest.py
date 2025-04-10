import pytest
from . import client


@pytest.fixture
def access_token():
    response = client.post(
        url="/api/admin/token",
        data={"username": "testadmin", "password": "testadmin", "grant_type": "password"},
    )
    return response.json()["access_token"]
