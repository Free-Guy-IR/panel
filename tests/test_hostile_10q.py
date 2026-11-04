import os
import subprocess

import pytest

from tests.test_hostile_10m import NODE_CLONE, load_inventory, moved_world

WF = "name: b\non: push\njobs:\n  b:\n    runs-on: ubuntu-latest\n    steps:\n"
ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "Test",
    "GIT_AUTHOR_EMAIL": "test@example.com",
    "GIT_COMMITTER_NAME": "Test",
    "GIT_COMMITTER_EMAIL": "test@example.com",
    "GIT_CONFIG_NOSYSTEM": "1",
    "HOME": "/nonexistent",
}


@pytest.mark.parametrize(
    "extra",
    [
        {".github/workflows/b.yml": WF + "      - run: |\n          CGO_ENABLED=0 GO\\\n          ARCH=riscv64 go build -o node .\n"},
        {"Dockerfile": "FROM golang:1.25\nRUN CGO_ENABLED=0 GO\\\nARCH=riscv64 go build -o /node .\n"},
        {"Makefile": "$(eval GOARCH := riscv64)\nbuild:\n\tGOARCH=$(GOARCH) go build\n"},
        {
            "Makefile": "$(eval include build/release.inc)\nbuild:\n\tGOARCH=$(GOARCH) go build\n",
            "build/release.inc": "GOARCH := riscv64\n",
        },
        {"scripts/build.sh": "#!/bin/sh\nprintf -v GOARCH riscv64\nGOARCH=$GOARCH go build\n"},
        {".github/workflows/b.yml": WF + "      - run: |\n          printf -v GOARCH riscv64\n          GOARCH=$GOARCH go build -o node .\n"},
        {"scripts/build.sh": "#!/bin/sh\nCGO_ENABLED=0 GO\\\nARCH=riscv64 go build\n"},
        {"scripts/build.sh": "#!/bin/sh\nfor TARGETARCH in amd64 riscv64; do GOARCH=$TARGETARCH go build; done\n"},
        {"scripts/build.sh": "#!/bin/bash\nread -r TARGETARCH <<< riscv64\nGOARCH=$TARGETARCH go build\n"},
        {"build/Makefile": "GOARCH := riscv64\nbuild:\n\tgo build\n"},
        {"Makefile.release": "GOARCH := riscv64\n"},
        {"build.sh": "#!/bin/sh\nGOARCH=riscv64 go build\n"},
        {"hack/build": "#!/bin/sh\nGOARCH=riscv64 go build\n"},
        {"scripts/build.bash": "GOARCH=riscv64 go build\n"},
        {"scripts/build": "GOARCH=riscv64 go build\n"},
        {"tools/release": "#!/bin/sh\nGOARCH=riscv64 go build\n"},
        {"docker-compose.yml": "services:\n  node:\n    build: .\n    platform: linux/riscv64\n"},
        {"compose.yaml": "services:\n  node:\n    platform: ${PLATFORM}\n"},
        {"Earthfile": "VERSION 0.8\nbuild:\n  FROM golang:1\n  RUN GOARCH=riscv64 go build\n"},
        {"release.make": "GOARCH ?= riscv64\n"},
        {"scripts/release.sh": "#!/bin/sh\nGOARCH=$TARGETARCH go build\n"},
        {"Makefile": "build:\n\tGOARCH=$(TARGETARCH) go build\nTARGETARCH ?= riscv64\n"},
    ],
    ids=[
        "workflow-run-block-split-ident",
        "dockerfile-split-ident",
        "makefile-eval-goarch",
        "makefile-eval-include",
        "script-printf-v",
        "workflow-printf-v",
        "script-split-ident",
        "script-for-targetarch",
        "script-read-targetarch",
        "subfolder-makefile",
        "makefile-dot-name",
        "root-build-sh",
        "hack-extensionless",
        "scripts-bash",
        "scripts-extensionless",
        "shebang-extensionless",
        "compose-platform",
        "compose-platform-variable",
        "earthfile",
        "dot-make",
        "script-targetarch-passthrough",
        "makefile-targetarch-default",
    ],
)
def test_10q_riscv64_through_v9_holes_and_v9_5_scope_fails_the_gate(tmp_path, extra):
    result = moved_world(tmp_path, "c.go", "//go:build !riscv64\n\n", extra)
    assert result.returncode == 1, result.stdout[-400:] + result.stderr[-400:]


