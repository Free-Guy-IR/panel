import json

from app.models.subscription import SubscriptionInboundData

from .base import BaseSubscription


class L2TPConfiguration(BaseSubscription):
    def __init__(self):
        self.proxy_remarks: list[str] = []
        self.details: list[dict[str, object]] = []

    def add(self, remark: str, address: str, inbound: SubscriptionInboundData, settings: dict):
        if inbound.protocol != "l2tp":
            return
        component = self._build_l2tp_components(remark, address, inbound, settings)
        if component is None:
            return
        self.proxy_remarks.append(component["remark"])
        self.details.append(component)

    def render(self) -> str:
        return json.dumps(self.details, ensure_ascii=False)
