import base64
import importlib
import os
import py_compile
import sys
import types
from pathlib import Path

import pytest
from cryptography import x509

from tests.emsign_support import (
    DEBIAN_EMSIGN_FIXTURE,
    build_image_tree,
    clean_roots,
    der,
    distrusted_roots,
    embedding_module,
    entry,
    excluded_for,
    load,
    make_root,
    pem,
    write_bundle,
)

emsign_roots = importlib.import_module("emsign_roots")
distrust = load("distrust_certifi_roots")


@pytest.fixture(autouse=True)
def no_real_filesystem(monkeypatch, tmp_path):
    empty = tmp_path / "empty-root"
    empty.mkdir()
    monkeypatch.setattr(distrust, "DISCOVERY_ROOT", empty)


@pytest.fixture
def bundle(tmp_path: Path) -> tuple[Path, list[x509.Certificate], dict[str, x509.Certificate]]:
    kept = clean_roots(3) + [make_root(country="HU", organization="NetLock Kft.", common="NetLock Arany Főtanúsítvány")]
    kept.append(make_root(country="US", organization="Example", common="SystemSign Global Root"))
    bad = dict(list(distrusted_roots().items())[:4])
    path = tmp_path / "cacert.pem"
    write_bundle(path, kept, bad)
    path.chmod(0o640)
    return path, kept, bad


def test_removes_every_distrusted_root_and_keeps_the_rest_byte_for_byte(bundle):
    path, kept, bad = bundle

    removed = distrust.strip_bundle(path)

    assert len(removed) == len(bad)
    after = path.read_bytes()
    assert x509.load_pem_x509_certificates(after) == kept
    assert after == b"".join(entry(f"kept-{index}", root) for index, root in enumerate(kept)) + b"\n"
    assert path.stat().st_mode & 0o777 == 0o640
    assert path.stat().st_uid == os.getuid()


def test_a_bundle_far_below_one_hundred_roots_is_stripped(tmp_path):
    path = tmp_path / "small.pem"
    kept = clean_roots(2)
    write_bundle(path, kept, {"bad": distrusted_roots()["by-owner"]})

    assert len(distrust.strip_bundle(path)) == 1
    assert x509.load_pem_x509_certificates(path.read_bytes()) == kept


def test_more_removals_than_known_emsign_roots_are_refused(tmp_path):
    path = tmp_path / "cacert.pem"
    before = write_bundle(path, clean_roots(10), distrusted_roots())

    with pytest.raises(distrust.BundleError, match="5 distinct distrusted roots, more than the 4 known"):
        distrust.strip_bundle(path)
    assert path.read_bytes() == before


def test_a_bundle_of_only_distrusted_roots_is_never_emptied(tmp_path):
    path = tmp_path / "cacert.pem"
    before = write_bundle(path, [], dict(list(distrusted_roots().items())[:2]))

    with pytest.raises(distrust.BundleError, match="would keep 0"):
        distrust.strip_bundle(path)
    assert path.read_bytes() == before


def test_a_second_run_changes_nothing(bundle):
    path, _, _ = bundle
    distrust.strip_bundle(path)
    after_first = path.read_bytes()

    assert distrust.strip_bundle(path) == []
    assert path.read_bytes() == after_first


def test_crlf_line_endings_survive_exactly(tmp_path):
    roots = clean_roots(5)
    lines = [entry(f"kept-{index}", root) for index, root in enumerate(roots)]
    content = (b"".join(lines[:2]) + entry("bad", distrusted_roots()["by-owner"]) + b"".join(lines[2:])).replace(
        b"\n", b"\r\n"
    )
    path = tmp_path / "cacert.pem"
    path.write_bytes(content)

    assert len(distrust.strip_bundle(path)) == 1
    assert path.read_bytes() == b"".join(lines).replace(b"\n", b"\r\n")


