import pytest

from tests.test_hostile_10m import load_inventory, moved_world
from tests.test_hostile_10q import commit, git

WF = "name: b\non: push\njobs:\n  b:\n    runs-on: ubuntu-latest\n    steps:\n"


@pytest.mark.parametrize(
    "extra",
    [
        {"scripts/build.sh": "#!/bin/bash\nenv GO''ARCH=riscv64 go build\n"},
        {"scripts/build.sh": "#!/bin/bash\nenv \"GO\"ARCH=riscv64 go build\n"},
        {"scripts/build.sh": "#!/bin/bash\nk=GO; declare -x \"${k}ARCH=riscv64\"; go build\n"},
        {"scripts/build.sh": "#!/bin/bash\nk=GOARCH; export \"$k=riscv64\"; go build\n"},
        {"scripts/build.sh": "#!/bin/bash\nenv \"GOARCH\"=riscv64 go build\n"},
        {"scripts/build.sh": "#!/bin/bash\nenv $'\\x47OARCH=riscv64' go build\n"},
        {"scripts/build.sh": "#!/bin/bash\nprintf -v \"GO\"\"ARCH\" riscv64\nexport GOARCH\ngo build\n"},
        {".github/workflows/b.yml": WF + "      - run: env GO''ARCH=riscv64 go build -o node .\n"},
        {".github/workflows/b.yml": WF + "      - run: |\n          k=GO\n          echo \"${k}ARCH=riscv64\" >> $GITHUB_ENV\n      - run: go build\n"},
        {"Dockerfile": "FROM golang:1.25\nRUN env GO\"\"ARCH=riscv64 go build -o /node .\n"},
        {"Makefile": "build:\n\tenv 'GO'ARCH=riscv64 go build\n"},
        {"scripts/b.py": "import os\nos.environ['GO' 'ARCH'] = 'riscv64'\n"},
        {"scripts/b.py": "import os\nk = 'GO' + 'ARCH'\nos.environ[k] = 'riscv64'\n"},
        {"scripts/b.py": "import os, subprocess\nsubprocess.run(['go', 'build'], env={**os.environ, 'GO' + 'ARCH': 'riscv64'})\n"},
        {"scripts/b.py": "import os\nos.environ.setdefault('GO' + 'ARCH', 'riscv64')\n"},
        {"scripts/b.py": "import os\nk = 'GOARCH'\nos.putenv(k, 'riscv64')\n"},
        {"scripts/b.py": "import os, subprocess\ne = dict(os.environ)\ne['GO' + 'ARCH'] = 'riscv64'\nsubprocess.run(['go', 'build'], env=e)\n"},
        {"scripts/b.py": "import os\nos.environ[f'GO{\"ARCH\"}'] = 'riscv64'\n"},
        {"magefile.go": "//go:build mage\n\npackage main\n\nfunc Build() { os.Setenv(\"GO\"+\"ARCH\", \"riscv64\") }\n"},
        {"magefile.go": "//go:build mage\n\npackage main\n\nfunc Build() error { k := \"GOARCH\"; return sh.RunWith(map[string]string{k: \"riscv64\"}, \"go\", \"build\") }\n"},
        {".github/workflows/b.yml": WF.replace("    steps:\n", "    env:\n      ${{ format('GO{0}', 'ARCH') }}: riscv64\n    steps:\n") + "      - run: go build\n"},
        {".github/workflows/b.yml": WF.replace("    steps:\n", "    env: {${{ format('GO{0}', 'ARCH') }}: riscv64}\n    steps:\n") + "      - run: go build\n"},
    ],
    ids=[
        "script-env-single-quote-split",
        "script-env-double-quote-split",
        "script-declare-built-name-in-quotes",
        "script-export-variable-name-in-quotes",
        "script-env-quoted-name-then-equals",
        "script-ansi-c-escape",
        "script-printf-v-quote-split",
        "workflow-run-quote-split",
        "workflow-github-env-built-name",
        "dockerfile-quote-split",
        "makefile-quote-split",
        "py-implicit-join-key",
        "py-variable-key",
        "py-env-dict-concat-key",
        "py-setdefault-concat-key",
        "py-putenv-variable-key",
        "py-env-variable-dict-concat-key",
        "py-fstring-key",
        "mage-setenv-concat-key",
        "mage-map-variable-key",
        "workflow-expression-env-key",
        "workflow-expression-flow-env-key",
    ],
)
def test_10s_riscv64_through_v11_holes_fails_the_gate(tmp_path, extra):
    result = moved_world(tmp_path, "c.go", "//go:build !riscv64\n\n", extra)
    assert result.returncode == 1, result.stdout[-400:] + result.stderr[-400:]