@pytest.mark.parametrize(
    "line",
    [
        "printf -v GOARCH riscv64",
        "printf -vGOARCH riscv64",
        "printf -v 'GOARCH' riscv64",
        "for TARGETARCH in amd64 riscv64; do go build; done",
        "read -r TARGETARCH",
        "export TARGETARCH",
        "local TARGETARCH",
        "declare TARGETARCH",
        "printf -v TARGETARCH riscv64",
    ],
)
def test_10q_bare_bindings_of_goarch_and_targetarch_fail_closed_in_workflows_and_scripts(line):
    module = load_inventory()
    with pytest.raises(module.ShippedArchesUnreadable):
        module.workflow_linux_arches(WF + f"      - run: {line}\n")
    with pytest.raises(module.ShippedArchesUnreadable):
        module.unread_source_linux_arches(f"#!/bin/sh\n{line}\n")


@pytest.mark.parametrize(
    "text",
    [
        "$(eval GOARCH := riscv64)\n",
        "${eval GOARCH := riscv64}\n",
        "$( eval X := 1)\n",
        "X := $(eval include a.inc)\n",
        "include a.mk\n",
        "-include a.mk\n",
        "sinclude a.mk\n",
        "build:\n\tinclude_tool\n",
        "X := a\\\n$(eval Y := 1)\n",
    ],
)
def test_10q_root_makefile_refuses_any_eval_or_include(text):
    module = load_inventory()
    with pytest.raises(module.ShippedArchesUnreadable):
        module.makefile_linux_arches(text)


def test_10q_backslash_newline_joins_without_a_space_in_workflow_and_build_lines():
    module = load_inventory()
    assert module.build_file_linux_arches("FROM golang:1\nRUN GO\\\nARCH=riscv64 go build\n") == {"riscv64"}
    assert module.build_file_linux_arches("FROM golang:1\nRUN go build \\\n  -o /x .\n") == set()


@pytest.mark.parametrize(
    ("path", "text"),
    [
        (
            "scripts/fixture.sh",
            "body=$(cat <<'TAIL'\nARG TARGETARCH\nRUN CGO_ENABLED=0 GOARCH=${TARGETARCH} go build \\\n    -o /x .\nTAIL\n)\n",
        ),
        ("docker/Dockerfile", "FROM golang:1\nARG TARGETARCH\nRUN GOARCH=$TARGETARCH go build\n"),
        (".specify/scripts/bash/common.sh", "printf -v code '%d' \"'$char\"\n"),
        ("build/Makefile", "VERSION ?= 1\n"),
        ("docker-compose.yml", "services:\n  node:\n    build: .\n"),
        ("scripts/build", "go vet ./...\n"),
    ],
    ids=["quoted-heredoc-captured-by-cat-into-a-variable", "subfolder-dockerfile", "printf-v-other", "makefile-no-arch", "compose-no-platform", "script-no-arch"],
)
def test_10q_sources_that_set_no_arch_are_not_refused(path, text):
    reader = load_inventory().shipped_linux_arch_source(path)
    assert reader is not None
    assert reader(text) == set()


@pytest.mark.parametrize(
    "text",
    [
        "body=$(cat <<TAIL\nRUN GOARCH=${TARGETARCH} go build\nTAIL\n)\n",
        "GOARCH=${TARGETARCH} go build\n",
        "cat <<'X'\nRUN GOARCH=riscv64 go build\nX\n",
    ],
    ids=["unquoted-heredoc", "plain-script", "quoted-heredoc-literal-arch"],
)
def test_10q_targetarch_passthrough_only_inside_dockerfile_text(text):
    module = load_inventory()
    with pytest.raises(module.ShippedArchesUnreadable):
        module.unread_source_linux_arches(text)


