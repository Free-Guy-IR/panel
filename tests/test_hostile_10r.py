import pytest

from tests.test_hostile_10m import load_inventory, moved_world
from tests.test_hostile_10q import commit, context_world, git

WF = "name: b\non: push\njobs:\n  b:\n    runs-on: ubuntu-latest\n    steps:\n"
HD = "bash <<'X'\nGOARCH=$TARGETARCH go build\nX\n"


@pytest.mark.parametrize(
    "extra",
    [
        {".github/workflows/b.yml": WF + "      - run: |\n          export GO$(echo ARCH)=riscv64\n          go build -o node .\n"},
        {"scripts/build.sh": "#!/bin/bash\nexport GO$(echo ARCH)=riscv64\ngo build\n"},
        {"scripts/build.sh": "#!/bin/bash\nexport TARGET$(echo ARCH)=riscv64\n" + HD},
        {"scripts/build.sh": "#!/bin/bash\nenv GO`echo ARCH`=riscv64 go build\n"},
        {"scripts/build.sh": "#!/bin/bash\nN=ARCH\ndeclare GO${N}=riscv64\ngo build\n"},
        {
            ".github/workflows/b.yml": WF + "      - run: ./scripts/build.sh\n        env:\n          TARGETARCH: riscv64\n",
            "scripts/build.sh": "#!/bin/bash\n" + HD,
        },
        {
            ".github/workflows/b.yml": "name: b\non: push\nenv:\n  TARGETARCH: riscv64\njobs:\n  b:\n    runs-on: ubuntu-latest\n"
            "    steps:\n      - run: ./scripts/build.sh\n",
            "scripts/build.sh": "#!/bin/bash\n" + HD,
        },
        {
            ".github/workflows/b.yml": WF + "      - run: ./scripts/build.sh\n        env: {TARGETARCH: riscv64}\n",
            "scripts/build.sh": "#!/bin/bash\n" + HD,
        },
        {"scripts/build.sh": "#!/bin/bash\nexport TARGETARCH=riscv64\n" + HD},
        {"scripts/build.sh": "#!/bin/bash\nsh <<'X'\nGOARCH=$TARGETARCH go build\nX\n"},
        {"scripts/build.sh": "#!/bin/bash\ncat <<'X' | bash\nGOARCH=$TARGETARCH go build\nX\n"},
        {"scripts/build.sh": "#!/bin/bash\n<<'X' zsh\nGOARCH=$TARGETARCH go build\nX\n"},
        {"scripts/build.sh": "#!/bin/bash\ncat <<'X' > build.sh\nGOARCH=$TARGETARCH go build\nX\n"},
        {"scripts/build.sh": "#!/bin/bash\ntee Dockerfile <<'X' | sh\nGOARCH=$TARGETARCH go build\nX\n"},
        {"Dockerfile": "FROM golang:1.25\nENV TARGETARCH riscv64\nRUN GOARCH=$TARGETARCH go build -o /node .\n"},
        {"Dockerfile": "FROM golang:1.25\nenv targetarch \"riscv64\"\nRUN GOARCH=$TARGETARCH go build -o /node .\n"},
        {"Dockerfile": "FROM golang:1.25\nENV TARGETARCH=riscv64\nRUN GOARCH=$TARGETARCH go build -o /node .\n"},
        {
            ".github/workflows/b.yml": WF + "      - run: docker build --build-arg TARGETARCH=riscv64 .\n",
            "Dockerfile": "FROM golang:1.25\nARG TARGETARCH\nRUN GOARCH=$TARGETARCH go build -o /node .\n",
        },
        {
            ".github/workflows/b.yml": WF + "      - uses: docker/build-push-action@v6\n        with:\n          build-args: |\n"
            "            TARGETARCH=riscv64\n",
            "Dockerfile": "FROM golang:1.25\nARG TARGETARCH\nRUN GOARCH=$TARGETARCH go build -o /node .\n",
        },
        {"Taskfile.yml": "version: '3'\ntasks:\n  b:\n    cmds:\n      - GOARCH=riscv64 go build\n"},
        {"Taskfile.yaml": "version: '3'\nenv:\n  GOARCH: riscv64\ntasks:\n  b:\n    cmds:\n      - go build\n"},
        {"justfile": "b:\n    GOARCH=riscv64 go build\n"},
        {".justfile": "export GOARCH := \"riscv64\"\nb:\n    go build\n"},
        {"magefile.go": "//go:build mage\n\npackage main\n\nfunc Build() error { return sh.RunWith(map[string]string{\"GOARCH\": \"riscv64\"}, \"go\", \"build\") }\n"},
        {"magefile.go": "//go:build mage\n\npackage main\n\nfunc Build() { os.Setenv(\"GOARCH\", \"riscv64\") }\n"},
        {"scripts/build.py": "import os\nos.environ['GOARCH'] = 'riscv64'\n"},
        {"scripts/build.py": "import subprocess\nsubprocess.run(['go', 'build'], env=dict(GOARCH='riscv64'))\n"},
    ],
    ids=[
        "workflow-dynamic-name",
        "script-dynamic-name",
        "script-dynamic-targetarch-then-shell-heredoc",
        "script-backtick-dynamic-name",
        "script-brace-dynamic-name",
        "workflow-step-env-targetarch",
        "workflow-top-env-targetarch",
        "workflow-flow-env-targetarch",
        "script-export-targetarch-then-shell-heredoc",
        "script-quoted-heredoc-to-sh",
        "script-quoted-heredoc-piped-to-bash",
        "script-quoted-heredoc-leading-redirect-zsh",
        "script-quoted-heredoc-to-a-non-dockerfile",
        "script-quoted-heredoc-tee-dockerfile-piped",
        "dockerfile-legacy-env-targetarch",
        "dockerfile-legacy-env-targetarch-quoted",
        "dockerfile-env-targetarch",
        "workflow-build-arg-targetarch",
        "workflow-build-args-input-targetarch",
        "taskfile-cmd",
        "taskfile-env",
        "justfile-recipe",
        "justfile-export",
        "magefile-map",
        "magefile-setenv",
        "py-environ",
        "py-dict-call",
    ],
)
def test_10r_riscv64_through_v10_holes_fails_the_gate(tmp_path, extra):
    result = moved_world(tmp_path, "c.go", "//go:build !riscv64\n\n", extra)
    assert result.returncode == 1, result.stdout[-400:] + result.stderr[-400:]