def test_a_symlinked_bundle_rewrites_its_target_and_keeps_the_link(bundle, tmp_path):
    path, kept, _ = bundle
    link = tmp_path / "link.pem"
    link.symlink_to(path)

    distrust.strip_bundle(link)

    assert link.is_symlink()
    assert x509.load_pem_x509_certificates(path.read_bytes()) == kept


@pytest.mark.parametrize(
    ("label", "mangle"),
    [
        ("byte order mark", lambda data: b"\xef\xbb\xbf" + data),
        (
            "glued markers",
            lambda data: data.replace(
                b"-----END CERTIFICATE-----\n", b"-----END CERTIFICATE----------BEGIN CERTIFICATE-----\n", 1
            ),
        ),
        ("trusted certificate", lambda data: data.replace(b"CERTIFICATE-----", b"TRUSTED CERTIFICATE-----", 2)),
        ("indented marker", lambda data: data.replace(b"\n-----BEGIN", b"\n  -----BEGIN", 1)),
        ("unterminated block", lambda data: data[: data.rindex(b"-----END")]),
        (
            "broken base64",
            lambda data: data.replace(b"-----BEGIN CERTIFICATE-----\n", b"-----BEGIN CERTIFICATE-----\n!", 1),
        ),
    ],
)
def test_input_the_splitter_cannot_read_fails_closed(bundle, label, mangle, monkeypatch):
    path, _, _ = bundle
    path.write_bytes(mangle(path.read_bytes()))
    before = path.read_bytes()

    with pytest.raises(distrust.BundleError):
        distrust.strip_bundle(path)
    monkeypatch.setitem(sys.modules, "certifi", types.SimpleNamespace(where=lambda: str(path)))
    assert distrust.main([]) == 1
    assert path.read_bytes() == before


def test_a_bundle_without_certificates_fails(tmp_path):
    path = tmp_path / "cacert.pem"
    path.write_bytes(b"# nothing but comments\n")

    with pytest.raises(distrust.BundleError):
        distrust.strip_bundle(path)


def test_a_rewrite_that_does_not_land_raises(bundle, monkeypatch):
    path, _, _ = bundle
    monkeypatch.setattr(distrust, "write_atomically", lambda target, content: None)

    with pytest.raises(RuntimeError, match="does not hold the bytes"):
        distrust.strip_bundle(path)


def test_the_post_check_rejects_a_remaining_root_a_wrong_count_and_an_empty_bundle():
    good = b"".join(pem(root) for root in clean_roots(3))
    bad = pem(distrusted_roots()["by-owner"])

    with pytest.raises(RuntimeError, match="distrusted"):
        distrust.verify_written(good + bad, 4)
    with pytest.raises(RuntimeError, match="expected 4"):
        distrust.verify_written(good, 4)
    with pytest.raises(RuntimeError, match="no certificate"):
        distrust.verify_written(b"# nothing left\n", 0)
    distrust.verify_written(good, 3)


def test_a_root_with_a_non_positive_serial_is_kept(tmp_path):
    zero = der(make_root(serial=1, organization="Example", common="Zero Serial Root"))
    marker = bytes.fromhex("a003020102020101")
    assert zero.count(marker) == 1
    zero = zero.replace(marker, bytes.fromhex("a003020102020100"))
    zero_pem = b"-----BEGIN CERTIFICATE-----\n" + base64.encodebytes(zero) + b"-----END CERTIFICATE-----\n"
    path = tmp_path / "cacert.pem"
    content = b"".join(entry(f"kept-{index}", root) for index, root in enumerate(clean_roots(3)))
    path.write_bytes(content + zero_pem + entry("bad", distrusted_roots()["by-owner"]))

    assert len(distrust.strip_bundle(path)) == 1
    assert zero_pem in path.read_bytes()


