import ast
import hashlib
from pathlib import Path

import pytest

JWT = Path(__file__).resolve().parents[1] / "app" / "utils" / "jwt.py"
TOKEN_CONTRACT_FUNCTIONS = (
    "revocation_epoch",
    "next_revocation_instant",
    "_stored_sub_revoked_at",
    "create_subscription_token",
    "_parse_subscription_data",
    "_decode_b64_token",
    "_issued_at",
    "get_subscription_payload",
    "get_secret_key",
)
TOKEN_CONTRACT_GLOBALS = ("_UNIX_EPOCH", "_READ_FROM_DATABASE")
ROLLBACK_IMAGE_TOKEN_CONTRACT_SHA256 = "e07a78977a62374d05cc35a32e0890aa92f7c6d8ab6cd4ba112c22328140743e"


def token_contract_sha256(source: str) -> str:
    tree = ast.parse(source)
    lines = source.splitlines()

    def span(node: ast.stmt) -> str:
        first = min([node.lineno, *(decorator.lineno for decorator in getattr(node, "decorator_list", ()))])
        return "\n".join(lines[first - 1 : node.end_lineno])

    imports = [span(node) for node in tree.body if isinstance(node, (ast.Import, ast.ImportFrom))]
    assignments = {}
    for node in tree.body:
        targets = (
            node.targets if isinstance(node, ast.Assign) else [node.target] if isinstance(node, ast.AnnAssign) else []
        )
        for target in targets:
            if isinstance(target, ast.Name):
                assignments[target.id] = span(node)
    functions = {
        node.name: span(node) for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    parts = [
        *imports,
        *(assignments[name] for name in TOKEN_CONTRACT_GLOBALS),
        *(functions[name] for name in TOKEN_CONTRACT_FUNCTIONS),
    ]
    return hashlib.sha256("\n\n".join(parts).encode()).hexdigest()


def _module_level_definitions(tree: ast.Module) -> dict[str, ast.stmt]:
    definitions = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            definitions[node.name] = node
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    definitions[target.id] = node
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            definitions[node.target.id] = node
    return definitions


def test_the_rollback_image_was_proven_against_the_token_code_this_release_ships():
    digest = token_contract_sha256(JWT.read_text(encoding="utf-8"))
    assert digest == ROLLBACK_IMAGE_TOKEN_CONTRACT_SHA256, (
        "a subscription token function in app/utils/jwt.py changed; vendor this file into "
        "hotfix/v5-prod-accept-v4 as tests/fixtures/v5_34_25_app_utils_jwt.py.txt, re-run that branch's "
        "token tests, rebuild the rollback image, then update both pins"
    )


@pytest.mark.parametrize(
    ("original", "mutated"),
    [
        ("_UNIX_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)", "_UNIX_EPOCH = datetime(1971, 1, 1, tzinfo=UTC)"),
        ("_READ_FROM_DATABASE = object()", "_READ_FROM_DATABASE = None"),
        ("@cached(ttl=60)\nasync def get_secret_key", "@cached(ttl=86400)\nasync def get_secret_key"),
        ("        key = await get_jwt_secret_key(db=db)\n", "        key = await get_jwt_secret_key(db=db) or 'x'\n"),
        ("from math import ceil, isfinite", "from numpy import ceil, isfinite"),
        ("from app.db.crud.general import get_jwt_secret_key", "from app.db.crud.admin import get_jwt_secret_key"),
    ],
    ids=["unix-epoch", "read-from-database", "decorator", "get-secret-key", "import-math", "import-secret"],
)
def test_the_pin_moves_when_code_the_token_functions_read_changes(original, mutated):
    source = JWT.read_text(encoding="utf-8")
    assert source.count(original) == 1
    assert token_contract_sha256(source.replace(original, mutated)) != token_contract_sha256(source)


def test_every_module_level_name_the_token_functions_read_is_pinned():
    tree = ast.parse(JWT.read_text(encoding="utf-8"))
    definitions = _module_level_definitions(tree)
    reached = set()
    pending = list(TOKEN_CONTRACT_FUNCTIONS)
    while pending:
        name = pending.pop()
        if name in reached:
            continue
        reached.add(name)
        for node in ast.walk(definitions[name]):
            if isinstance(node, ast.Name) and node.id in definitions and node.id not in reached:
                pending.append(node.id)
    pinned = set(TOKEN_CONTRACT_FUNCTIONS) | set(TOKEN_CONTRACT_GLOBALS)
    assert reached <= pinned, sorted(reached - pinned)


@pytest.mark.parametrize(
    "appended",
    [
        "get_secret_key = cached(ttl=86400)(get_secret_key.__wrapped__)\n",
        "_issued_at = lambda payload: 0\n",
        "class _issued_at:\n    pass\n",
        "def create_subscription_token(*args, **kwargs):\n    return ''\n",
        "(_READ_FROM_DATABASE, _x) = (None, None)\n",
    ],
    ids=["rebind-decorated", "rebind-lambda", "rebind-class", "rebind-def", "rebind-tuple"],
)
def test_the_pin_moves_when_a_pinned_name_is_rebound_later_at_top_level(appended):
    source = JWT.read_text(encoding="utf-8")
    assert token_contract_sha256(source + "\n" + appended) != token_contract_sha256(source)


@pytest.mark.parametrize(
    "appended",
    [
        "_UNIX_EPOCH += timedelta(days=1)\n",
        "if True:\n    _UNIX_EPOCH = datetime(1971, 1, 1, tzinfo=UTC)\n",
        "try:\n    get_secret_key = None\nexcept Exception:\n    pass\n",
        "globals().update(_READ_FROM_DATABASE=None)\n",
        "@globals().update\ndef _unpinned_helper():\n    pass\n",
    ],
    ids=["augassign", "if", "try", "bare-expression", "unpinned-decorator"],
)
def test_the_pin_moves_when_any_other_top_level_statement_is_added(appended):
    source = JWT.read_text(encoding="utf-8")
    assert token_contract_sha256(source + "\n" + appended) != token_contract_sha256(source)