@pytest.mark.parametrize(
    ("path", "text"),
    [
        ("scripts/fixture.sh", "cat <<'X' > Dockerfile\nFROM golang:1\nARG TARGETARCH\nRUN GOARCH=${TARGETARCH} go build\nX\n"),
        ("scripts/fixture.sh", "cat > \"$tmp/Dockerfile.node\" <<'X'\nRUN GOARCH=$TARGETARCH go build\nX\n"),
        ("scripts/fixture.sh", "tee build/Containerfile <<'X'\nRUN GOARCH=$TARGETARCH go build\nX\n"),
        ("scripts/fixture.sh", "tee -a Dockerfile >/dev/null <<\"X\"\nRUN GOARCH=$TARGETARCH go build\nX\n"),
        ("scripts/fixture.sh", "tail=$(cat <<'TAIL'\nRUN CGO_ENABLED=0 GOARCH=${TARGETARCH} go build \\\n    -o /x .\nTAIL\n)\n"),
        ("Taskfile.yml", "version: '3'\ntasks:\n  b:\n    cmds:\n      - go build\n"),
        ("justfile", "b:\n    go build\n"),
        ("magefile.go", "//go:build mage\n\npackage main\n\nfunc Build() error { return sh.Run(\"go\", \"build\") }\n"),
        ("scripts/check.py", "import os\nprint(os.environ.get('HOME'))\n"),
        ("scripts/build.sh", "for name in a b; do echo \"$name=$value\"; done\n"),
    ],
    ids=[
        "heredoc-written-to-dockerfile",
        "heredoc-written-to-dockerfile-dot-name",
        "heredoc-tee-containerfile",
        "heredoc-tee-append-dockerfile",
        "heredoc-captured-by-cat-into-a-variable-like-the-real-node-drift-test",
        "taskfile-no-arch",
        "justfile-no-arch",
        "magefile-no-arch",
        "py-no-arch",
        "quoted-dynamic-text-is-not-a-name",
    ],
)
def test_10r_sources_that_set_no_arch_are_not_refused(path, text):
    reader = load_inventory().shipped_linux_arch_source(path)
    assert reader is not None
    assert reader(text) == set()


@pytest.mark.parametrize(
    ("path", "text", "arches"),
    [
        ("scripts/replay.py", "env = dict(self.env, CGO_ENABLED=\"0\", GOOS=\"linux\", GOARCH=\"amd64\")\n", {"amd64"}),
        ("scripts/build.py", "os.environ['GOARCH'] = 'riscv64'\n", {"riscv64"}),
        ("magefile.go", "os.Setenv(\"GOARCH\", \"riscv64\")\n", {"riscv64"}),
        ("Taskfile.yml", "env:\n  GOARCH: riscv64\n", {"riscv64"}),
    ],
    ids=["py-dict-literal-amd64", "py-environ-literal", "mage-setenv-literal", "taskfile-env-literal"],
)
def test_10r_task_sources_read_a_literal_arch(path, text, arches):
    assert load_inventory().shipped_linux_arch_source(path)(text) == arches


