import json
import re
from enum import Enum

from app.templates import render_template
from config import GRPC_USER_AGENT_TEMPLATE, USER_AGENT_TEMPLATE


class BaseSubscription:
    def __init__(self):
        self.proxy_remarks = []
        user_agent_data = json.loads(render_template(USER_AGENT_TEMPLATE))
        if "list" in user_agent_data and isinstance(user_agent_data["list"], list):
            self.user_agent_list = user_agent_data["list"]
        else:
            self.user_agent_list = []

        grpc_user_agent_data = json.loads(render_template(GRPC_USER_AGENT_TEMPLATE))

        if "list" in grpc_user_agent_data and isinstance(grpc_user_agent_data["list"], list):
            self.grpc_user_agent_data = grpc_user_agent_data["list"]
        else:
            self.grpc_user_agent_data = []

        del user_agent_data, grpc_user_agent_data

    def _remark_validation(self, remark):
        if remark not in self.proxy_remarks:
            return remark
        c = 2
        while True:
            new = f"{remark} ({c})"
            if new not in self.proxy_remarks:
                return new
            c += 1

    def _normalize_and_remove_none_values(self, data: dict) -> dict:
        """
        Clean dictionary by removing None, empty strings, and 0 values.
        Converts Enum values and recursively cleans nested dictionaries.

        Args:
            data: Input dictionary to clean

        Returns:
            Cleaned dictionary with empty values removed
        """

        def clean_dict(d: dict) -> dict:
            new_dict = {}
            for k, v in d.items():
                if v not in (None, "", 0):
                    if isinstance(v, dict):
                        if cleaned_dict := clean_dict(v):
                            new_dict[k] = cleaned_dict
                    else:
                        if isinstance(v, Enum):
                            new_dict[k] = v.value
                        else:
                            new_dict[k] = v
            return new_dict

        return clean_dict(data)

    def snake_to_camel(self, snake_str):
        return re.sub(r"_([a-z])", lambda match: match.group(1).upper(), snake_str)
