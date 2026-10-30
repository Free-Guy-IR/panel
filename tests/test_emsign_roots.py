import base64
import hashlib
import importlib
import io
import os
import stat
import tracemalloc
import zipfile
from pathlib import Path

import pytest
from cryptography import x509

from tests.emsign_support import (
    DEBIAN_EMSIGN_FIXTURE,
    DEBIAN_EMSIGN_FIXTURE_SHA256,
    DEBIAN_EMSIGN_FIXTURE_SOURCE,
    build_image_tree,
    der,
    distrusted_roots,
    excluded_for,
    make_root,
    pem,
    zip_bytes,
)

emsign_roots = importlib.import_module("emsign_roots")


def test_systemsign_is_not_an_emsign_root():
    assert not emsign_roots.is_distrusted(make_root(organization="Example", common="SystemSign Global Root"))
    assert not emsign_roots.is_distrusted(make_root(organization="SystemSign Holdings", common="Root"))


@pytest.mark.parametrize("label", sorted(distrusted_roots()))
def test_every_emsign_or_emudhra_word_in_o_ou_or_cn_is_distrusted(label):
    assert emsign_roots.is_distrusted(distrusted_roots()[label])


def test_a_known_fingerprint_is_distrusted_whatever_its_names(monkeypatch):
    plain = make_root(organization="Example", common="Plain Root")
    monkeypatch.setitem(emsign_roots.DISTRUSTED_FINGERPRINTS, emsign_roots.der_fingerprint(der(plain)), "test root")

    assert emsign_roots.is_distrusted(plain)
    assert emsign_roots.is_distrusted_der(der(plain))
    assert emsign_roots.is_distrusted_der(b"not a certificate") is False


def test_the_four_fingerprints_cover_every_emsign_root_the_installed_certifi_carries():
    import certifi

    for certificate in x509.load_pem_x509_certificates(Path(certifi.where()).read_bytes()):
        if emsign_roots.names_a_distrusted_owner(certificate.subject):
            assert emsign_roots.der_fingerprint(der(certificate)) in emsign_roots.DISTRUSTED_FINGERPRINTS
    assert len(emsign_roots.DISTRUSTED_FINGERPRINTS) == 4


def test_the_debian_fixture_holds_exactly_the_four_known_roots():
    data = DEBIAN_EMSIGN_FIXTURE.read_bytes()

    assert hashlib.sha256(data).hexdigest() == DEBIAN_EMSIGN_FIXTURE_SHA256, DEBIAN_EMSIGN_FIXTURE_SOURCE
    fingerprints = [
        emsign_roots.der_fingerprint(der(certificate)) for certificate in x509.load_pem_x509_certificates(data)
    ]
    assert sorted(fingerprints) == sorted(emsign_roots.DISTRUSTED_FINGERPRINTS)


def test_a_trusted_certificate_block_is_read_by_its_leading_certificate():
    bad = distrusted_roots()["by-owner"]
    trusted = der(bad) + bytes.fromhex("3003060100")
    block = (
        b"-----BEGIN TRUSTED CERTIFICATE-----\n" + base64.encodebytes(trusted) + b"-----END TRUSTED CERTIFICATE-----\n"
    )

    assert list(emsign_roots.certificate_ders(block)) == [der(bad)]
    assert emsign_roots.holds_distrusted(block)
    assert not emsign_roots.holds_distrusted(b'BLOCK = rb"-----BEGIN CERTIFICATE-----(.*?)-----END CERTIFICATE-----"')


def test_discovery_finds_bundles_by_content_and_skips_excluded_trees(tmp_path):
    image_tree = build_image_tree(tmp_path)
    root = image_tree["root"]

    found = emsign_roots.discover(root, excluded_for(root))

    assert set(found.bundles) == {
        image_tree["certifi"],
        image_tree["venv-pip"],
        image_tree["unusual"],
        image_tree["clean"],
        image_tree["source"],
    }
    assert found.archives == [image_tree["archive"]]
    assert found.unreadable == []


LARGE_FILE_BYTES = 512 * 1024 * 1024
PEAK_MEMORY_BOUND = LARGE_FILE_BYTES // 4


def test_discovery_reads_a_large_file_in_bounded_memory(tmp_path):
    tree = tmp_path / "tree"
    tree.mkdir()
    with (tree / "asset.bin").open("wb") as handle:
        handle.truncate(LARGE_FILE_BYTES)
    already_tracing = tracemalloc.is_tracing()
    if not already_tracing:
        tracemalloc.start()
    tracemalloc.reset_peak()
    try:
        found = emsign_roots.discover(tree, ())
        _, peak = tracemalloc.get_traced_memory()
    finally:
        if not already_tracing:
            tracemalloc.stop()

    assert (found.files, found.bytes, found.bundles, found.archives, found.unreadable) == (
        1,
        LARGE_FILE_BYTES,
        [],
        [],
        [],
    )
    assert peak < PEAK_MEMORY_BOUND, f"discovery allocated {peak} bytes at its peak for a {LARGE_FILE_BYTES} byte file"


def whole_file_discovery(root: Path) -> tuple[list[Path], list[Path], int, int]:
    bundles, archives, files, total = [], [], 0, 0
    for directory, subdirectories, names in os.walk(root):
        subdirectories.sort()
        for name in sorted(names):
            path = Path(directory) / name
            if not stat.S_ISREG(os.lstat(path).st_mode):
                continue
            data = path.read_bytes()
            files += 1
            total += len(data)
            if data.startswith(emsign_roots.ZIP_MAGIC) or zipfile.is_zipfile(io.BytesIO(data)):
                archives.append(path)
            elif emsign_roots.CERTIFICATE_TRAILER in data:
                bundles.append(path)
    return bundles, archives, files, total


@pytest.mark.parametrize("chunk", [16, 17, 64, 1 << 20])
def test_chunked_discovery_matches_a_whole_file_read(tmp_path, monkeypatch, chunk):
    tree = build_image_tree(tmp_path)["root"]
    marker = emsign_roots.CERTIFICATE_TRAILER
    samples = {
        "split/marker-across-chunks.txt": b"x" * 9 + pem(make_root(organization="Example", common="Split")),
        "split/marker-at-the-end.txt": b"y" * 40 + marker,
        "split/marker-cut-short.txt": b"z" * 40 + marker[:-1],
        "split/marker-only.txt": marker,
        "split/empty.txt": b"",
        "split/prefixed.pyz": b"#!/usr/bin/env python3\n"
        + b"p" * 300
        + zip_bytes({"certs/roots.txt": pem(distrusted_roots()["by-unit"])}),
        "split/zip-magic-only.bin": emsign_roots.ZIP_MAGIC + b"not a zip at all" * 8,
        "split/stored.zip": zip_bytes({"cacert.pem": b"q" * 200 + marker}),
    }
    for name, data in samples.items():
        path = tree / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    monkeypatch.setattr(emsign_roots, "DISCOVERY_CHUNK", chunk, raising=False)

    found = emsign_roots.discover(tree, ())

    bundles, archives, files, total = whole_file_discovery(tree)
    assert found.bundles == bundles
    assert found.archives == archives
    assert (found.files, found.bytes) == (files, total)
    assert found.unreadable == []