@pytest.mark.parametrize(
    ("path", "text"),
    [
        ("scripts/build.sh", "env GO''ARCH=riscv64 go build\n"),
        ("scripts/build.sh", "env \"GO\"ARCH=riscv64 go build\n"),
        ("scripts/build.sh", "k=GO; declare -x \"${k}ARCH=riscv64\"; go build\n"),
        ("scripts/build.sh", "env \"GOARCH\"=riscv64 go build\n"),
        ("scripts/build.sh", "env GO\\ARCH=riscv64 go build\n"),
        ("scripts/build.sh", "env $\"GO\"ARCH=riscv64 go build\n"),
        ("scripts/b.py", "import os\nos.environ['GO' 'ARCH'] = 'riscv64'\n"),
        ("scripts/b.py", "import os\nk='GO'+'ARCH'\nos.environ[k] = 'riscv64'\n"),
        ("scripts/b.py", "import os,subprocess\nsubprocess.run(['go','build'],env={**os.environ,'GO'+'ARCH':'riscv64'})\n"),
        ("scripts/b.py", "import os\nos.environ['GO\\x41RCH'] = 'riscv64'\n"),
        ("scripts/b.py", "import os\nos.environ.setdefault(k, 'riscv64')\n"),
        ("scripts/b.py", "import os,subprocess\nsubprocess.run(['go'], env=dict(os.environ, **{'GO'+'ARCH': 'riscv64'}))\n"),
        ("scripts/b.py", "def broken(:\n"),
        ("magefile.go", "os.Setenv(k, \"riscv64\")\n"),
        ("magefile.go", "syscall.Setenv(\"GO\" + \"ARCH\", \"riscv64\")\n"),
        (".github/workflows/b.yml", "jobs:\n  b:\n    runs-on: ubuntu-latest\n    env:\n      ${{ format('GO{0}', 'ARCH') }}: riscv64\n    steps:\n      - run: go build\n"),
    ],
    ids=[
        "single-quote-split",
        "double-quote-split",
        "declare-built-name",
        "quoted-name-then-equals",
        "backslash-split",
        "locale-quote-split",
        "py-implicit-join",
        "py-variable",
        "py-env-concat",
        "py-escaped-literal",
        "py-setdefault-variable",
        "py-dict-unpack-concat",
        "py-unparsable",
        "mage-setenv-variable",
        "mage-setenv-concat",
        "workflow-expression-key",
    ],
)
def test_10s_readers_refuse_a_name_they_cannot_read(path, text):
    module = load_inventory()
    with pytest.raises(module.ShippedArchesUnreadable):
        module.shipped_linux_arch_source(path)(text)


@pytest.mark.parametrize(
    ("path", "text", "arches"),
    [
        ("scripts/build.sh", "for name in a b; do echo \"$name=$value\"; done\n", set()),
        ("Taskfile.yml", "version: '3'\ntasks:\n  b:\n    cmds:\n      - env \"GOARCH=amd64\" go build\n", {"amd64"}),
        ("scripts/build.sh", "IFS=$'\\n'\necho \"GOARCH\" is set\n", set()),
        ("scripts/build.sh", "_NAME=\"${GOOS}-${GOARCH}-${_GOARM}\"\n", set()),
        ("scripts/drift.sh", "if (a !~ /^(CGO_ENABLED|GOOS|GOARCH|GOARM|GOAMD64)$/) return \"other\"\n", set()),
        ("scripts/b.py", "import os\nos.environ['GOARCH'] = 'amd64'\n", {"amd64"}),
        ("scripts/b.py", "import os,subprocess\nsubprocess.run(['go'], env={**os.environ, 'GOARCH': 'arm64'})\n", {"arm64"}),
        ("scripts/b.py", "import os\nself_env = dict(os.environ)\nenv = dict(self_env, CGO_ENABLED='0', GOOS='linux', GOARCH='amd64')\n", {"amd64"}),
        ("scripts/b.py", "import os\nprint(os.environ.get(name))\ncounts = {}\ncounts[name] = 1\n", set()),
        ("magefile.go", "os.Setenv(\"GOARCH\", \"arm64\")\n", {"arm64"}),
        (".github/workflows/b.yml", "jobs:\n  b:\n    runs-on: ubuntu-latest\n    steps:\n      - run: echo \"${{ github.sha }}\"\n", set()),
    ],
    ids=[
        "echo-dynamic-text",
        "whole-word-quoted-literal",
        "ansi-c-newline-and-quoted-mention",
        "real-node-name-line",
        "real-node-awk-pattern",
        "py-literal",
        "py-env-literal",
        "py-dict-call-literal",
        "py-unrelated-subscript",
        "mage-literal",
        "workflow-expression-value",
    ],
)
def test_10s_readable_forms_are_read(path, text, arches):
    assert load_inventory().shipped_linux_arch_source(path)(text) == arches