def test_blocks_a_future_cryptography_refuses_are_matched_by_fingerprint(tmp_path, monkeypatch):
    roots = clean_roots(4)
    known = make_root(organization="Example", common="Known Bad Without Names")
    path = tmp_path / "cacert.pem"
    path.write_bytes(b"".join(entry(f"kept-{index}", root) for index, root in enumerate(roots)) + entry("known", known))
    monkeypatch.setitem(emsign_roots.DISTRUSTED_FINGERPRINTS, emsign_roots.der_fingerprint(der(known)), "test root")
    refused = {der(roots[0]), der(known)}
    real_der, real_pem = x509.load_der_x509_certificate, x509.load_pem_x509_certificates

    def refusing_der(data, *args):
        if data in refused:
            raise ValueError("refused by a future cryptography")
        return real_der(data, *args)

    def refusing_pem(data, *args):
        if pem(roots[0]) in data:
            raise ValueError("refused by a future cryptography")
        return real_pem(data, *args)

    monkeypatch.setattr(x509, "load_der_x509_certificate", refusing_der)
    monkeypatch.setattr(x509, "load_pem_x509_certificates", refusing_pem)

    removed = distrust.strip_bundle(path)

    assert removed == [f"unparsed certificate with a distrusted fingerprint ({len(der(known))} bytes)"]
    assert pem(roots[0]) in path.read_bytes()


def test_main_fails_when_certifi_cannot_be_imported(monkeypatch):
    monkeypatch.setitem(sys.modules, "certifi", None)

    assert distrust.main([]) == 1


def test_main_fails_when_a_named_bundle_is_missing(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "certifi", types.SimpleNamespace(where=lambda: str(tmp_path / "absent.pem")))

    assert distrust.main([]) == 1
    assert distrust.main([str(tmp_path / "absent.pem")]) == 1


def test_main_strips_certifi_and_an_explicit_path(monkeypatch, bundle, tmp_path):
    path, kept, _ = bundle
    other = tmp_path / "pip-cacert.pem"
    other.write_bytes(path.read_bytes())
    monkeypatch.setitem(sys.modules, "certifi", types.SimpleNamespace(where=lambda: str(path)))

    assert distrust.main([]) == 0
    assert distrust.main([str(other)]) == 0
    assert x509.load_pem_x509_certificates(path.read_bytes()) == kept
    assert x509.load_pem_x509_certificates(other.read_bytes()) == kept


def test_a_discovered_file_with_lines_other_than_comments_is_refused(tmp_path):
    path = tmp_path / "roots.txt"
    content = (
        b"roots = 'mine'\n"
        + pem(make_root(organization="Example", common="Fine"))
        + pem(distrusted_roots()["by-owner"])
    )
    path.write_bytes(content)

    with pytest.raises(distrust.BundleError, match="line 1 .* is neither blank nor a # comment"):
        distrust.strip_bundle(path)
    assert path.read_bytes() == content


def test_certifi_itself_is_stripped_even_with_lines_other_than_comments(tmp_path, monkeypatch):
    path = tmp_path / "cacert.pem"
    good = make_root(organization="Example", common="Fine")
    path.write_bytes(b"certifi-style preamble\n" + pem(good) + pem(distrusted_roots()["by-owner"]))
    monkeypatch.setitem(sys.modules, "certifi", types.SimpleNamespace(where=lambda: str(path)))

    assert distrust.main([]) == 0
    assert x509.load_pem_x509_certificates(path.read_bytes()) == [good]


def test_image_mode_refuses_to_run_outside_the_image_build(monkeypatch):
    monkeypatch.delenv("PG_IMAGE_BUILD", raising=False)

    def walk_forbidden(*arguments, **keywords):
        raise AssertionError("discovery must not run")

    monkeypatch.setattr(distrust, "discover", walk_forbidden)

    assert distrust.main(["--image"]) == 2


