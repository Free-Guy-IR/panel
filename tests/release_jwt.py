import ast
import hashlib
import importlib.machinery
import importlib.util
from pathlib import Path

RELEASE_COMMIT = "833c8ca47ae5651db4cf5bb316382348b183d9ce"
RELEASE_JWT = Path(__file__).resolve().parent / "fixtures" / "v5_34_25_app_utils_jwt.py.txt"
RELEASE_JWT_SHA256 = "872298fffc630fec66d212d735e172f00055b1fc240ff8131d1988b3a08ca1ee"
TOKEN_CONTRACT_FUNCTIONS = (
    "revocation_epoch",
    "next_revocation_instant",
    "_stored_sub_revoked_at",
    "create_subscription_token",
    "_parse_subscription_data",
    "_decode_b64_token",
    "get_subscription_payload",
)
TOKEN_CONTRACT_SHA256 = "21111c668beba9535c654a049fb63024ce82572925e61b8a85fefa14d261379f"


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
