import hashlib

TYPE_PLAIN = 0
TYPE_REGEX = 1
TYPE_DOMAIN = 2
TYPE_FULL = 3


def _varint(value: int) -> bytes:
    out = bytearray()
    while True:
        chunk = value & 0x7F
        value >>= 7
        if value:
            out.append(chunk | 0x80)
        else:
            out.append(chunk)
            return bytes(out)


def _tag(field: int, wire: int) -> bytes:
    return _varint((field << 3) | wire)


def _bytes_field(field: int, payload: bytes) -> bytes:
    return _tag(field, 2) + _varint(len(payload)) + payload


def _varint_field(field: int, value: int) -> bytes:
    return _tag(field, 0) + _varint(value)


def _domain(value: str, kind: int = TYPE_DOMAIN) -> bytes:
    body = _varint_field(1, kind) + _bytes_field(2, value.encode("utf-8"))
    return _bytes_field(2, body)


def _site(code: str, domains: list[str]) -> bytes:
    parts = [_bytes_field(1, code.upper().encode("utf-8"))]
    parts.extend(_domain(d) for d in domains)
    return _bytes_field(1, b"".join(parts))


def build(categories: dict[str, list[str]]) -> bytes:
    return b"".join(_site(code, domains) for code, domains in categories.items())


def digest(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()
