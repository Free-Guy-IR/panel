import ast
import hashlib
from pathlib import Path

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
ROLLBACK_IMAGE_TOKEN_CONTRACT_SHA256 = "e098552f8130e26465c2ca21256ec634dc331a1e998e012e5e09d0abf17a1ee7"


def test_the_rollback_image_was_proven_against_the_token_code_this_release_ships():
    source = JWT.read_text(encoding="utf-8")
    functions = {
        node.name: ast.get_source_segment(source, node)
        for node in ast.parse(source).body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    digest = hashlib.sha256("\n\n".join(functions[name] for name in TOKEN_CONTRACT_FUNCTIONS).encode()).hexdigest()
    assert digest == ROLLBACK_IMAGE_TOKEN_CONTRACT_SHA256, (
        "a subscription token function in app/utils/jwt.py changed; vendor this file into "
        "hotfix/v5-prod-accept-v4 as tests/fixtures/v5_34_25_app_utils_jwt.py.txt, re-run that branch's "
        "token tests, rebuild the rollback image, then update both pins"
    )
