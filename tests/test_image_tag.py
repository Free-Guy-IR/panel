import importlib.util
import re
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "image_tag", Path(__file__).resolve().parents[1] / "scripts" / "image_tag.py"
)
_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_module)
image_tag = _module.image_tag

DOCKER_TAG = re.compile(r"^[a-zA-Z0-9_][a-zA-Z0-9._-]{0,127}$")

REFS = [
    "main-dev",
    "fork/boundary-extraction",
    "fork-boundary-extraction",
    "feature/x",
    "feature-x",
    "v1.2+build",
    ".hidden",
    "-leading-dash",
    "///",
    "Mixed/Case",
    "a" * 300,
    "sub/dir/deep/branch",
]


def test_every_ref_yields_a_valid_docker_tag():
    for ref in REFS:
        tag = image_tag(ref)
        assert DOCKER_TAG.match(tag), (ref, tag)
        assert len(tag) <= 128, (ref, len(tag))


def test_distinct_refs_never_share_a_tag():
    tags = {}
    for ref in REFS:
        tag = image_tag(ref)
        assert tag not in tags, (ref, tags.get(tag), tag)
        tags[tag] = ref


def test_a_branch_named_like_another_refs_tag_does_not_collide():
    derived = image_tag("feature/x")
    assert image_tag(derived) != derived


def test_slash_and_plus_are_removed():
    assert "/" not in image_tag("fork/boundary-extraction")
    assert "+" not in image_tag("v1.2+build")


def test_is_deterministic():
    assert image_tag("fork/boundary-extraction") == image_tag("fork/boundary-extraction")
