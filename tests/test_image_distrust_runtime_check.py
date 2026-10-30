import ast
import sys

from tests.emsign_support import REPOSITORY, SCRIPTS, load


def test_the_dockerfile_runs_the_expression_the_runtime_check_tests():
    tree = ast.parse((SCRIPTS / "image_distrust_runtime_check.py").read_text(encoding="utf-8"))
    expression = next(
        node.value.value
        for node in tree.body
        if isinstance(node, ast.Assign)
        and any(getattr(target, "id", None) == "DOCKERFILE_DESELECT_EXPRESSION" for target in node.targets)
    )
    dockerfile = (REPOSITORY / "Dockerfile").read_text(encoding="utf-8")

    assert f"sed -i -E '{expression}' /etc/ca-certificates.conf" in dockerfile
    assert "PG_IMAGE_BUILD=1 python -B /code/scripts/distrust_certifi_roots.py --image" in dockerfile
    assert "python -B /code/scripts/image_trust_store_check.py" in dockerfile


def test_the_runtime_check_refuses_to_run_outside_a_marked_container(monkeypatch):
    monkeypatch.delenv("PG_THROWAWAY_CONTAINER", raising=False)
    monkeypatch.setattr(sys, "argv", ["image_distrust_runtime_check.py"])

    assert load("image_distrust_runtime_check").main() == 2
