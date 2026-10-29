import ast
from pathlib import Path

APP = Path(__file__).resolve().parent.parent / "app"
GUARDED = {
    ("models/host.py", "validate_format_variables"),
    ("models/settings.py", "validate_value_format"),
    ("subscription/share.py", "_format_dynamic_value"),
}


def _format_calls():
    for path in sorted(APP.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for function in ast.walk(tree):
            if not isinstance(function, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            for node in ast.walk(function):
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr in ("format_map", "format")
                    and not isinstance(node.func.value, ast.Constant)
                ):
                    yield path.relative_to(APP).as_posix(), function, node


def test_every_template_format_call_is_one_of_the_guarded_ones():
    unguarded = sorted(
        {
            (path, function.name, node.lineno)
            for path, function, node in _format_calls()
            if node.func.attr == "format_map" and (path, function.name) not in GUARDED
        }
    )
    assert unguarded == []


def test_each_guarded_call_checks_the_format_spec_before_it_formats():
    found = set()
    for path, function, node in _format_calls():
        if (path, function.name) not in GUARDED or node.func.attr != "format_map":
            continue
        checks = [
            call.lineno
            for call in ast.walk(function)
            if isinstance(call, ast.Call)
            and isinstance(call.func, ast.Name)
            and call.func.id == "has_oversized_format_spec"
        ]
        assert checks and min(checks) < node.lineno, (path, function.name)
        found.add((path, function.name))
    assert found == GUARDED
