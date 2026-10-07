import ast
from pathlib import Path

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory

ROLLBACK_FLOOR = "e5a9c3f1b7d2"


def _rollback_reachable_scripts():
    script = ScriptDirectory.from_config(Config("alembic.ini"))
    heads = script.get_heads()
    reachable = []
    for revision in script.iterate_revisions(heads, ROLLBACK_FLOOR):
        if revision.revision == ROLLBACK_FLOOR:
            continue
        reachable.append(revision)
    return {revision.revision: Path(revision.module.__file__) for revision in reachable}


SCRIPTS = _rollback_reachable_scripts()


def _str(node):
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def _columns(node):
    if isinstance(node, (ast.List, ast.Tuple)):
        return [c.value for c in node.elts if isinstance(c, ast.Constant)]
    return []


def _op_calls(func, name):
    found = []
    for call in ast.walk(func):
        if not isinstance(call, ast.Call) or not isinstance(call.func, ast.Attribute):
            continue
        if call.func.attr != name:
            continue
        if isinstance(call.func.value, ast.Name) and call.func.value.id == "op":
            found.append(call)
    return found


def _functions(tree):
    return {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}


def _fk_local_columns(create_table_call):
    columns = []
    for arg in create_table_call.args:
        if (
            isinstance(arg, ast.Call)
            and isinstance(arg.func, ast.Attribute)
            and arg.func.attr == "ForeignKeyConstraint"
            and arg.args
        ):
            columns.append(_columns(arg.args[0]))
    return columns


def _offending(path):
    tree = ast.parse(path.read_text())
    functions = _functions(tree)
    upgrade, downgrade = functions.get("upgrade"), functions.get("downgrade")
    if upgrade is None or downgrade is None:
        return []
    fk_columns_by_table = {}
    index_columns_by_name = {}
    for call in _op_calls(upgrade, "create_table"):
        table = _str(call.args[0]) if call.args else None
        if table is not None:
            fk_columns_by_table[table] = _fk_local_columns(call)
    for call in _op_calls(upgrade, "create_index"):
        if len(call.args) >= 3:
            index_columns_by_name[_str(call.args[0])] = (_str(call.args[1]), _columns(call.args[2]))
    offending = []
    for call in _op_calls(downgrade, "drop_index"):
        index_name = _str(call.args[0]) if call.args else None
        table, index_columns = index_columns_by_name.get(index_name, (None, []))
        for fk_columns in fk_columns_by_table.get(table, []):
            if fk_columns and index_columns[: len(fk_columns)] == fk_columns:
                offending.append((path.name, index_name))
    return offending


@pytest.mark.parametrize("revision", sorted(SCRIPTS))
def test_rollback_reachable_downgrades_never_drop_a_foreign_key_backing_index_before_the_table(revision):
    offending = _offending(SCRIPTS[revision])
    assert offending == [], (
        f"migration {revision} is downgraded by app.fork.db_rollback and drops an index that backs a "
        "foreign key before dropping its table; MySQL/InnoDB refuses this with error 1553 and leaves the "
        "rollback partway. Let op.drop_table remove the index and the constraint instead: " + str(offending)
    )
