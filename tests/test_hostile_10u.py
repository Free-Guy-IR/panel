import pytest

from tests.test_hostile_10m import load_inventory
from tests.test_hostile_10q import commit, git

ELSEWHERE_BASE = (
    "import os\n\n\ndef first(admin):\n    load(admin)\n    return admin\n\n\n"
    "def middle(z):\n    z1 = 1\n    z2 = 2\n    z3 = 3\n    return z\n\n\n"
    "def second(admin):\n    require_admin(admin)\n    save(admin)\n    return admin\n"
)
ELSEWHERE_PICK = ELSEWHERE_BASE.replace("    load(admin)\n", "    require_admin(admin)\n    load(admin)\n")
ELSEWHERE_LATER = ELSEWHERE_PICK.replace("    require_admin(admin)\n    save(admin)\n", "    save(admin)\n")
ELSEWHERE_HEAD = ELSEWHERE_LATER.replace("    require_admin(admin)\n    load(admin)\n", "    load(admin)\n")

TWIN = "def handler(admin):\n    require_admin(admin)\n    load(admin)\n    return admin\n"
TWIN_BARE = TWIN.replace("    require_admin(admin)\n", "")
TWIN_BASE = f"import os\n\n\n{TWIN_BARE}\n\n{TWIN}"
TWIN_PICK = f"import os\n\n\n{TWIN}\n\n{TWIN}"
TWIN_LATER = f"import os\n\n\n{TWIN}\n\n{TWIN_BARE}"
TWIN_HEAD = f"import os\n\n\n{TWIN_BARE}\n\n{TWIN_BARE}"

PARTIAL_BASE = (
    "import os\n\n\ndef guard(x):\n    return x\n\n\n"
    "def keep(a):\n    a1 = 1\n    a2 = 2\n    a3 = 3\n    return a\n\n\n"
    "def count(q):\n    return q\n"
)
PARTIAL_PICK = PARTIAL_BASE.replace("    return x\n", "    validate(x)\n    return x\n")
PARTIAL_LATER = PARTIAL_PICK.replace("    validate(x)\n", "    validate_strict(x)\n").replace(
    "    return q\n", "    return single_pass(q)\n"
)
PARTIAL_PORTED = PARTIAL_PICK.replace("    return q\n", "    return single_pass(q)\n")
PARTIAL_HEAD = PARTIAL_PORTED.replace("    validate(x)\n", "")

SPLIT_FIRST = "def first(admin):\n    load(admin)\n    return admin\n"
SPLIT_SECOND = "def second(admin):\n    save(admin)\n    return admin\n"
SPLIT_MIDDLE = "def middle(z):\n    z1 = 1\n    z2 = 2\n    z3 = 3\n    return z\n"


def split_guarded(block):
    return block.replace("(admin):\n", "(admin):\n    require_admin(admin)\n", 1)


def split_doc(first, second):
    return f"import os\n\n\n{first}\n\n{SPLIT_MIDDLE}\n\n{second}"


SPLIT_BASE = split_doc(SPLIT_FIRST, split_guarded(SPLIT_SECOND))
SPLIT_PICK = split_doc(split_guarded(SPLIT_FIRST), split_guarded(SPLIT_SECOND))
SPLIT_LATER = split_doc(SPLIT_FIRST, SPLIT_SECOND)
SPLIT_PORTED = split_doc(split_guarded(SPLIT_FIRST), SPLIT_SECOND)

CLASS_A = "class A:\n    def handler(self, admin):\n        load(admin)\n        return admin\n"
CLASS_B = CLASS_A.replace("class A:", "class B:")
CLASS_C = CLASS_A.replace("class A:", "class C:")
CLASS_D = CLASS_A.replace("class A:", "class D:")
SPLIT_MIDDLE_TWO = SPLIT_MIDDLE.replace("middle", "middle2")


def class_guarded(block):
    return block.replace("(self, admin):\n", "(self, admin):\n        require_admin(admin)\n", 1)


def twin_doc(a, b, c, d):
    return split_doc(f"{a}\n\n{SPLIT_MIDDLE}\n\n{c}", f"{b}\n\n{SPLIT_MIDDLE_TWO}\n\n{d}")


CLASS_BASE = split_doc(CLASS_A, class_guarded(CLASS_B))
CLASS_PICK = split_doc(class_guarded(CLASS_A), class_guarded(CLASS_B))
CLASS_LATER = split_doc(CLASS_A, CLASS_B)
CLASS_PORTED = split_doc(class_guarded(CLASS_A), CLASS_B)

