import ast
from pathlib import Path

APP = Path(__file__).resolve().parent.parent / "app"
GUARDED = {
    ("fork/format_template.py", "check_template_value"),
    ("fork/format_template.py", "format_bounded"),
    ("models/host.py", "validate_format_variables"),
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
                    yield path.relative_to(APP).as_posix(), function, node, node.func.attr
                elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "format":
                    yield path.relative_to(APP).as_posix(), function, node, "format_map"


def test_every_template_format_call_is_one_of_the_guarded_ones():
    unguarded = sorted(
        {
            (path, function.name, node.lineno)
            for path, function, node, kind in _format_calls()
            if kind == "format_map" and (path, function.name) not in GUARDED and not path.startswith("db/migrations/")
        }
    )
    assert unguarded == []


def test_each_guarded_call_checks_the_format_spec_before_it_formats():
    found = set()
    for path, function, node, kind in _format_calls():
        if (path, function.name) not in GUARDED or kind != "format_map":
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


def test_every_dynamic_value_is_formatted_through_the_bounded_formatter():
    share = ast.parse((APP / "subscription/share.py").read_text(encoding="utf-8"))
    function = next(
        node for node in ast.walk(share) if isinstance(node, ast.FunctionDef) and node.name == "_format_dynamic_value"
    )
    called = {
        node.func.id for node in ast.walk(function) if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "format_bounded" in called
