#!/usr/bin/env python3
import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import tomllib
import urllib.error
import urllib.request
from datetime import UTC, date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import upstream_inventory

MODES = {"self", "ledger", "workflow", "covered-by", "original"}
VERSION_TAG_RE = re.compile(r"^v?(\d+\.\d+\.\d+(?:-[0-9A-Za-z.]+)?)$")
ARCH_SUFFIX_RE = re.compile(r"-(amd64|arm64|armv7|arm)$")
HTTP_TIMEOUT = 20


def run_git(cwd, *args, check=True):
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=False)
    if check and result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed in {cwd}: {result.stderr.strip()}")
    return result


def github_url(slug):
    return f"https://github.com/{slug}.git"


def load_config(path):
    try:
        data = tomllib.loads(Path(path).read_text())
    except (OSError, tomllib.TOMLDecodeError) as exc:
        sys.stderr.write(f"error: cannot read {path}: {exc}\n")
        raise SystemExit(2)
    repos = data.get("repo", [])
    images = data.get("image", [])
    errors = []
    names = set()
    for entry in repos:
        name = entry.get("name")
        mode = entry.get("mode")
        if not name or mode not in MODES:
            errors.append(f"repo entry {entry!r} needs a name and a mode in {sorted(MODES)}")
            continue
        if name in names:
            errors.append(f"repo {name} is listed twice")
        names.add(name)
        if not str(entry.get("reason", "")).strip():
            errors.append(f"repo {name} has no reason")
        required = {
            "ledger": ("fork", "upstream", "ported_ref", "ledger"),
            "workflow": ("fork", "upstream", "tracker_branch", "tracker_path"),
            "covered-by": ("covered_by",),
            "self": ("fork", "upstream", "tracker_path"),
            "original": ("fork",),
        }[mode]
        for field in required:
            if not entry.get(field):
                errors.append(f"repo {name} ({mode}) needs {field}")
    for entry in repos:
        target = entry.get("covered_by")
        if target and target not in names:
            errors.append(f"repo {entry['name']} is covered by {target}, which is not in the map")
    for image in images:
        if not image.get("name") or not image.get("upstream"):
            errors.append(f"image entry {image!r} needs a name and an upstream repo")
    return repos, images, errors


def fetch_mirror(workdir, name, fork_url, upstream_url):
    mirror = Path(workdir) / f"{name}.git"
    if not mirror.exists():
        run_git(workdir, "init", "-q", "--bare", str(mirror))
    run_git(mirror, "fetch", "-q", "--no-tags", fork_url, "+refs/heads/*:refs/remotes/fork/*")
    run_git(
        mirror,
        "fetch",
        "-q",
        "--no-tags",
        upstream_url,
        "+refs/heads/*:refs/remotes/upstream/*",
        "+refs/tags/*:refs/upstream-tags/*",
    )
    return mirror


def fetch_snapshot_heads(mirror, snapshot, upstream_url):
    if not snapshot or not Path(snapshot).is_file():
        return
    for line in Path(snapshot).read_text().splitlines():
        sha = line.split(" ", 1)[0].strip()
        if not sha:
            continue
        if run_git(mirror, "cat-file", "-e", f"{sha}^{{commit}}", check=False).returncode != 0:
            run_git(mirror, "fetch", "-q", "--no-tags", upstream_url, sha, check=False)


def check_ledger_repo(entry, root, workdir, today, write_snapshot):
    name = entry["name"]
    fork_url = entry.get("fork_url") or github_url(entry["fork"])
    upstream_url = entry.get("upstream_url") or github_url(entry["upstream"])
    ledger = str((root / entry["ledger"]).resolve())
    snapshot = str((root / entry["snapshot"]).resolve()) if entry.get("snapshot") else None
    mirror = fetch_mirror(workdir, name, fork_url, upstream_url)
    fetch_snapshot_heads(mirror, snapshot, upstream_url)
    base = f"refs/remotes/fork/{entry['ported_ref']}"
    previous = Path.cwd()
    os.chdir(mirror)
    try:
        print(f"\n=== {name}: {entry['upstream']} tracked against {entry['fork']} {entry['ported_ref']}")
        sys.stdout.flush()
        return upstream_inventory.parity_gate(
            base,
            base,
            ["refs/remotes/upstream/*", "refs/upstream-tags/*"],
            ledger,
            snapshot,
            write_snapshot,
            today,
            require_shared_history=False,
        )
    finally:
        os.chdir(previous)