TWIN_SPLIT_BASE = twin_doc(CLASS_A, class_guarded(CLASS_B), CLASS_C, class_guarded(CLASS_D))
TWIN_SPLIT_PICK = twin_doc(class_guarded(CLASS_A), class_guarded(CLASS_B), CLASS_C, class_guarded(CLASS_D))
TWIN_SPLIT_LATER = twin_doc(CLASS_A, CLASS_B, CLASS_C, CLASS_D)
TWIN_SPLIT_PORTED = twin_doc(class_guarded(CLASS_A), CLASS_B, CLASS_C, CLASS_D)

WORLDS = {
    "removal-elsewhere": (ELSEWHERE_BASE, ELSEWHERE_PICK, ELSEWHERE_LATER, ELSEWHERE_LATER, ELSEWHERE_HEAD),
    "twin-copy": (TWIN_BASE, TWIN_PICK, TWIN_LATER, TWIN_LATER, TWIN_HEAD),
    "partial-port": (PARTIAL_BASE, PARTIAL_PICK, PARTIAL_LATER, PARTIAL_PORTED, PARTIAL_HEAD),
    "honest-port": (PARTIAL_BASE, PARTIAL_PICK, PARTIAL_LATER, PARTIAL_LATER, PARTIAL_LATER),
    "split-later-far-hunk-only": (SPLIT_BASE, SPLIT_PICK, SPLIT_LATER, SPLIT_PORTED, SPLIT_LATER),
    "split-honest": (SPLIT_BASE, SPLIT_PICK, SPLIT_LATER, SPLIT_LATER, SPLIT_LATER),
    "same-nearest-context": (CLASS_BASE, CLASS_PICK, CLASS_LATER, CLASS_PORTED, CLASS_LATER),
    "twin-split-later": (TWIN_SPLIT_BASE, TWIN_SPLIT_PICK, TWIN_SPLIT_LATER, TWIN_SPLIT_PORTED, TWIN_SPLIT_LATER),
    "same-nearest-context-honest": (CLASS_BASE, CLASS_PICK, CLASS_LATER, CLASS_LATER, CLASS_LATER),
}


def positional_world(tmp_path, name):
    base, pick_text, later_text, ported_text, head_text = WORLDS[name]
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "fork")
    git(repo, "config", "commit.gpgsign", "false")
    commit(repo, "base", {"a.py": base})
    git(repo, "checkout", "-q", "-b", "up")
    superseded = commit(repo, "security: require admin", {"a.py": pick_text})
    later = commit(repo, "refactor: tidy checks", {"a.py": later_text})
    git(repo, "update-ref", "refs/remotes/upstream/main", "up")
    git(repo, "checkout", "-q", "fork")
    pick = commit(repo, f"security: require admin\n\n(cherry picked from commit {superseded})", {"a.py": pick_text})
    head = commit(repo, f"refactor: tidy checks\n\n(cherry picked from commit {later})", {"a.py": ported_text})
    if head_text != ported_text:
        head = commit(repo, "chore: simplify", {"a.py": head_text})
    return repo, superseded, pick, head


def run_row(tmp_path, monkeypatch, name):
    repo, superseded, pick, head = positional_world(tmp_path, name)
    module = load_inventory()
    monkeypatch.chdir(repo)
    reason = f"Ported by its cherry-pick -x {pick[:12]}. security: yes"
    return module.security_design_problems(reason, head, superseded, {})


@pytest.mark.parametrize(
    "name",
    [
        "removal-elsewhere",
        "twin-copy",
        "partial-port",
        "split-later-far-hunk-only",
        "same-nearest-context",
        "twin-split-later",
    ],
)
def test_10u_unported_reversal_is_refused(tmp_path, monkeypatch, name):
    problems = run_row(tmp_path, monkeypatch, name)
    assert problems is not None
    assert any("a.py:+" in problem for problem in problems)


@pytest.mark.parametrize("name", ["honest-port", "split-honest", "same-nearest-context-honest"])
def test_10u_brought_later_hunk_at_the_spot_is_accepted(tmp_path, monkeypatch, name):
    assert run_row(tmp_path, monkeypatch, name) == []


@pytest.mark.parametrize(
    "text",
    [
        "export GOARCH := riscv64 && for GOARCH in amd64 s390x; do go build; done\n",
        "export GOARCH := $(shell uname -m)\n",
        "override GOARCH := ${ARCH}\n",
    ],
    ids=["make-then-loop", "make-shell", "make-variable"],
)
def test_10u_make_assignment_with_unreadable_value_fails_closed(text):
    module = load_inventory()
    with pytest.raises(module.ShippedArchesUnreadable):
        module.build_file_linux_arches(text)


def test_10u_make_export_assignment_is_read():
    assert load_inventory().build_file_linux_arches("export GOARCH := s390x\n") == {"s390x"}
