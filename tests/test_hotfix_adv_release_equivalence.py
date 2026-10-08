import ast
import asyncio
import hashlib
import hmac
import os
import random
import subprocess
import time
from base64 import b64encode
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path

import jwt as pyjwt
import pytest
from fastapi import HTTPException

import app.operation as operation_module
from app.operation import BaseOperation, OperatorType
from app.utils import jwt as jwt_utils
from app.utils.helpers import ensure_datetime_timezone
from tests.release_jwt import load_release_jwt

ROOT = Path(__file__).resolve().parents[1]
SECRET = "a-secret-that-only-the-server-knows"
PREDICATE_COMMIT = "8b0387911609f6a583a703fe59f610b3eb14240c"
PREDICATE_FILE = ROOT / "tests" / "fixtures" / "v5_34_25_get_validated_sub_predicate.py.txt"
PREDICATE_SHA256 = "e6dd43740a1a1d00dcb7bf690fc728d8b2488c7a74139e254345bd9d5aad7c88"
B64_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"


def _release_predicate_source() -> str:
    source = PREDICATE_FILE.read_bytes()
    assert hashlib.sha256(source).hexdigest() == PREDICATE_SHA256
    return source.decode("utf-8")


RELEASE_PREDICATE = compile("(" + _release_predicate_source() + ")", str(PREDICATE_FILE), "eval")


def _predicate_of(source: str) -> str:
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "get_validated_sub":
            return ast.unparse([st for st in node.body if isinstance(st, ast.If)][-1].test)
    raise AssertionError("get_validated_sub not found")


class _User:
    def __init__(self, user_id, created_at, sub_revoked_at=None):
        self.id = user_id
        self.username = f"user{user_id}"
        self.created_at = created_at
        self.sub_revoked_at = sub_revoked_at


@pytest.fixture
def secret(monkeypatch):
    async def _secret():
        return SECRET

    monkeypatch.setattr(jwt_utils, "get_secret_key", _secret)


@pytest.fixture
def release(monkeypatch):
    module = load_release_jwt("release_jwt_equivalence")

    async def _secret():
        return SECRET

    monkeypatch.setattr(module, "get_secret_key", _secret)
    return module


@pytest.fixture
def store(monkeypatch):
    users = {}

    async def _get_user_by_id(_db, user_id, **_load_kwargs):
        return users.get(user_id)

    async def _get_user(_db, username, **_load_kwargs):
        return next((u for u in users.values() if u.username == username), None)

    monkeypatch.setattr(operation_module, "get_user_by_id", _get_user_by_id)
    monkeypatch.setattr(operation_module, "get_user", _get_user)
    return users


@pytest.fixture(params=["Asia/Tehran", "America/Los_Angeles", "UTC"])
def process_tz(request):
    saved = os.environ.get("TZ")
    os.environ["TZ"] = request.param
    time.tzset()
    yield request.param
    if saved is None:
        os.environ.pop("TZ", None)
    else:
        os.environ["TZ"] = saved
    time.tzset()


def _body(payload: str) -> str:
    return b64encode(payload.encode("utf-8"), altchars=b"-_").decode("utf-8").rstrip("=")


def _hmac_token(payload: str, secret: str = SECRET) -> str:
    body = _body(payload)
    signature = b64encode(hmac.new(secret.encode("utf-8"), body.encode("utf-8"), sha256).digest(), altchars=b"-_")
    return body + "." + signature.decode("utf-8").rstrip("=")


def _legacy_token(payload: str, hex_signature: bool, secret: str = SECRET) -> str:
    body = _body(payload)
    digest = sha256((body + secret).encode("utf-8")).digest()
    signature = digest.hex()[:10] if hex_signature else b64encode(digest, altchars=b"-_").decode("utf-8")[:10]
    return body + signature


def _tamper(token: str, payload: str) -> str:
    return _body(payload) + "." + token.rpartition(".")[2]


def _fuzz(tokens, count, seed):
    rng = random.Random(seed)
    out = []
    for _ in range(count):
        token = rng.choice(tokens)
        op = rng.randrange(7)
        i = rng.randrange(len(token))
        if op == 0:
            token = token[:i] + rng.choice(B64_ALPHABET) + token[i + 1 :]
        elif op == 1:
            token = token[:i] + token[i + 1 :]
        elif op == 2:
            token = token[:i] + rng.choice(B64_ALPHABET + ".,=é١ ") + token[i:]
        elif op == 3:
            token = token[: max(i, 1)]
        elif op == 4:
            token = token + "." + token
        elif op == 5:
            head, _, tail = token.rpartition(".")
            token = tail + "." + head if head else token[::-1]
        else:
            token = token[:i] + "é" * rng.randrange(1, 11) + token[i:]
        out.append(token)
    return out


def _release_decision(release, token, user):
    try:
        sub = asyncio.run(release.get_subscription_payload(token))
    except Exception:
        return "error"
    db_user = None
    if sub and user is not None:
        if sub.get("user_id"):
            db_user = user if user.id == sub["user_id"] else None
        elif sub.get("username"):
            db_user = user if user.username == sub["username"] else None
    namespace = {"ensure_datetime_timezone": ensure_datetime_timezone, "revocation_epoch": release.revocation_epoch}
    refused = eval(RELEASE_PREDICATE, namespace, {"db_user": db_user, "sub": sub})
    return "refuse" if refused else "accept"


