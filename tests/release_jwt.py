import ast
import hashlib
import importlib.machinery
import importlib.util
from pathlib import Path

RELEASE_COMMIT = "334a4076d247146b954d33c3aa18227e0df55973"
RELEASE_JWT = Path(__file__).resolve().parent / "fixtures" / "v5_34_25_app_utils_jwt.py.txt"
RELEASE_JWT_SHA256 = "354c5125589c1204328d1153e177d7ad1f77966c3be51aaccf82b96a2859323e"
TOKEN_CONTRACT_FUNCTIONS = (
    "revocation_epoch",
    "next_revocation_instant",
    "_stored_sub_revoked_at",
    "create_subscription_token",
    "_parse_subscription_data",
    "_decode_b64_token",
    "get_subscription_payload",
)
TOKEN_CONTRACT_SHA256 = "706b4b2d1857d784c2ffaad3f200cc0302c9ce58dafef152ee131e0d3d092812"


def token_contract_sha256(source: str) -> str:
    functions = {
        node.name: ast.get_source_segment(source, node)
        for node in ast.parse(source).body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    return hashlib.sha256("\n\n".join(functions[name] for name in TOKEN_CONTRACT_FUNCTIONS).encode()).hexdigest()


def load_release_jwt(name: str):
    source = RELEASE_JWT.read_bytes()
    if hashlib.sha256(source).hexdigest() != RELEASE_JWT_SHA256:
        raise AssertionError(f"{RELEASE_JWT.name} is not the vendored release minter")
    loader = importlib.machinery.SourceFileLoader(name, str(RELEASE_JWT))
    spec = importlib.util.spec_from_loader(name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module