def pending_verdict(entry, today, missing):
    until = entry.get("pending_until")
    if not until:
        return 1, f"FAILED: {missing}"
    deadline = date.fromisoformat(str(until))
    if today > deadline:
        return 1, f"FAILED: {missing}; the pending decision expired on {deadline.isoformat()}"
    return 0, f"PENDING until {deadline.isoformat()}: {missing}; {entry.get('pending_reason', '').strip()}"


def check_workflow_repo(entry, workdir, today):
    name = entry["name"]
    fork_url = entry.get("fork_url") or github_url(entry["fork"])
    mirror = Path(workdir) / f"{name}-tracker.git"
    if not mirror.exists():
        run_git(workdir, "init", "-q", "--bare", str(mirror))
    branch = entry["tracker_branch"]
    fetched = run_git(mirror, "fetch", "-q", "--no-tags", fork_url, f"+refs/heads/{branch}:refs/tracker", check=False)
    path = entry["tracker_path"]
    if fetched.returncode != 0:
        code, message = pending_verdict(entry, today, f"branch {branch} of {entry['fork']} is not published")
    elif run_git(mirror, "cat-file", "-e", f"refs/tracker:{path}", check=False).returncode != 0:
        code, message = pending_verdict(entry, today, f"{path} is not on {entry['fork']} {branch}")
    else:
        code, message = 0, f"OK: {path} is on {entry['fork']} {branch}"
    print(f"\n=== {name}: tracker workflow\n  {message}")
    return code


def owner_forks(owner, repos_file):
    if repos_file:
        data = json.loads(Path(repos_file).read_text())
    else:
        request = urllib.request.Request(f"https://api.github.com/users/{owner}/repos?per_page=100&type=owner")
        token = os.environ.get("GITHUB_TOKEN")
        if token:
            request.add_header("Authorization", f"Bearer {token}")
        try:
            with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as response:
                data = json.loads(response.read())
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            sys.stderr.write(f"error: cannot list the repositories of {owner}: {exc}\n")
            raise SystemExit(2)
    return {f"{owner}/{item['name']}" for item in data if item.get("fork")}


def check_map_completeness(repos, owner, repos_file):
    forks = owner_forks(owner, repos_file)
    mapped = {entry.get("fork") for entry in repos}
    missing = sorted(forks - mapped)
    print(f"\n=== map completeness: {len(forks)} forked repositories of {owner}, {len(missing)} missing from the map")
    for slug in missing:
        print(f"  {slug} has no entry and therefore no tracker")
    return 1 if missing else 0


def registry_tags(image, tags_file):
    if tags_file:
        return json.loads(Path(tags_file).read_text()).get(image, [])
    host, _, repository = image.partition("/")
    token_url = f"https://{host}/token?scope=repository:{repository}:pull"
    try:
        with urllib.request.urlopen(token_url, timeout=HTTP_TIMEOUT) as response:
            token = json.loads(response.read())["token"]
        request = urllib.request.Request(f"https://{host}/v2/{repository}/tags/list?n=10000")
        request.add_header("Authorization", f"Bearer {token}")
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as response:
            return json.loads(response.read()).get("tags") or []
    except (urllib.error.URLError, TimeoutError, KeyError, json.JSONDecodeError) as exc:
        sys.stderr.write(f"error: cannot list the tags of {image}: {exc}\n")
        raise SystemExit(2)


