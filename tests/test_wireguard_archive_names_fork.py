import io
import unicodedata
import zipfile

import pytest

from app.models.subscription import SubscriptionInboundData, TCPTransportConfig, TLSConfig
from app.subscription.wireguard import WireGuardConfiguration

WINDOWS_DEVICE_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def _inbound() -> SubscriptionInboundData:
    return SubscriptionInboundData(
        remark="wg",
        inbound_tag="wg-in",
        protocol="wireguard",
        address="wg.example.com",
        port=51820,
        network="udp",
        tls_config=TLSConfig(),
        transport_config=TCPTransportConfig(path="", host=[]),
        mux_settings=None,
        wireguard_public_key="cHViLWtleS1wdWIta2V5LXB1Yi1rZXktcHViLWtleTA=",
        wireguard_allowed_ips=["0.0.0.0/0"],
    )


def _archive_stems(*remarks: str) -> list[str]:
    conf = WireGuardConfiguration()
    inbound = _inbound()
    for remark in remarks:
        conf.add(remark, inbound.address, inbound, {"private_key": "cHJpdi1rZXk=", "peer_ips": ["10.0.0.2/32"]})
    assert len(conf.configs) == len(remarks)
    with zipfile.ZipFile(io.BytesIO(conf.render())) as archive:
        names = archive.namelist()
    assert len(names) == len(remarks)
    assert all(name.endswith(".conf") for name in names)
    return [name.removesuffix(".conf") for name in names]


def test_fork_wireguard_add_path_keeps_hostile_remarks_flat_in_the_archive():
    stems = _archive_stems("../../etc/cron.d/x", "..\\..\\Windows\\x", "C:\\temp\\x", "/abs", "a\x00b\r\n", "x*?<>|")
    for stem in stems:
        assert not any(char in stem for char in '/\\:*?"<>|\x00\r\n'), stem
        assert ".." not in stem and not stem.startswith("."), stem


def test_fork_wireguard_add_path_gives_case_colliding_remarks_distinct_entries():
    stems = _archive_stems("Node", "node", "NODE")
    assert len({stem.casefold() for stem in stems}) == 3, stems


def test_fork_wireguard_add_path_prefixes_windows_device_names():
    stems = _archive_stems("NUL", "con", "COM1.backup", "lpt9")
    for stem in stems:
        assert stem.split(".", 1)[0].upper() not in WINDOWS_DEVICE_NAMES, stem


def test_fork_wireguard_add_path_treats_nfc_and_nfd_forms_as_one_name():
    stems = _archive_stems(unicodedata.normalize("NFC", "Café"), unicodedata.normalize("NFD", "Café"))
    assert len({unicodedata.normalize("NFC", stem).casefold() for stem in stems}) == 2, stems


@pytest.mark.xfail(
    strict=True,
    reason="BSUB-N-1: upstream v5.6.0 _remark_validation suffixes a duplicate remark with ' (N)' before _archive_name runs",
)
def test_fork_wireguard_add_path_keeps_android_sized_names_within_fifteen_characters():
    stems = _archive_stems(*["Berlin-Server1"] * 12)
    assert len(set(stems)) == 12, stems
    assert all(len(stem) <= 15 for stem in stems), stems


def test_fork_wireguard_add_path_keeps_long_names_within_sixty_four_characters():
    stems = _archive_stems(*["x" * 80] * 3)
    assert len(set(stems)) == 3, stems
    assert all(len(stem) <= 64 for stem in stems), stems
