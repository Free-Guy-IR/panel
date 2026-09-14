#!/usr/bin/env python3
import argparse
import html
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ONE_OBJECT_MUTATOR_CALL = re.compile(r"\(\{url:")
TWO_ARG_MUTATOR_CALL = re.compile(r"\([\w$]+\([^()]*\),\{\.\.\.[\w$]+,method:")
PASSWORD_INPUT = re.compile(r"<input\b[^>]*\btype=\"password\"", re.IGNORECASE)
TITLE_TAG = re.compile(r"<title>(.*?)</title>", re.IGNORECASE | re.DOTALL)
SCRIPT_OR_STYLE_BLOCK = re.compile(r"<(script|style)\b[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL)
ANY_TAG = re.compile(r"<[^>]+>")
WHITESPACE = re.compile(r"\s+")

NOT_FOUND_MARKERS = ("page not found", "does not exist or has moved")
OBJECT_OBJECT_MARKER = "object Object"
VISIBLE_TEXT_PREVIEW_CHARS = 300

CHROME_COMMANDS = ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser")
MACOS_CHROME = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")

EXIT_PASS = 0
EXIT_FAIL = 1
EXIT_USAGE = 2


def report(line):
    print(line, flush=True)


def check_bundle(bundle_dir):
    failures = []
    index_html = bundle_dir / "index.html"
    if not index_html.is_file():
        failures.append(f"missing {index_html}")
    api_chunks = sorted((bundle_dir / "statics").glob("api-*.js"))
    if not api_chunks:
        chunk_names = sorted(p.name for p in (bundle_dir / "statics").glob("*.js"))
        failures.append(f"no statics/api-*.js chunk in {bundle_dir} (js chunks: {chunk_names})")
    total_one_object = 0
    total_two_arg = 0
    for chunk in api_chunks:
        source = chunk.read_text(encoding="utf-8", errors="replace")
        one_object = len(ONE_OBJECT_MUTATOR_CALL.findall(source))
        two_arg = len(TWO_ARG_MUTATOR_CALL.findall(source))
        total_one_object += one_object
        total_two_arg += two_arg
        report(f"{chunk.relative_to(bundle_dir)}: {chunk.stat().st_size} bytes, one-object mutator calls={one_object}, two-arg mutator calls={two_arg}")
        for match in ONE_OBJECT_MUTATOR_CALL.finditer(source):
            start = max(0, match.start() - 40)
            report(f"  one-object call at offset {match.start()}: {source[start:match.start() + 80]!r}")
            break
    if api_chunks and total_one_object != 0:
        failures.append(f"api chunk still contains {total_one_object} one-object mutator call(s) ({{url: ...}}); the generated client does not match the two-arg orvalFetcher(url, options) mutator")
    if api_chunks and total_two_arg == 0:
        failures.append("api chunk contains no two-arg mutator call (helper(url), {...options, method: ...})")
    report(f"bundle summary: one-object={total_one_object} two-arg={total_two_arg}")
    return failures


def find_chrome(explicit):
    if explicit:
        return explicit
    for command in CHROME_COMMANDS:
        found = shutil.which(command)
        if found:
            return found
    if MACOS_CHROME.is_file():
        return str(MACOS_CHROME)
    return None


def dump_dom(chrome, url, timeout_seconds, virtual_time_budget_ms):
    profile_dir = tempfile.mkdtemp(prefix="dashboard-smoke-")
    command = [
        chrome,
        "--headless=new",
        "--disable-gpu",
        "--no-sandbox",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-extensions",
        "--disable-dev-shm-usage",
        f"--user-data-dir={profile_dir}",
        f"--virtual-time-budget={virtual_time_budget_ms}",
        "--dump-dom",
        url,
    ]
    timed_out = False
    try:
        completed = subprocess.run(command, capture_output=True, timeout=timeout_seconds, check=False)
        raw_dom = completed.stdout
        stderr = completed.stderr
    except subprocess.TimeoutExpired as expired:
        timed_out = True
        raw_dom = expired.stdout or b""
        stderr = expired.stderr or b""
    finally:
        shutil.rmtree(profile_dir, ignore_errors=True)
    dom = raw_dom.decode("utf-8", errors="replace")
    if timed_out:
        report(f"chrome did not exit within {timeout_seconds}s; using the partial DOM ({len(dom)} chars)")
    if not dom.strip():
        report(f"chrome produced no DOM; stderr tail: {stderr.decode('utf-8', errors='replace')[-800:]!r}")
    return dom


def visible_text(dom):
    without_blocks = SCRIPT_OR_STYLE_BLOCK.sub(" ", dom)
    without_tags = ANY_TAG.sub(" ", without_blocks)
    return WHITESPACE.sub(" ", html.unescape(without_tags)).strip()


def inspect_dom(dom):
    failures = []
    title_match = TITLE_TAG.search(dom)
    title = WHITESPACE.sub(" ", title_match.group(1)).strip() if title_match else ""
    text = visible_text(dom)
    lowered_text = text.lower()
    has_password_input = PASSWORD_INPUT.search(dom) is not None
    not_found_hits = [marker for marker in NOT_FOUND_MARKERS if marker in lowered_text]
    report(f"dom chars: {len(dom)}")
    report(f"title: {title or '-'}")
    report(f"password input: {'yes' if has_password_input else 'no'}")
    report(f"not-found markers: {not_found_hits if not_found_hits else 'none'}")
    report(f"visible text: {text[:VISIBLE_TEXT_PREVIEW_CHARS]!r}")
    if not has_password_input:
        failures.append('rendered DOM has no <input type="password">; the login form did not render')
    if not_found_hits:
        failures.append(f"rendered DOM shows the not-found route: {not_found_hits}")
    return failures


def check_backend_log(log_path):
    failures = []
    if not log_path.is_file():
        return [f"backend log {log_path} does not exist"]
    lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
    offending = [line for line in lines if OBJECT_OBJECT_MARKER in line]
    report(f"backend log: {len(lines)} lines, '{OBJECT_OBJECT_MARKER}' lines={len(offending)}")
    for line in offending[:5]:
        report(f"  {line.strip()}")
    if offending:
        failures.append(f"backend log contains {len(offending)} request(s) with '{OBJECT_OBJECT_MARKER}' in the path")
    return failures


def run_render_mode(args):
    chrome = find_chrome(args.chrome)
    if not chrome:
        report("no headless Chrome found (google-chrome, chromium or the macOS Google Chrome app); pass --chrome <path>")
        return EXIT_USAGE
    report(f"chrome: {chrome}")
    report(f"url: {args.url}")
    dom = dump_dom(chrome, args.url, args.timeout, args.virtual_time_budget)
    if args.dom_out:
        Path(args.dom_out).write_text(dom, encoding="utf-8")
        report(f"dom written to {args.dom_out}")
    failures = inspect_dom(dom)
    if args.backend_log:
        failures.extend(check_backend_log(Path(args.backend_log)))
    return finish(failures)


def run_bundle_mode(args):
    bundle_dir = Path(args.image_bundle)
    if not bundle_dir.is_dir():
        report(f"bundle directory {bundle_dir} does not exist")
        return EXIT_USAGE
    failures = check_bundle(bundle_dir)
    if args.backend_log:
        failures.extend(check_backend_log(Path(args.backend_log)))
    return finish(failures)


def finish(failures):
    if failures:
        for failure in failures:
            report(f"FAIL: {failure}")
        return EXIT_FAIL
    report("PASS")
    return EXIT_PASS


def build_parser():
    parser = argparse.ArgumentParser(description="Dashboard smoke gate: bundle mode checks the built api chunk, render mode checks a headless-Chrome render of the login page.")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--image-bundle", metavar="DIR", help="built dashboard directory containing index.html and statics/")
    mode.add_argument("--url", metavar="URL", help="dashboard URL to render with headless Chrome")
    parser.add_argument("--backend-log", metavar="FILE", help="backend log that must not contain 'object Object'")
    parser.add_argument("--chrome", metavar="PATH", help="Chrome binary (default: auto-detect)")
    parser.add_argument("--timeout", type=int, default=90, help="seconds to wait for Chrome before using the partial DOM")
    parser.add_argument("--virtual-time-budget", type=int, default=15000, help="Chrome --virtual-time-budget in milliseconds")
    parser.add_argument("--dom-out", metavar="FILE", help="write the rendered DOM to this file")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    if args.image_bundle:
        return run_bundle_mode(args)
    return run_render_mode(args)


if __name__ == "__main__":
    sys.exit(main())
