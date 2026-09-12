from app.models.subscription import SubscriptionInboundData
from app.subscription.outline import OutlineConfiguration as UpstreamOutlineConfiguration


class OutlineConfiguration(UpstreamOutlineConfiguration):
    def add(self, remark: str, address: str, inbound: SubscriptionInboundData, settings: dict):
        if inbound.protocol != "shadowsocks":
            return
        if self.config:
            return
        outbound = self._build_shadowsocks(remark, address, inbound, settings)
        self.add_directly(outbound)
