import os
import sys
import pytest
import warnings
from pydantic import PydanticDeprecatedSince20

# Add the project root directory to the Python path
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, project_root)

# Override the config module for tests
import config  # noqa
from tests.api import XRAY_JSON_TEST_FILE, client  # noqa

config.TESTING = True
config.XRAY_JSON = XRAY_JSON_TEST_FILE
config.SUDOERS["testadmin"] = "testadmin"


# Filter out all warnings
@pytest.fixture(autouse=True)
def ignore_all_warnings():
    warnings.filterwarnings("ignore")
    warnings.filterwarnings("ignore", category=DeprecationWarning)
    warnings.filterwarnings("ignore", category=PydanticDeprecatedSince20)
    warnings.filterwarnings("ignore", category=UserWarning)
    warnings.filterwarnings("ignore", category=FutureWarning)
    warnings.filterwarnings("ignore", category=RuntimeWarning)
