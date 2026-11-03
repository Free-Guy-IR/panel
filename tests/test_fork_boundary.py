import functools
import json
import re
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi import APIRouter
from fastapi.routing import APIRoute
from fastapi.security.base import SecurityBase

from app import lifecycle
from app.fork import bootstrap, registry
from app.fork.routers import FORK_ROUTER_MODULES
from app.routers import authentication
from config import subscription_env_settings

REPO_ROOT = Path(__file__).resolve().parent.parent
PUBLIC_LOCALE_RELATIVE_DIR = "dashboard/public/statics/locales"
FORK_LOCALE_DIR = REPO_ROOT / "dashboard" / "src" / "fork" / "locales"
LOCALE_CODES = ("en", "fa", "ru", "zh")
UPSTREAM_SYNC_POINT_FILE = REPO_ROOT / ".upstream-sync-point"
UPSTREAM_REF = (
    UPSTREAM_SYNC_POINT_FILE.read_text(encoding="utf-8").strip() if UPSTREAM_SYNC_POINT_FILE.is_file() else "v5.4.1"
)
UPSTREAM_LOCALE_PICKS_FILE = REPO_ROOT / "tests" / "fixtures" / "upstream_locale_picks.json"
CHERRY_PICK_TRAILER_PREFIX = "(cherry picked from commit "
UPSTREAM_REPOSITORY_URL = "https://github.com/PasarGuard/panel.git"
UPSTREAM_REF_NAMESPACES = ("refs/remotes/upstream", "refs/upstream-tags")
UPSTREAM_FETCH_COMMAND = (
    f"git fetch --no-tags {UPSTREAM_REPOSITORY_URL} '+refs/heads/*:refs/remotes/upstream/*' "
    "'+refs/tags/*:refs/upstream-tags/*'"
)
FULL_COMMIT_SHA = re.compile(r"[0-9a-f]{40}")

FORK_MODULE_PREFIX = "app.fork."
FORK_ROUTER_MODULE_PREFIX = "app.fork.routers."
CONNECTION_LIMIT_PREFIX = "/api/connection-limit/"

UNSAFE_LOCALE_KEYS = frozenset({"__proto__", "prototype", "constructor"})
MISSING = object()

SUBSCRIPTION_PING_ENDPOINT = (f"/{subscription_env_settings.path}/{{token}}/ping", "GET")
TRAFFIC_LOG_LIVE_ENDPOINT = ("/api/traffic-log/live", "GET")
TOKEN_SCOPED_FORK_ROUTES = frozenset({SUBSCRIPTION_PING_ENDPOINT, TRAFFIC_LOG_LIVE_ENDPOINT})

CONNECTION_LIMIT_ENDPOINTS = frozenset(
    {
        ("/api/connection-limit/states", "GET"),
        ("/api/connection-limit/states/by-user", "GET"),
        ("/api/connection-limit/defaults/cdn-ranges", "GET"),
        ("/api/connection-limit/overrides", "GET"),
        ("/api/connection-limit/overrides/{user_id}", "PUT"),
        ("/api/connection-limit/overrides/{user_id}", "DELETE"),
        ("/api/connection-limit/addresses/{user_id}", "GET"),
        ("/api/connection-limit/violations", "GET"),
        ("/api/connection-limit/violations/{user_id}/release", "POST"),
    }
)

NON_CONNECTION_LIMIT_FORK_ENDPOINTS = frozenset(
    {
        ("/api/core/openvpn/generate-pki", "POST"),
        ("/api/core/{core_id}/mtproto/{tag}/registration-secret", "GET"),
        ("/api/node/inbounds/usage", "GET"),
        ("/api/users/bulk/mtproto_activate", "POST"),
        ("/api/users/bulk/l2tp_activate", "POST"),
        ("/api/users/bulk/openvpn_activate", "POST"),
        ("/api/users/bulk/repair_duplicate_secrets", "POST"),
        SUBSCRIPTION_PING_ENDPOINT,
    }
)