S_BASE = {"a.py": "import os\n\n\ndef guard(x):\n    return x\n", "b.py": "import sys\n\n\ndef check(y):\n    return y\n"}
S_ADDS = {
    "a.py": "import os\n\n\ndef guard(x):\n    validate_v1(x)\n    return x\n",
    "b.py": "import sys\n\n\ndef check(y):\n    sanitize_v1(y)\n    return y\n",
}
S_LATER = {
    "a.py": "import os\n\n\ndef guard(x):\n    validate_v2(x, strict=True)\n    return x\n",
    "b.py": "import sys\n\n\ndef check(y):\n    sanitize_v2(y)\n    return y\n",
}


def supersede_world(tmp_path, later=S_LATER, pick=None, after=None, trailer=None, unrelated=None):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "fork")
    git(repo, "config", "commit.gpgsign", "false")
    commit(repo, "base", S_BASE)
    git(repo, "checkout", "-q", "-b", "up")
    superseded = commit(repo, "security: validate input", S_ADDS)
    other = commit(repo, "docs", {"c.txt": "notes\n"})
    replaced = commit(repo, "security: stricter validation", later)
    newer = commit(repo, "unrelated upstream", unrelated) if unrelated else None
    git(repo, "update-ref", "refs/remotes/upstream/main", "up")
    git(repo, "checkout", "-q", "fork")
    named = {"later": replaced, "other": other, "newer": newer, "superseded": superseded}[trailer or "later"]
    for rel, text in (pick or later).items():
        (repo / rel).write_text(text)
    git(repo, "add", *(pick or later))
    git(repo, "commit", "-q", "-m", f"port stricter validation\n\n(cherry picked from commit {named})")
    fork = git(repo, "rev-parse", "HEAD")
    head = commit(repo, "later fork change", after) if after else fork
    return repo, superseded, replaced, fork, head


def test_10s_superseding_pick_carries_a_superseded_security_row(tmp_path, monkeypatch):
    repo, superseded, _, fork, head = supersede_world(tmp_path)
    module = load_inventory()
    monkeypatch.chdir(repo)
    reason = f"superseded upstream; carried by {fork[:12]}"
    assert module.superseding_pick(reason, head, superseded) == fork
    assert module.security_design_problems(reason, head, superseded, {}) == []


def test_10s_superseding_pick_survives_an_unrelated_later_fork_change(tmp_path, monkeypatch):
    after = {"d.txt": "fork notes\n"}
    repo, superseded, _, fork, head = supersede_world(tmp_path, after=after)
    module = load_inventory()
    monkeypatch.chdir(repo)
    assert module.security_design_problems(f"carried by {fork[:12]}", head, superseded, {}) == []


@pytest.mark.parametrize(
    "case",
    ["unrelated-later-pick", "pick-of-the-superseded-commit", "pick-content-unrelated", "later-keeps-a-line", "later-keeps-a-file", "pick-lines-gone"],
)
def test_10s_supersession_stays_strict(tmp_path, monkeypatch, case):
    kwargs = {
        "unrelated-later-pick": {"trailer": "newer", "unrelated": {"c.txt": "notes v2\n"}},
        "pick-of-the-superseded-commit": {"trailer": "superseded"},
        "pick-content-unrelated": {"pick": {"e.txt": "other work\n"}},
        "later-keeps-a-line": {
            "later": {
                "a.py": "import os\n\n\ndef guard(x):\n    validate_v1(x)\n    validate_v2(x, strict=True)\n    return x\n",
                "b.py": S_LATER["b.py"],
            }
        },
        "later-keeps-a-file": {"later": {"a.py": S_LATER["a.py"], "b.py": S_ADDS["b.py"]}},
        "pick-lines-gone": {"after": {"a.py": S_BASE["a.py"], "b.py": S_BASE["b.py"]}},
    }[case]
    repo, superseded, _, fork, head = supersede_world(tmp_path, **kwargs)
    module = load_inventory()
    monkeypatch.chdir(repo)
    reason = f"superseded upstream; carried by {fork[:12]}"
    assert module.superseding_pick(reason, head, superseded) is None
    problems = module.security_design_problems(reason, head, superseded, {})
    assert problems is None or problems


@pytest.mark.parametrize(
    "text",
    [
        'def build():\n    """Build every GOARCH the release ships."""\n',
        'print("GOARCH is %s\\n" % name)\n',
    ],
    ids=["docstring-mention", "escaped-mention"],
)
def test_10s_python_mentions_of_goarch_are_not_built_names(text):
    assert load_inventory().shipped_linux_arch_source("scripts/b.py")(text) == set()