def upstream_git_tags(entry, workdir):
    url = entry.get("upstream_url") or github_url(entry["upstream"])
    listing = run_git(workdir, "ls-remote", "--tags", url).stdout
    tags = set()
    for line in listing.splitlines():
        ref = line.split("\t", 1)[1]
        tags.add(ref.removeprefix("refs/tags/").removesuffix("^{}"))
    return tags


def check_image(entry, workdir, tags_file):
    image = entry["name"]
    git_tags = upstream_git_tags(entry, workdir)
    versions = {}
    for tag in registry_tags(image, tags_file):
        release = ARCH_SUFFIX_RE.sub("", tag)
        if VERSION_TAG_RE.match(release):
            versions.setdefault(release, []).append(tag)
    untracked = sorted(release for release in versions if release not in git_tags)
    print(f"\n=== image {image}: {len(versions)} release tags, {len(untracked)} without an upstream git tag")
    for release in untracked:
        print(f"  {release} is published as an image but has no git tag the parity gate could track")
    return 1 if untracked else 0


def main():
    parser = argparse.ArgumentParser(description="Prove that every forked repository has a mechanical upstream tracker.")
    parser.add_argument("--config", default="upstream-forks/forks.toml", help="fork map (TOML)")
    parser.add_argument("--workdir", help="directory for the git mirrors (default: a temporary directory)")
    parser.add_argument("--only", action="append", default=[], help="check only these repo names (repeatable)")
    parser.add_argument("--owner", help="GitHub owner whose forked repositories must all be in the map")
    parser.add_argument("--repos-file", help="JSON list of the owner's repositories instead of the GitHub API")
    parser.add_argument("--image-tags-file", help="JSON map of image to tags instead of the registry")
    parser.add_argument("--skip-images", action="store_true", help="do not check registry image tags")
    parser.add_argument("--write-refs-snapshot", action="store_true", help="record the reviewed upstream heads")
    parser.add_argument("--today", metavar="YYYY-MM-DD", help="date used for until: deadlines (default: today, UTC)")
    args = parser.parse_args()

    config = Path(args.config).resolve()
    root = config.parent.parent
    repos, images, errors = load_config(config)
    if errors:
        for message in errors:
            print(f"map error: {message}")
        raise SystemExit(1)
    today = date.fromisoformat(args.today) if args.today else datetime.now(UTC).date()
    selected = [entry for entry in repos if not args.only or entry["name"] in args.only]

    with tempfile.TemporaryDirectory(prefix="upstream-forks-") as temporary:
        workdir = Path(args.workdir or temporary).resolve()
        workdir.mkdir(parents=True, exist_ok=True)
        codes = [0]
        print("Fork tracker map")
        for entry in repos:
            print(f"  {entry['name']:<24} {entry['mode']:<11} {entry.get('fork', '')} <- {entry.get('upstream', '-')}")
        for entry in selected:
            mode = entry["mode"]
            if mode == "ledger":
                codes.append(check_ledger_repo(entry, root, workdir, today, args.write_refs_snapshot))
            elif mode == "workflow":
                codes.append(check_workflow_repo(entry, workdir, today))
            elif mode == "self":
                present = (root / entry["tracker_path"]).is_file()
                print(f"\n=== {entry['name']}: tracked by this repository's {entry['tracker_path']}")
                codes.append(0 if present else 1)
            elif mode == "covered-by":
                print(f"\n=== {entry['name']}: covered by the tracker of {entry['covered_by']}: {entry['reason']}")
        if args.owner:
            codes.append(check_map_completeness(repos, args.owner, args.repos_file))
        if not args.skip_images:
            for image in images:
                codes.append(check_image(image, workdir, args.image_tags_file))

    verdict = max(codes)
    print()
    print("Fork tracker coverage OK" if verdict == 0 else "Fork tracker coverage FAILED")
    raise SystemExit(verdict)


if __name__ == "__main__":
    main()