CONTENT_FILTER_ENDPOINTS = frozenset(
    {
        ("/api/content-filter/catalog", "GET"),
        ("/api/content-filter/capability", "GET"),
        ("/api/content-filter/targets", "GET"),
        ("/api/content-filter/profiles", "GET"),
        ("/api/content-filter/profiles", "POST"),
        ("/api/content-filter/profiles/{profile_id}", "PUT"),
        ("/api/content-filter/profiles/{profile_id}", "DELETE"),
        ("/api/content-filter/assignments", "GET"),
        ("/api/content-filter/assignments", "POST"),
        ("/api/content-filter/assignments/bulk", "POST"),
        ("/api/content-filter/assignments/{assignment_id}", "DELETE"),
        ("/api/content-filter/assignments/{assignment_id}/apply", "POST"),
        ("/api/content-filter/test", "POST"),
    }
)

TRAFFIC_LOG_ENDPOINTS = frozenset(
    {
        TRAFFIC_LOG_LIVE_ENDPOINT,
        ("/api/traffic-log/history", "GET"),
        ("/api/traffic-log/summary", "GET"),
        ("/api/traffic-log/status", "GET"),
        ("/api/traffic-log/settings", "PUT"),
        ("/api/traffic-log/purge", "POST"),
    }
)

EXPECTED_FORK_ENDPOINTS = (
    CONNECTION_LIMIT_ENDPOINTS | NON_CONNECTION_LIMIT_FORK_ENDPOINTS | CONTENT_FILTER_ENDPOINTS | TRAFFIC_LOG_ENDPOINTS
)

AUTHENTICATION_DEPENDENCIES = frozenset(
    {
        authentication.get_current,
        authentication.get_current_with_metrics,
        authentication.require_owner,
    }
)

ACCEPTED_LOCALE_VALUE_OVERRIDES = {
    "en": frozenset(),
    "fa": frozenset({("hostsDialog", "sni.info")}),
    "ru": frozenset({("hostsDialog", "sni.info"), ("hostsDialog", "sniPlaceholder")}),
    "zh": frozenset(),
}

REGISTRY_SANDBOX_PREFIX = "fork-boundary-test-"


def _flatten_routes(routes):
    collected = []
    for route in routes:
        if isinstance(route, APIRoute):
            collected.append(route)
        elif type(route).__name__ == "_IncludedRouter" and hasattr(route, "original_router"):
            collected.extend(_flatten_routes(route.original_router.routes))
        elif hasattr(route, "routes"):
            collected.extend(_flatten_routes(route.routes))
    return collected


def _route_pairs(routes):
    pairs = {}
    for route in routes:
        for method in sorted(route.methods or ()):
            pairs.setdefault((route.path, method), []).append(route)
    return pairs


def _endpoint_module(route):
    return getattr(route.endpoint, "__module__", "") or ""


def _is_fork_route(route):
    return _endpoint_module(route).startswith(FORK_MODULE_PREFIX)


def _dependency_callables(dependant, seen=None):
    if seen is None:
        seen = set()
    if id(dependant) in seen:
        return []
    seen.add(id(dependant))
    collected = []
    call = getattr(dependant, "call", None)
    if call is not None:
        collected.append(call)
    for requirement in getattr(dependant, "security_requirements", None) or ():
        scheme = getattr(requirement, "security_scheme", None)
        if scheme is not None:
            collected.append(scheme)
    for sub_dependant in getattr(dependant, "dependencies", None) or ():
        collected.extend(_dependency_callables(sub_dependant, seen))
    return collected


def _identity_count(items, target):
    return sum(1 for item in items if item is target)


def _is_plain_locale_node(value):
    return isinstance(value, dict)


def _merge_deep(base, extra):
    if not _is_plain_locale_node(extra):
        return base if extra is MISSING else extra
    if not _is_plain_locale_node(base):
        return extra
    merged = dict(base)
    for key, value in extra.items():
        if key in UNSAFE_LOCALE_KEYS:
            continue
        merged[key] = _merge_deep(base.get(key, MISSING), value)
    return merged


