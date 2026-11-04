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
)
TOKEN_CONTRACT_GLOBALS: tuple[str, ...] = ()
ROLLBACK_IMAGE_TOKEN_CONTRACT_SHA256 = "e098552f8130e26465c2ca21256ec634dc331a1e998e012e5e09d0abf17a1ee7"


def token_contract_sha256(source: str) -> str:
    functions = {
        node.name: ast.get_source_segment(source, node)
        for node in ast.parse(source).body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    return hashlib.sha256("\n\n".join(functions[name] for name in TOKEN_CONTRACT_FUNCTIONS).encode()).hexdigest()


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