@pytest.mark.parametrize("ref", ["86da26e5", "2d8e11f3", "0544e60b", "origin/main"])
def test_10q_real_node_still_reads_exactly_the_shipped_set(monkeypatch, ref):
    if not (NODE_CLONE / ".git").exists():
        pytest.skip(f"node clone not present at {NODE_CLONE}")
    module = load_inventory()
    module.shipped_linux_arches.cache_clear()
    monkeypatch.chdir(NODE_CLONE)
    assert module.shipped_linux_arches(ref) == module.GO_SHIPPED_ARCHES


def git(repo, *args):
    return subprocess.run(["git", *args], cwd=repo, env=ENV, capture_output=True, text=True, check=True).stdout.strip()


def commit(repo, message, files):
    for rel, text in files.items():
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text(text)
    git(repo, "add", *files)
    git(repo, "commit", "-q", "-m", message)
    return git(repo, "rev-parse", "HEAD")


BASE = "import os\nimport sys\nfrom app.models import Admin\nfrom app.stats import old_stats\nfrom app.users import User\nimport json\nimport re\n"
PORTED = BASE.replace("from app.models import Admin\n", "from app.models import Admin\nfrom app.notify import notify\n")


def context_world(tmp_path, later):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "fork")
    git(repo, "config", "commit.gpgsign", "false")
    base = commit(repo, "base", {"admin.py": BASE})
    git(repo, "checkout", "-q", "-b", "up")
    upstream = commit(repo, "upstream notify", {"admin.py": PORTED})
    git(repo, "checkout", "-q", "fork")
    fork = commit(repo, "fork carries notify", {"admin.py": PORTED})
    head = commit(repo, "later port", {"admin.py": later}) if later is not None else fork
    return repo, base, upstream, fork, head


@pytest.mark.parametrize(
    "later",
    [
        PORTED.replace("old_stats", "new_stats"),
        PORTED.replace("from app.models import Admin\n", "from app.models import Admin, Role\n"),
        PORTED.replace("from app.notify import notify\n", "from app.notify import notify\nfrom app.audit import audit\n").replace(
            "old_stats", "new_stats"
        ),
        PORTED.replace("import sys\n", "").replace("old_stats", "new_stats"),
        None,
    ],
    ids=["after-context-changed", "before-context-changed", "context-changed-and-line-added", "context-removed", "unchanged"],
)
def test_10q_design_fork_commit_survives_a_later_port_changing_its_hunk_context(tmp_path, monkeypatch, later):
    repo, _, upstream, fork, head = context_world(tmp_path, later)
    module = load_inventory()
    monkeypatch.chdir(repo)
    assert module.design_fork_commit(f"carried by {fork[:12]}", head, upstream, frozenset()) == fork


@pytest.mark.parametrize(
    "later",
    [
        PORTED.replace("from app.notify import notify\n", "").replace("old_stats", "new_stats"),
        PORTED.replace("from app.notify import notify\n", "from app.notify import notify_v2\n").replace("old_stats", "new_stats"),
        PORTED.replace("from app.notify import notify\n", "").replace("import re\n", "import re\nfrom app.notify import notify\n"),
        BASE.replace("old_stats", "new_stats"),
    ],
    ids=["own-line-removed", "own-line-changed", "own-line-moved-away", "whole-port-reverted"],
)
def test_10q_design_fork_commit_still_requires_every_own_added_line(tmp_path, monkeypatch, later):
    repo, _, upstream, fork, head = context_world(tmp_path, later)
    module = load_inventory()
    monkeypatch.chdir(repo)
    assert module.design_fork_commit(f"carried by {fork[:12]}", head, upstream, frozenset()) is None