@pytest.mark.parametrize(
    ("path", "text"),
    [
        ("scripts/build.py", "os.environ['GOARCH'] = arch\n"),
        ("magefile.go", "os.Setenv(\"GOARCH\", arch)\n"),
        ("justfile", "export GOARCH := `uname -m`\n"),
    ],
    ids=["py-variable", "mage-variable", "just-backtick"],
)
def test_10r_task_sources_refuse_an_arch_they_cannot_read(path, text):
    module = load_inventory()
    with pytest.raises(module.ShippedArchesUnreadable):
        module.shipped_linux_arch_source(path)(text)


GO_BASE = (
    "package a\n\nfunc Keep() error {\n\tx, err := one()\n\tif err != nil {\n\t\treturn err\n\t}\n\treturn x\n}\n\n"
    "func Guard() error {\n\ty, err := two()\n\treturn y\n}\n"
)
GO_FORK = GO_BASE.replace("\ty, err := two()\n\treturn y\n", "\ty, err := two()\n\tif err != nil {\n\t\treturn err\n\t}\n\treturn y\n")
GO_GONE = "package a\n\nfunc Keep() error {\n\tx, err := one()\n\tif err != nil {\n\t\treturn err\n\t}\n\treturn x\n}\n"


def go_world(tmp_path, later):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "fork")
    git(repo, "config", "commit.gpgsign", "false")
    commit(repo, "base", {"a.go": GO_BASE})
    fork = commit(repo, "fork guards two", {"a.go": GO_FORK})
    heads = [commit(repo, f"later {index}", {"a.go": text}) for index, text in enumerate(later)]
    return repo, fork, heads[-1] if heads else fork


@pytest.mark.parametrize(
    "later",
    [
        [GO_GONE],
        [GO_GONE + "\nfunc Other() {}\n"],
        [GO_FORK.replace("func Guard() error {", "func Guarded() error {"), GO_GONE],
        [GO_BASE],
        [GO_GONE.replace("package a\n", "package a\n\nimport \"x\"\n")],
    ],
    ids=["guarded-function-deleted", "guarded-function-deleted-and-other-added", "renamed-then-deleted", "plain-revert", "deleted-plus-unrelated-edit"],
)
def test_10r_a_deleted_or_reverted_fork_hunk_is_missing_even_when_its_lines_exist_elsewhere(tmp_path, monkeypatch, later):
    repo, fork, head = go_world(tmp_path, later)
    module = load_inventory()
    monkeypatch.chdir(repo)
    expected, missing = module.own_lines_missing(fork, head, set())
    assert expected == 1
    assert missing


@pytest.mark.parametrize(
    "later",
    [
        [GO_FORK.replace("package a\n", "package a\n\nimport \"x\"\n")],
        [GO_FORK.replace("\treturn y\n", "\treturn y + 0\n")],
        [GO_FORK.replace("func Guard() error {", "func Guard() (int, error) {")],
        [GO_FORK],
    ],
    ids=["unrelated-edit-elsewhere", "after-context-changed", "before-context-changed", "unchanged"],
)
def test_10r_a_surviving_fork_hunk_is_present(tmp_path, monkeypatch, later):
    repo, fork, head = go_world(tmp_path, later[:-1] + [later[-1] + "\n"] if later[-1] == GO_FORK else later)
    module = load_inventory()
    monkeypatch.chdir(repo)
    assert module.own_lines_missing(fork, head, set()) == (1, [])


def test_10r_both_context_sides_need_a_surviving_anchor(tmp_path, monkeypatch):
    later = PORTED_ALL_CONTEXT_CHANGED
    repo, _, upstream, fork, head = context_world(tmp_path, later)
    module = load_inventory()
    monkeypatch.chdir(repo)
    assert module.design_fork_commit(f"carried by {fork[:12]}", head, upstream, frozenset()) is None


PORTED_ALL_CONTEXT_CHANGED = (
    "import os2\nimport sys2\nfrom app.models import Admin2\nfrom app.notify import notify\n"
    "from app.stats import new_stats\nfrom app.users import User2\nimport json2\nimport re\n"
)