def _hotfix_decision(store, token, user):
    store.clear()
    if user is not None:
        store[user.id] = user
    try:
        resolved = asyncio.run(BaseOperation(OperatorType.API).get_validated_sub(None, token))
    except HTTPException as exc:
        return "refuse" if exc.status_code == 404 else f"http{exc.status_code}"
    except Exception as exc:
        return f"error:{type(exc).__name__}"
    assert resolved is user
    return "accept"


def _naive(instant: datetime) -> datetime:
    return instant.astimezone(UTC).replace(tzinfo=None)


def _scenario(release):
    now = datetime.now(UTC).replace(microsecond=0)
    revoked = now - timedelta(minutes=30, microseconds=-700_000)
    revoke_values = [
        None,
        revoked,
        revoked - timedelta(seconds=1),
        revoked + timedelta(seconds=1),
        revoked + timedelta(microseconds=200_000),
        revoked - timedelta(microseconds=600_000),
    ]
    tokens = [asyncio.run(release.create_subscription_token(42, value)) for value in revoke_values]
    tokens.append(asyncio.run(release.create_subscription_token(7, None)))
    tokens.append(asyncio.run(jwt_utils.create_subscription_token(42)))
    for minted in (now - timedelta(hours=2), now - timedelta(minutes=10), now, now + timedelta(hours=1)):
        ts = int(minted.timestamp())
        tokens += [
            _hmac_token(f"v3,42,{ts}"),
            _hmac_token(f"v2,42,{ts}"),
            _hmac_token(f"user42,{ts}"),
            _legacy_token(f"v2,42,{ts}", hex_signature=True),
            _legacy_token(f"v3,42,{ts}", hex_signature=False),
            _legacy_token(f"v4,42,{ts},0", hex_signature=False),
            pyjwt.encode({"sub": "user42", "access": "subscription", "iat": ts}, SECRET, algorithm="HS256"),
        ]
    stale = tokens[1]
    ts = int(now.timestamp())
    tokens += [
        _tamper(stale, f"v4,43,{ts},0"),
        _tamper(stale, f"v4,42,{ts + 1},0"),
        _tamper(stale, f"v4,42,{ts},{jwt_utils.revocation_epoch(revoked)}"),
        _hmac_token(f"v4,42,{ts},0", secret="a-guess-at-the-secret"),
        _legacy_token(f"v4,42,{ts},0", hex_signature=True, secret="a-guess-at-the-secret"),
        pyjwt.encode({"sub": "user42", "access": "subscription", "iat": ts}, "a-guess", algorithm="HS256"),
        _hmac_token("v4,42,99999999999999999999,0"),
        _legacy_token("v4,42,9999999999999,0", hex_signature=True),
        _body("v4,42,1900000000,0") + "é" * 10,
        stale.rpartition(".")[0] + ".",
        "." + stale.rpartition(".")[2],
    ]
    users = [None]
    for created in (now - timedelta(days=30), now + timedelta(hours=2)):
        for value in revoke_values + [now - timedelta(seconds=1)]:
            users.append(_User(42, created, value))
            users.append(_User(42, _naive(created), None if value is None else _naive(value)))
    return tokens, users


def test_the_release_predicate_fixture_is_the_release_commit_source():
    shown = subprocess.run(
        ["git", "show", f"{PREDICATE_COMMIT}:app/operation/__init__.py"], cwd=ROOT, capture_output=True, check=False
    )
    if shown.returncode:
        pytest.skip(f"release commit {PREDICATE_COMMIT} is not in this checkout; the sha256 pin still holds")
    assert _predicate_of(shown.stdout.decode("utf-8")) == ast.unparse(
        ast.parse("(" + _release_predicate_source() + ")", mode="eval").body
    )


def test_the_hotfix_refuses_exactly_what_the_release_refuses(secret, release, store, process_tz):
    tokens, users = _scenario(release)
    mismatches = []
    for token in tokens:
        for user in users:
            expected = _release_decision(release, token, user)
            got = _hotfix_decision(store, token, user)
            if expected == "error":
                expected = "refuse"
            if got != expected:
                mismatches.append((process_tz, token[:40], vars(user) if user else None, expected, got))
    assert not mismatches, mismatches[:5]


def test_no_mutated_token_is_accepted_by_one_side_only_or_crashes_the_hotfix(secret, release, store):
    tokens, _ = _scenario(release)
    fuzzed = _fuzz(tokens, 400, seed=20261008)
    user = _User(42, datetime.now(UTC) - timedelta(days=30), None)
    for token in fuzzed:
        expected = _release_decision(release, token, user)
        got = _hotfix_decision(store, token, user)
        assert got in ("accept", "refuse"), (token, got)
        assert got == ("refuse" if expected == "error" else expected), (token, expected, got)
