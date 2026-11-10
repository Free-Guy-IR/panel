from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID, uuid4

from app.utils.system import random_password


class ProxySecretField(StrEnum):
    hysteria_auth = "hysteria.auth"
    hysteria2_password = "hysteria2.password"
    tuic_password = "tuic.password"
    tuic_uuid = "tuic.uuid"
    trojan_password = "trojan.password"
    shadowsocks_password = "shadowsocks.password"
    vless_id = "vless.id"
    vmess_id = "vmess.id"


@dataclass(frozen=True)
class SecretFieldSpec:
    field: ProxySecretField
    protocol: str
    attribute: str
    generate: Callable[[], str | UUID]


SECRET_FIELD_SPECS: tuple[SecretFieldSpec, ...] = (
    SecretFieldSpec(ProxySecretField.hysteria_auth, "hysteria", "auth", random_password),
    SecretFieldSpec(ProxySecretField.hysteria2_password, "hysteria2", "password", random_password),
    SecretFieldSpec(ProxySecretField.tuic_password, "tuic", "password", random_password),
    SecretFieldSpec(ProxySecretField.tuic_uuid, "tuic", "uuid", uuid4),
    SecretFieldSpec(ProxySecretField.trojan_password, "trojan", "password", random_password),
    SecretFieldSpec(ProxySecretField.shadowsocks_password, "shadowsocks", "password", random_password),
    SecretFieldSpec(ProxySecretField.vless_id, "vless", "id", uuid4),
    SecretFieldSpec(ProxySecretField.vmess_id, "vmess", "id", uuid4),
)

SPEC_BY_FIELD: dict[ProxySecretField, SecretFieldSpec] = {spec.field: spec for spec in SECRET_FIELD_SPECS}


def specs_for(fields) -> list[SecretFieldSpec]:
    if not fields:
        return list(SECRET_FIELD_SPECS)
    wanted = {ProxySecretField(field) for field in fields}
    return [spec for spec in SECRET_FIELD_SPECS if spec.field in wanted]
