import importlib
import shutil
import zipfile
from pathlib import Path

import pytest

from tests.emsign_support import (
    REPOSITORY,
    build_image_tree,
    distrusted_roots,
    load,
    make_root,
    pem,
    zip_bytes,
)

emsign_roots = importlib.import_module("emsign_roots")
store_check = load("image_trust_store_check")


@pytest.fixture(autouse=True)
def no_real_filesystem(monkeypatch, tmp_path):
    empty = tmp_path / "empty-root"
    empty.mkdir()
    monkeypatch.setattr(store_check, "DISCOVERY_ROOT", empty)


def test_the_store_check_reads_discovered_archives_and_skips_marker_only_sources(tmp_path):
    image_tree = build_image_tree(tmp_path)

    archive_sources = store_check.scan_archive(image_tree["archive"])
    assert [len(source.distrusted) for source in archive_sources] == [1]
    source = store_check.scan_file("bundle", image_tree["source"])
    assert source.certificates == 0
    assert source.failures() == []


def test_the_store_check_reads_ssl_context_without_importing_the_app(tmp_path):
    code = tmp_path / "code"
    (code / "app/utils").mkdir(parents=True)
    (code / "app/__init__.py").write_text('raise RuntimeError("the app package must not be imported")\n')
    shutil.copy(REPOSITORY / "app/utils/ssl_context.py", code / "app/utils/ssl_context.py")

    source = store_check.scan_context(code)

    assert source.errors == []
    assert source.certificates > 0


def test_the_store_check_requires_a_non_empty_core_source(tmp_path):
    empty = tmp_path / "empty.pem"
    empty.write_bytes(b"")
    one = tmp_path / "one.pem"
    one.write_bytes(pem(make_root(organization="Example", common="Only Root")))

    assert store_check.scan_file("certifi", empty, store_check.MINIMUM_ROOTS).failures()
    assert store_check.scan_file("certifi", one, store_check.MINIMUM_ROOTS).failures() == []


def archive_problems(tmp_path: Path, name: str, data: bytes) -> tuple[list, list[str]]:
    path = tmp_path / name
    path.write_bytes(data)
    sources = store_check.scan_archive(path)
    return sources, [problem for source in sources for problem in source.failures()]


def test_an_encrypted_member_is_an_actionable_failure(tmp_path):
    data = bytearray(zip_bytes({"cacert.pem": pem(make_root(organization="Example"))}))
    data[6] |= 1
    central = data.index(b"PK\x01\x02")
    data[central + 8] |= 1

    _, problems = archive_problems(tmp_path, "locked.zip", bytes(data))

    assert len(problems) == 1
    assert "unreadable archive" in problems[0] and "encrypted" in problems[0]


def test_a_corrupt_deflate_stream_is_an_actionable_failure(tmp_path):
    payload = pem(make_root(organization="Example")) * 4
    data = bytearray(zip_bytes({"cacert.pem": payload}, zipfile.ZIP_DEFLATED))
    for offset in range(40, 60):
        data[offset] ^= 0xFF

    _, problems = archive_problems(tmp_path, "broken.whl", bytes(data))

    assert len(problems) == 1
    assert "unreadable archive" in problems[0]


def test_a_file_with_zip_magic_that_is_no_zip_is_an_actionable_failure(tmp_path):
    _, problems = archive_problems(tmp_path, "fake.zip", b"PK\x03\x04" + b"not a zip at all" * 8)

    assert len(problems) == 1
    assert problems[0].startswith("archive: unreadable archive")


def test_a_zipapp_with_a_shebang_prefix_is_scanned(tmp_path):
    data = b"#!/usr/bin/env python3\n" + zip_bytes({"certs/roots.txt": pem(distrusted_roots()["by-unit"])})
    assert not data.startswith(emsign_roots.ZIP_MAGIC)
    assert emsign_roots.is_archive_candidate(data)
    path = tmp_path / "tool.pyz"
    path.write_bytes(data)
    found = emsign_roots.discover(tmp_path, ())
    assert found.archives == [path]
    assert found.bundles == []

    _, problems = archive_problems(tmp_path, "tool.pyz", data)

    assert len(problems) == 1
    assert "distrusted root" in problems[0]


def test_a_root_inside_nested_zips_is_found(tmp_path):
    inner = zip_bytes({"pip/_vendor/certifi/cacert.pem": pem(distrusted_roots()["by-owner"])})
    middle = zip_bytes({"inner.whl": inner}, zipfile.ZIP_DEFLATED)
    outer = zip_bytes({"middle.zip": middle}, zipfile.ZIP_DEFLATED)

    _, problems = archive_problems(tmp_path, "outer.zip", outer)

    assert len(problems) == 1
    assert "outer.zip!middle.zip!inner.whl!pip/_vendor/certifi/cacert.pem" in problems[0]


def test_zips_nested_deeper_than_three_levels_fail(tmp_path):
    data = zip_bytes({"cacert.pem": pem(make_root(organization="Example"))})
    for level in range(3):
        data = zip_bytes({f"level{level}.zip": data})

    _, problems = archive_problems(tmp_path, "deep.zip", data)

    assert len(problems) == 1
    assert "nested deeper than 3 levels" in problems[0]