def test_duplicate_entries_of_the_known_roots_are_removed_under_the_distinct_cap(tmp_path):
    debian = DEBIAN_EMSIGN_FIXTURE.read_bytes()
    kept = clean_roots(2)
    combined = tmp_path / "combined.pem"
    combined.write_bytes(b"".join(pem(root) for root in kept) + debian + debian)
    entries, _ = distrust.split_entries(combined.read_bytes())
    distrusted = [der for _, _, der in entries if distrust.entry_is_distrusted(der)]
    assert len({emsign_roots.der_fingerprint(der) for der in distrusted}) == 4 < len(distrusted) == 8

    removed = distrust.strip_bundle(combined)

    assert len(removed) == 8
    assert x509.load_pem_x509_certificates(combined.read_bytes()) == kept

    fifth = tmp_path / "fifth.pem"
    before = b"".join(pem(root) for root in kept) + debian + debian + pem(distrusted_roots()["by-common-name-g9"])
    fifth.write_bytes(before)
    with pytest.raises(distrust.BundleError, match="5 distinct distrusted roots, more than the 4 known"):
        distrust.strip_bundle(fifth)
    assert fifth.read_bytes() == before


def test_image_mode_never_rewrites_python_source_or_bytecode(tmp_path, monkeypatch):
    root = tmp_path / "image"
    certifi_path = root / "venv/certifi/cacert.pem"
    write_bundle(certifi_path, clean_roots(3), {})
    package = root / "venv/attestlib"
    package.mkdir(parents=True)
    source = embedding_module(distrusted_roots()["by-owner"], make_root(organization="Example", common="Other"))
    (package / "__init__.py").write_bytes(source)
    compiled = package / "__pycache__" / "__init__.cpython-314.pyc"
    py_compile.compile(str(package / "__init__.py"), cfile=str(compiled), doraise=True)
    compiled_before = compiled.read_bytes()
    monkeypatch.setitem(sys.modules, "certifi", types.SimpleNamespace(where=lambda: str(certifi_path)))
    monkeypatch.setattr(distrust, "DISCOVERY_ROOT", root)
    monkeypatch.setattr(distrust, "EXCLUDED_FROM_DISCOVERY", ())
    monkeypatch.setenv("PG_IMAGE_BUILD", "1")
    certifi_before = certifi_path.read_bytes()

    assert distrust.main(["--image"]) == 1

    assert (package / "__init__.py").read_bytes() == source
    assert compiled.read_bytes() == compiled_before
    assert certifi_path.read_bytes() == certifi_before


def test_the_image_mode_strips_every_discovered_bundle_without_a_base_pip(tmp_path, monkeypatch):
    image_tree = build_image_tree(tmp_path)
    root = image_tree["root"]
    monkeypatch.setitem(sys.modules, "certifi", types.SimpleNamespace(where=lambda: str(image_tree["certifi"])))
    monkeypatch.setattr(distrust, "DISCOVERY_ROOT", root)
    monkeypatch.setattr(distrust, "EXCLUDED_FROM_DISCOVERY", excluded_for(root))
    monkeypatch.setenv("PG_IMAGE_BUILD", "1")
    assert not (root / "usr/local/lib/python3.14/site-packages/pip").exists()

    assert set(distrust.image_targets()) == {image_tree["certifi"], image_tree["venv-pip"], image_tree["unusual"]}
    assert distrust.main(["--image"]) == 0

    for name in ("certifi", "venv-pip", "unusual", "clean"):
        assert not emsign_roots.holds_distrusted(image_tree[name].read_bytes())
    assert emsign_roots.holds_distrusted(image_tree["excluded"].read_bytes())
    assert emsign_roots.holds_distrusted(image_tree["proc"].read_bytes())


def test_the_image_mode_still_fails_without_certifi(tmp_path, monkeypatch):
    image_tree = build_image_tree(tmp_path)
    monkeypatch.setitem(sys.modules, "certifi", None)
    monkeypatch.setattr(distrust, "DISCOVERY_ROOT", image_tree["root"])
    monkeypatch.setenv("PG_IMAGE_BUILD", "1")

    assert distrust.main(["--image"]) == 1