def _flatten_locale(tree, path=()):
    flat = {}
    for key, value in tree.items():
        if isinstance(value, dict):
            flat.update(_flatten_locale(value, path + (key,)))
        else:
            flat[path + (key,)] = value
    return flat


def _git(*args):
    return subprocess.run(["git", *args], cwd=REPO_ROOT, capture_output=True, text=True, check=False)


def _is_commit_in_clone(commit):
    return _git("cat-file", "-e", f"{commit}^{{commit}}").returncode == 0


@functools.cache
def _fork_only_commits():
    result = _git("rev-list", "HEAD", "--not", *(f"--glob={namespace}" for namespace in UPSTREAM_REF_NAMESPACES))
    assert result.returncode == 0, f"cannot list the fork-only history of HEAD: {result.stderr.strip()}"
    return frozenset(result.stdout.split())


@functools.cache
def _upstream_refs_containing(commit):
    result = _git("for-each-ref", "--contains", commit, "--format=%(refname)", *UPSTREAM_REF_NAMESPACES)
    assert result.returncode == 0, f"cannot list the upstream refs containing {commit}: {result.stderr.strip()}"
    return tuple(result.stdout.split())


def _require_upstream_commit(commit, role):
    if not FULL_COMMIT_SHA.fullmatch(commit):
        pytest.fail(f"the {role} {commit!r} is not a full lowercase 40-hex commit SHA")
    if not _is_commit_in_clone(commit):
        pytest.fail(
            f"the {role} {commit} is not in this clone, fetch the upstream branches and tags by name: {UPSTREAM_FETCH_COMMAND}"
        )
    if commit in _fork_only_commits():
        pytest.fail(f"the {role} {commit} is a commit of the fork's own history, not an upstream commit")
    if not _upstream_refs_containing(commit):
        pytest.fail(
            f"the {role} {commit} is not reachable from any upstream branch or tag under "
            f"{' or '.join(UPSTREAM_REF_NAMESPACES)}: {UPSTREAM_FETCH_COMMAND}"
        )


def _require_upstream_ref():
    resolved = _git("rev-parse", "--verify", "--quiet", f"{UPSTREAM_REF}^{{commit}}")
    if resolved.returncode != 0:
        pytest.fail(f"the upstream sync point {UPSTREAM_REF} is not in this clone: {UPSTREAM_FETCH_COMMAND}")
    _require_upstream_commit(resolved.stdout.strip(), f"upstream sync point {UPSTREAM_REF}")


