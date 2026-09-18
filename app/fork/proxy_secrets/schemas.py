from pydantic import Field

from app.fork.proxy_secrets.fields import ProxySecretField
from app.models.user import BulkUserFilter


class BulkRepairProxySecrets(BulkUserFilter):
    secret_fields: set[ProxySecretField] = Field(min_length=1)
