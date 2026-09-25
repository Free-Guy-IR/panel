import os
import re
import stat
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
INSTALLER = REPO_ROOT / "install_service.sh"
SIMPLE_ESCAPES = {"a": "\a", "b": "\b", "f": "\f", "n": "\n", "r": "\r", "t": "\t", "v": "\v", "\\": "\\", '"': '"', "'": "'", "s": " "}


def systemd_lines(text):
    entries = []
    continuation = None
    for raw in text.split("\n"):
        stripped = raw.lstrip(" \t")
        if stripped[:1] in ("#", ";"):
            continue
        line = raw if continuation is None else continuation + raw
        escaped = False
        for char in line:
            escaped = False if escaped else char == "\\"
        if escaped:
            continuation = line[:-1] + " "
            continue
        continuation = None
        line = line.strip(" \t\r")
        if not line or line.startswith("["):
            continue
        key, _, value = line.partition("=")
        entries.append((key.strip(), value.strip(" \t")))
    return entries


def unescape_specifiers(value):
    out = []
    index = 0
    while index < len(value):
        if value[index] == "%":
            assert value[index + 1 : index + 2] == "%", f"unexpected specifier in {value!r}"
            out.append("%")
            index += 2
            continue
        out.append(value[index])
        index += 1
    return "".join(out)


def exec_words(value):
    value = unescape_specifiers(value)
    words = []
    index = 0
    while index < len(value):
        while index < len(value) and value[index] in " \t":
            index += 1
        if index >= len(value):
            break
        word = []
        quote = None
        while index < len(value):
            char = value[index]
            if quote is None and char in " \t":
                break
            if char in "\"'" and quote is None:
                quote = char
            elif char == quote:
                quote = None
            elif char == "\\":
                index += 1
                assert value[index] in SIMPLE_ESCAPES, f"unsupported escape in {value!r}"
                word.append(SIMPLE_ESCAPES[value[index]])
            elif char == "$":
                assert value[index + 1 : index + 2] == "$", f"variable expansion in {value!r}"
                word.append("$")
                index += 1
            else:
                word.append(char)
            index += 1
        assert quote is None, f"unterminated quote in {value!r}"
        words.append("".join(word))
    return words


def run_installer(tmp_path, directory_name):
    unit_file = tmp_path / "out" / "pasarguard.service"
    unit_file.parent.mkdir()
    source, count = re.subn(
        r"^SERVICE_FILE=.*$", f'SERVICE_FILE="{unit_file}"', INSTALLER.read_text(), count=1, flags=re.MULTILINE
    )
    assert count == 1
    script = tmp_path / "install_service.sh"
    script.write_text(source)
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "systemctl").write_text("#!/bin/sh\nexit 0\n")
    (bin_dir / "systemctl").chmod(0o755)
    install_dir = tmp_path / "base" / directory_name
    install_dir.mkdir(parents=True)
    result = subprocess.run(
        ["bash", str(script)],
        cwd=install_dir,
        env={**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"},
        capture_output=True,
        text=True,
        check=False,
    )
    return result, unit_file, install_dir.resolve()


SAFE_NAMES = ["plain", "pasar guard", "pasar%hguard", "pasar${HOME}guard", "pa\"sar", "pa'sar", "pa\\nsar", "a;b", "-dash"]
UNSAFE_NAMES = [
    "panel\nExecStartPre=/bin/touch /tmp/pwned",
    "trailing-backslash\\",
    "tab\tinside",
    "edge space ",
    "carriage\rreturn",
]


@pytest.mark.parametrize("name", SAFE_NAMES)
def test_the_unit_resolves_to_the_install_directory_as_systemd_parses_it(tmp_path, name):
    result, unit_file, install_dir = run_installer(tmp_path, name)
    assert result.returncode == 0, result.stderr
    entries = systemd_lines(unit_file.read_text())
    keys = [key for key, _ in entries]
    assert keys.count("ExecStart") == 1
    assert keys.count("WorkingDirectory") == 1
    assert not any(key.startswith(("ExecStartPre", "ExecStop")) for key in keys)
    values = dict(entries)
    assert exec_words(values["ExecStart"]) == [f"{install_dir}/.venv/bin/python3", f"{install_dir}/main.py"]
    assert unescape_specifiers(values["WorkingDirectory"]) == str(install_dir)


@pytest.mark.parametrize("name", UNSAFE_NAMES)
def test_a_directory_systemd_would_misread_is_refused(tmp_path, name):
    result, unit_file, _ = run_installer(tmp_path, name)
    assert result.returncode != 0
    assert not unit_file.exists()