def _load_json_file(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _locale_at(ref, code):
    shown = _git("show", f"{ref}:{PUBLIC_LOCALE_RELATIVE_DIR}/{code}.json")
    if shown.returncode != 0:
        return {}
    return _flatten_locale(json.loads(shown.stdout))


def _locale_delta(before_flat, after_flat):
    return {
        "set": [
            [list(key), value] for key, value in sorted(after_flat.items()) if before_flat.get(key, MISSING) != value
        ],
        "removed": [list(key) for key in sorted(before_flat) if key not in after_flat],
    }


def _commit_locale_delta(commit, code):
    return _locale_delta(_locale_at(f"{commit}^", code), _locale_at(commit, code))


def _recorded_locale_picks():
    return _load_json_file(UPSTREAM_LOCALE_PICKS_FILE)["picks"]


def _recorded_pick_delta(pick, code):
    return pick["locales"].get(code, {"set": [], "removed": []})


def _apply_locale_picks(baseline_flat, picks, code):
    expected = dict(baseline_flat)
    for pick in picks:
        delta = _recorded_pick_delta(pick, code)
        for path, value in delta["set"]:
            expected[tuple(path)] = value
        for path in delta["removed"]:
            expected.pop(tuple(path), None)
    return expected


def _public_locale_violations(local_flat, expected_flat):
    added = sorted(set(local_flat) - set(expected_flat))
    removed = sorted(set(expected_flat) - set(local_flat))
    retranslated = sorted(key for key in expected_flat if key in local_flat and local_flat[key] != expected_flat[key])
    return added, removed, retranslated


def _ported_public_locale_picks():
    log = _git("log", "--full-history", "--format=%H%x00%B%x01", "HEAD", "--", PUBLIC_LOCALE_RELATIVE_DIR)
    assert log.returncode == 0, f"git log over {PUBLIC_LOCALE_RELATIVE_DIR} failed: {log.stderr.strip()}"
    picks = {}
    for record in log.stdout.split("\x01"):
        fork_commit, _, body = record.strip().partition("\x00")
        for line in body.splitlines():
            line = line.strip()
            if line.startswith(CHERRY_PICK_TRAILER_PREFIX) and line.endswith(")"):
                picks[line[len(CHERRY_PICK_TRAILER_PREFIX) : -1]] = fork_commit
    return picks


def _is_ahead_of_sync_point(upstream_commit):
    _require_upstream_commit(upstream_commit, "cherry-pick trailer commit")
    result = _git("merge-base", "--is-ancestor", upstream_commit, UPSTREAM_REF)
    if result.returncode not in (0, 1):
        pytest.fail(f"cannot place {upstream_commit} against {UPSTREAM_REF}: {result.stderr.strip()}")
    return result.returncode == 1


def _ported_public_locale_picks_ahead_of_sync_point():
    return {
        upstream: fork for upstream, fork in _ported_public_locale_picks().items() if _is_ahead_of_sync_point(upstream)
    }


@pytest.fixture(scope="module")
def fork_app():
    from app.app_factory import create_app

    startup_snapshot = list(lifecycle.startup_functions)
    shutdown_snapshot = list(lifecycle.shutdown_functions)
    try:
        return create_app()
    finally:
        lifecycle.startup_functions[:] = startup_snapshot
        lifecycle.shutdown_functions[:] = shutdown_snapshot


@pytest.fixture(scope="module")
def flattened_routes(fork_app):
    routes = _flatten_routes(fork_app.routes)
    assert len(routes) > len(fork_app.routes), (
        "the route walk never recursed into _IncludedRouter.original_router.routes, "
        f"so it only saw {len(routes)} routes and every route assertion below would be vacuous"
    )
    return routes


@pytest.fixture(scope="module")
def fork_routes(flattened_routes):
    routes = [route for route in flattened_routes if _is_fork_route(route)]
    assert routes, "no route resolves to an app.fork. endpoint module"
    return routes


@pytest.fixture
def registry_sandbox():
    router_keys = dict(registry._ROUTER_KEYS)
    routers = list(registry._ROUTERS)
    core_types = dict(registry._CORE_TYPES)
    yield
    registry._ROUTER_KEYS.clear()
    registry._ROUTER_KEYS.update(router_keys)
    registry._ROUTERS[:] = routers
    registry._CORE_TYPES.clear()
    registry._CORE_TYPES.update(core_types)


def test_no_duplicate_path_and_method_pairs(flattened_routes):
    pairs = _route_pairs(flattened_routes)
    duplicates = {key: sorted(route.name for route in routes) for key, routes in pairs.items() if len(routes) > 1}
    report = ", ".join(f"{method} {path} -> {names}" for (path, method), names in sorted(duplicates.items()))
    assert not duplicates, f"duplicate path+method routes registered: {report}"


def test_no_fork_route_shadows_an_upstream_route(flattened_routes):
    fork_pairs = {}
    upstream_pairs = {}
    for route in flattened_routes:
        target = fork_pairs if _is_fork_route(route) else upstream_pairs
        for method in sorted(route.methods or ()):
            target.setdefault((route.path, method), _endpoint_module(route) + "." + route.name)
    shadowed = sorted(set(fork_pairs) & set(upstream_pairs))
    report = ", ".join(
        f"{method} {path} (fork={fork_pairs[(path, method)]} upstream={upstream_pairs[(path, method)]})"
        for path, method in shadowed
    )
    assert not shadowed, f"fork routes shadow upstream routes: {report}"


def test_every_expected_fork_router_is_registered(fork_app):
    expected_keys = {key for _, key in FORK_ROUTER_MODULES}
    registered = registry.registered_router_keys()
    missing = sorted(expected_keys - registered)
    assert not missing, f"fork router keys absent from the registry after create_app(): {missing}"
    assert fork_app.routes


def test_required_subscription_formats_are_registered():
    bootstrap.load_fork()
    formats = registry.extra_subscription_formats()
    missing = [key for key in ("openvpn", "l2tp") if key not in formats]
    assert not missing, f"required fork subscription formats missing after load_fork(): {missing}"
    assert all(callable(formats[key]) for key in ("openvpn", "l2tp"))
    assert {"openvpn", "l2tp"} <= set(bootstrap.REQUIRED_SUBSCRIPTION_FORMATS)
    bootstrap.verify_fork_capabilities()


def test_fork_endpoint_surface_is_intact(fork_routes):
    live_pairs = set(_route_pairs(fork_routes))
    missing = sorted(EXPECTED_FORK_ENDPOINTS - live_pairs)
    assert not missing, f"fork endpoints vanished from the route table: {missing}"
    undeclared = sorted(live_pairs - EXPECTED_FORK_ENDPOINTS)
    assert not undeclared, f"fork endpoints not declared in EXPECTED_FORK_ENDPOINTS: {undeclared}"


def test_connection_limit_control_surface_is_intact(fork_routes):
    live_pairs = {pair for pair in _route_pairs(fork_routes) if pair[0].startswith(CONNECTION_LIMIT_PREFIX)}
    missing = sorted(CONNECTION_LIMIT_ENDPOINTS - live_pairs)
    undeclared = sorted(live_pairs - CONNECTION_LIMIT_ENDPOINTS)
    assert not missing, f"connection-limit endpoints vanished: {missing}"
    assert not undeclared, f"undeclared connection-limit endpoints: {undeclared}"


def test_authentication_is_present_on_every_fork_endpoint(fork_routes):
    unauthenticated = set()
    inspected = 0
    for route in fork_routes:
        if not _endpoint_module(route).startswith(FORK_ROUTER_MODULE_PREFIX):
            continue
        inspected += 1
        resolved = _dependency_callables(route.dependant)
        has_auth_dependency = any(item in AUTHENTICATION_DEPENDENCIES for item in resolved)
        has_security_scheme = any(isinstance(item, SecurityBase) for item in resolved)
        if has_auth_dependency and has_security_scheme:
            continue
        for method in sorted(route.methods or ()):
            unauthenticated.add((route.path, method))
    assert inspected == len(EXPECTED_FORK_ENDPOINTS)
    unprotected = sorted(unauthenticated - TOKEN_SCOPED_FORK_ROUTES)
    assert not unprotected, f"fork endpoints reachable without an authentication dependency: {unprotected}"
    stale_exceptions = sorted(TOKEN_SCOPED_FORK_ROUTES - unauthenticated)
    assert not stale_exceptions, f"documented token-scoped exceptions now carry auth, drop them: {stale_exceptions}"


@pytest.mark.parametrize("code", LOCALE_CODES)
def test_public_locale_file_is_unchanged_from_upstream(code):
    _require_upstream_ref()
    relative_path = f"{PUBLIC_LOCALE_RELATIVE_DIR}/{code}.json"
    shown = _git("show", f"{UPSTREAM_REF}:{relative_path}")
    assert shown.returncode == 0, f"{UPSTREAM_REF}:{relative_path} is not readable: {shown.stderr.strip()}"
    baseline_flat = _flatten_locale(json.loads(shown.stdout))
    assert baseline_flat, f"{code}: the pinned upstream baseline {UPSTREAM_REF} produced no keys"
    upstream_flat = _apply_locale_picks(baseline_flat, _recorded_locale_picks(), code)
    local_flat = _flatten_locale(_load_json_file(REPO_ROOT / relative_path))
    added, removed, retranslated = _public_locale_violations(local_flat, upstream_flat)
    assert not added, (
        f"{code}: keys added to the public locale, they belong in dashboard/src/fork/locales: {added[:10]}"
    )
    assert not removed, f"{code}: keys removed from the public locale: {removed[:10]}"
    assert not retranslated, f"{code}: public locale values edited in place: {retranslated[:10]}"


def test_recorded_locale_picks_are_exactly_the_ported_upstream_commits_ahead_of_the_sync_point():
    _require_upstream_ref()
    recorded = [pick["upstream"] for pick in _recorded_locale_picks()]
    assert len(recorded) == len(set(recorded)), f"duplicate entries in {UPSTREAM_LOCALE_PICKS_FILE.name}: {recorded}"
    ported = _ported_public_locale_picks_ahead_of_sync_point()
    unrecorded = sorted(set(ported) - set(recorded))
    assert not unrecorded, (
        f"cherry-picked upstream commits ahead of {UPSTREAM_REF} change the public locale without an entry in "
        f"{UPSTREAM_LOCALE_PICKS_FILE.name}: {unrecorded}"
    )
    unported = sorted(set(recorded) - set(ported))
    assert not unported, (
        f"{UPSTREAM_LOCALE_PICKS_FILE.name} lists upstream commits with no cherry-pick -x trailer on a public "
        f"locale change ahead of {UPSTREAM_REF}: {unported}"
    )


@pytest.mark.parametrize("code", LOCALE_CODES)
def test_recorded_locale_picks_match_the_fork_cherry_pick_commits(code):
    _require_upstream_ref()
    ported = _ported_public_locale_picks_ahead_of_sync_point()
    for pick in _recorded_locale_picks():
        fork_commit = ported.get(pick["upstream"])
        assert fork_commit, f"{pick['upstream']} has no fork cherry-pick commit"
        assert _commit_locale_delta(fork_commit, code) == _recorded_pick_delta(pick, code), (
            f"{code}: fork commit {fork_commit} does not apply exactly the recorded locale change of {pick['upstream']}"
        )


@pytest.mark.parametrize("code", LOCALE_CODES)
def test_recorded_locale_picks_match_the_upstream_commits(code):
    _require_upstream_ref()
    picks = _recorded_locale_picks()
    for pick in picks:
        _require_upstream_commit(pick["upstream"], "recorded upstream locale pick")
    for pick in picks:
        upstream_commit = pick["upstream"]
        assert _commit_locale_delta(upstream_commit, code) == _recorded_pick_delta(pick, code), (
            f"{code}: the recorded locale change of {upstream_commit} differs from the upstream commit itself"
        )


def test_public_locale_check_rejects_an_arbitrary_key_and_a_changed_ported_value():
    baseline_flat = {("common", "save"): "Save"}
    picks = [
        {
            "upstream": "0" * 40,
            "locales": {"en": {"set": [[["coreEditor", "api", "title"], "API"]], "removed": []}},
        }
    ]
    expected = _apply_locale_picks(baseline_flat, picks, "en")
    assert expected == {("common", "save"): "Save", ("coreEditor", "api", "title"): "API"}
    assert _public_locale_violations(dict(expected), expected) == ([], [], [])
    arbitrary = {**expected, ("coreEditor", "api", "forkOnly"): "Fork text"}
    assert _public_locale_violations(arbitrary, expected)[0] == [("coreEditor", "api", "forkOnly")]
    edited = {**expected, ("coreEditor", "api", "title"): "API (edited)"}
    assert _public_locale_violations(edited, expected)[2] == [("coreEditor", "api", "title")]
    dropped = {("common", "save"): "Save"}
    assert _public_locale_violations(dropped, expected)[1] == [("coreEditor", "api", "title")]
    assert _apply_locale_picks(baseline_flat, picks, "fa") == baseline_flat


FORGED_UPSTREAM_SHA = "f0" * 20


def test_a_forged_cherry_pick_trailer_sha_is_rejected_not_counted_as_ahead():
    _require_upstream_ref()
    with pytest.raises(pytest.fail.Exception, match=FORGED_UPSTREAM_SHA):
        _is_ahead_of_sync_point(FORGED_UPSTREAM_SHA)


def test_a_recorded_upstream_pick_missing_from_the_clone_fails_the_upstream_check(monkeypatch):
    forged = {"upstream": FORGED_UPSTREAM_SHA, "locales": {"en": {"set": [], "removed": []}}}
    monkeypatch.setattr(
        sys.modules[__name__],
        "_recorded_locale_picks",
        lambda: [*_load_json_file(UPSTREAM_LOCALE_PICKS_FILE)["picks"], forged],
    )
    with pytest.raises(pytest.fail.Exception, match=FORGED_UPSTREAM_SHA):
        test_recorded_locale_picks_match_the_upstream_commits("en")


def test_an_unreachable_sync_point_fails_the_boundary_checks_instead_of_skipping(monkeypatch):
    monkeypatch.setattr(sys.modules[__name__], "UPSTREAM_REF", FORGED_UPSTREAM_SHA)
    with pytest.raises(pytest.fail.Exception, match=FORGED_UPSTREAM_SHA):
        _require_upstream_ref()


def test_a_fork_commit_laundered_as_an_upstream_locale_pick_is_rejected(monkeypatch):
    _require_upstream_ref()
    real_trailers = _ported_public_locale_picks()
    real_picks = _load_json_file(UPSTREAM_LOCALE_PICKS_FILE)["picks"]
    fork_commit = real_trailers[real_picks[0]["upstream"]]
    laundered_pick = {**real_picks[0], "upstream": fork_commit}
    module = sys.modules[__name__]
    monkeypatch.setattr(module, "_ported_public_locale_picks", lambda: {**real_trailers, fork_commit: fork_commit})
    monkeypatch.setattr(module, "_recorded_locale_picks", lambda: [*real_picks, laundered_pick])
    with pytest.raises(pytest.fail.Exception, match=fork_commit):
        _ported_public_locale_picks_ahead_of_sync_point()
    with pytest.raises(pytest.fail.Exception, match=fork_commit):
        test_recorded_locale_picks_match_the_upstream_commits("en")


def test_an_abbreviated_or_uppercase_upstream_sha_is_rejected():
    _require_upstream_ref()
    full = _recorded_locale_picks()[0]["upstream"]
    for malformed in (full[:12], full.upper()):
        with pytest.raises(pytest.fail.Exception, match=malformed):
            _is_ahead_of_sync_point(malformed)


def test_public_locale_check_rejects_a_new_key_in_the_real_public_file():
    _require_upstream_ref()
    baseline_flat = _locale_at(UPSTREAM_REF, "en")
    expected = _apply_locale_picks(baseline_flat, _recorded_locale_picks(), "en")
    local_flat = _flatten_locale(_load_json_file(REPO_ROOT / PUBLIC_LOCALE_RELATIVE_DIR / "en.json"))
    tampered = {**local_flat, ("coreEditor", "api", "forkOnlyHint"): "not upstream text"}
    assert _public_locale_violations(tampered, expected)[0] == [("coreEditor", "api", "forkOnlyHint")]


@pytest.mark.parametrize("code", LOCALE_CODES)
def test_fork_locale_merge_loses_nothing(code):
    public = _load_json_file(REPO_ROOT / PUBLIC_LOCALE_RELATIVE_DIR / f"{code}.json")
    overlay = _load_json_file(FORK_LOCALE_DIR / f"{code}.json")
    merged = _merge_deep(public, overlay)
    public_flat = _flatten_locale(public)
    merged_flat = _flatten_locale(merged)
    lost = sorted(key for key in public_flat if key not in merged_flat)
    assert not lost, f"{code}: the fork overlay dropped {len(lost)} public keys, e.g. {lost[:10]}"
    assert len(merged_flat) >= len(public_flat)
    overridden = {key for key, value in public_flat.items() if merged_flat[key] != value}
    unexpected = sorted(overridden - ACCEPTED_LOCALE_VALUE_OVERRIDES[code])
    assert not unexpected, f"{code}: the fork overlay silently rewrote upstream values: {unexpected}"
    no_longer_overridden = sorted(ACCEPTED_LOCALE_VALUE_OVERRIDES[code] - overridden)
    assert not no_longer_overridden, f"{code}: stale entries in ACCEPTED_LOCALE_VALUE_OVERRIDES: {no_longer_overridden}"


def test_fork_locale_merge_skips_prototype_polluting_keys():
    base = {"kept": "base", "nested": {"kept": "base"}}
    overlay = {
        "__proto__": {"polluted": True},
        "prototype": "polluted",
        "constructor": "polluted",
        "nested": {"added": "overlay"},
        "added": "overlay",
    }
    assert _merge_deep(base, overlay) == {
        "kept": "base",
        "nested": {"kept": "base", "added": "overlay"},
        "added": "overlay",
    }


def test_fork_locale_merge_prefers_overlay_leaves_over_base_subtrees():
    assert _merge_deep({"a": {"b": "base"}}, {"a": "overlay"}) == {"a": "overlay"}
    assert _merge_deep({"a": "base"}, {"a": {"b": "overlay"}}) == {"a": {"b": "overlay"}}
    assert _merge_deep({"a": "base"}, {"a": None}) == {"a": None}


def test_register_router_rejects_a_conflicting_key(registry_sandbox):
    key = f"{REGISTRY_SANDBOX_PREFIX}router"
    first = APIRouter(prefix=f"/{REGISTRY_SANDBOX_PREFIX}first")
    second = APIRouter(prefix=f"/{REGISTRY_SANDBOX_PREFIX}second")

    registry.register_router(first, name=key)
    assert key in registry.registered_router_keys()
    assert _identity_count(registry.extra_routers(), first) == 1

    registry.register_router(first, name=key)
    assert _identity_count(registry.extra_routers(), first) == 1
    assert registry._ROUTER_KEYS[key] is first

    with pytest.raises(ValueError, match="already registered with a different router"):
        registry.register_router(second, name=key)
    assert _identity_count(registry.extra_routers(), second) == 0
    assert registry._ROUTER_KEYS[key] is first


def test_register_core_type_rejects_a_conflicting_class(registry_sandbox):
    key = f"{REGISTRY_SANDBOX_PREFIX}core-type"

    class FirstCore:
        pass

    class SecondCore:
        pass

    registry.register_core_type(key, FirstCore)
    assert registry.extra_core_types()[key] is FirstCore

    registry.register_core_type(key, FirstCore)
    assert registry.extra_core_types()[key] is FirstCore

    with pytest.raises(ValueError, match="already registered as"):
        registry.register_core_type(key, SecondCore)
    assert registry.extra_core_types()[key] is FirstCore


def test_registry_sandbox_left_no_residue():
    leaked_routers = [key for key in registry.registered_router_keys() if str(key).startswith(REGISTRY_SANDBOX_PREFIX)]
    leaked_cores = [key for key in registry.extra_core_types() if str(key).startswith(REGISTRY_SANDBOX_PREFIX)]
    assert not leaked_routers, f"throwaway router keys still registered: {leaked_routers}"
    assert not leaked_cores, f"throwaway core types still registered: {leaked_cores}"


def test_violation_response_accepts_the_legacy_reason_shape():
    from app.models.connection_limit import ConnectionStateResponse, ConnectionViolationResponse

    legacy = ["disabled 9 devices against a limit of 2"]
    structured = [{"code": "over_limit", "devices": 9, "limit": 2}]

    for payload in (legacy, structured, legacy + structured):
        violation = ConnectionViolationResponse(id=1, user_id=1, created_at=datetime.now(UTC), reasons=payload)
        assert violation.reasons == payload
        state = ConnectionStateResponse(user_id=1, reasons=payload)
        assert state.reasons == payload
