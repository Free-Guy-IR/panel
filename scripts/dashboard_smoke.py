#!/usr/bin/env python3
import argparse
import base64
import html
import json
import os
import re
import shutil
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

EXIT_PASS = 0
EXIT_FAIL = 1
EXIT_USAGE = 2

DEFAULT_CLIENT_SOURCE = "dashboard/src/service/api/index.ts"
DEFAULT_MUTATOR_NAME = "orvalFetcher"
DEFAULT_API_PATH = "/api/admin"
DEFAULT_API_METHOD = "GET"
DEFAULT_API_STATUS = 401
DEFAULT_LOGIN_HASH = "#/login"
DEFAULT_LOGIN_SELECTOR = '[data-testid="login-form"], form input[type="password"]'

OBJECT_URL_MARKERS = ("[object", "object]", "%5bobject", "object%5d")
NOT_FOUND_MARKERS = ("page not found", "does not exist or has moved")

MODULE_BINDING_TAIL = re.compile(r"\s*(?:,|\}|as\s)")
OBJECT_KEY = re.compile(r"(?:^|[,{\s])url\s*:")
METHOD_KEY = re.compile(r"(?:^|[,{\s])method\s*:")
MINIFIED_TWO_ARG_CALL = re.compile(r"\([\w$]+\([^()]*\),\s*\{\s*\.\.\.[\w$]+\s*,\s*method\s*:")
SCRIPT_OR_STYLE_BLOCK = re.compile(r"<(script|style)\b[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL)
ANY_TAG = re.compile(r"<[^>]+>")
WHITESPACE = re.compile(r"\s+")

CHROME_COMMANDS = ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "chrome")
MACOS_CHROME_PATHS = (
    Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
    Path("/Applications/Chromium.app/Contents/MacOS/Chromium"),
)

OBJECT_LITERAL_WINDOW = 400
VISIBLE_TEXT_PREVIEW_CHARS = 300
CALL_SITE_PREVIEW_CHARS = 160
MAX_REPORTED_HITS = 5


def report(line):
    print(line, flush=True)


def line_number(source, offset):
    return source.count("\n", 0, offset) + 1


def skip_whitespace(source, index):
    while index < len(source) and source[index].isspace():
        index += 1
    return index


def skip_generic_arguments(source, index):
    angle_depth = 0
    bracket_depth = 0
    while index < len(source):
        char = source[index]
        if char == "=" and source[index : index + 2] == "=>":
            index += 2
            continue
        if char in "([{":
            bracket_depth += 1
        elif char in ")]}":
            if bracket_depth == 0:
                return index
            bracket_depth -= 1
        elif char == "<" and bracket_depth == 0:
            angle_depth += 1
        elif char == ">" and bracket_depth == 0:
            angle_depth -= 1
            if angle_depth == 0:
                return index + 1
        elif char == ";":
            return index
        index += 1
    return index


def split_call_arguments(source, open_index):
    depth = 0
    quote = None
    arguments = []
    current = []
    index = open_index
    while index < len(source):
        char = source[index]
        if quote is not None:
            if char == "\\":
                current.append(source[index : index + 2])
                index += 2
                continue
            if char == quote:
                quote = None
            current.append(char)
            index += 1
            continue
        if char in "\"'`":
            quote = char
            current.append(char)
            index += 1
            continue
        if char in "([{":
            depth += 1
            if not (depth == 1 and index == open_index):
                current.append(char)
            index += 1
            continue
        if char in ")]}":
            depth -= 1
            if depth == 0:
                arguments.append("".join(current).strip())
                return [argument for argument in arguments if argument]
            current.append(char)
            index += 1
            continue
        if char == "," and depth == 1:
            arguments.append("".join(current).strip())
            current = []
            index += 1
            continue
        current.append(char)
        index += 1
    return None


def enclosing_line(source, offset):
    start = source.rfind("\n", 0, offset) + 1
    end = source.find("\n", offset)
    return source[start : end if end != -1 else len(source)]


def is_module_binding(source, line, offset):
    if not line.startswith(("import ", "export ")):
        return False
    tail = source[offset:]
    return bool(MODULE_BINDING_TAIL.match(tail))


def find_mutator_call_sites(source, mutator_name):
    call_sites = []
    unresolved = []
    identifier = re.compile(r"\b" + re.escape(mutator_name) + r"\b")
    for match in identifier.finditer(source):
        index = skip_whitespace(source, match.end())
        if index < len(source) and source[index] == "<":
            index = skip_generic_arguments(source, index)
            index = skip_whitespace(source, index)
        line = enclosing_line(source, match.start()).lstrip()
        if index < len(source) and source[index] == "(":
            arguments = split_call_arguments(source, index)
            if arguments is None:
                unresolved.append((match.start(), line[:CALL_SITE_PREVIEW_CHARS]))
                continue
            call_sites.append((match.start(), arguments))
            continue
        lookbehind = source[max(0, match.start() - 16) : match.start()].rstrip()
        if lookbehind.endswith("typeof"):
            continue
        if is_module_binding(source, line, match.end()):
            continue
        unresolved.append((match.start(), line[:CALL_SITE_PREVIEW_CHARS]))
    return call_sites, unresolved


def classify_call_site(arguments):
    if len(arguments) != 2:
        return f"expected 2 arguments, found {len(arguments)}"
    url_argument, options_argument = arguments
    if url_argument.startswith("{"):
        return "first argument is an object literal, not a url expression"
    if OBJECT_KEY.search(url_argument):
        return "first argument carries a url: key instead of being the url itself"
    if not options_argument.startswith("{"):
        return "second argument is not an options object literal"
    if not METHOD_KEY.search(options_argument):
        return "second argument has no method: key"
    return None


def check_client_source(source_path, mutator_name, required_path, min_call_sites):
    failures = []
    if not source_path.is_file():
        return [f"generated client source {source_path} does not exist"], 0
    source = source_path.read_text(encoding="utf-8", errors="replace")
    call_sites, unresolved = find_mutator_call_sites(source, mutator_name)
    conforming = []
    violations = []
    for offset, arguments in call_sites:
        reason = classify_call_site(arguments)
        if reason is None:
            conforming.append(offset)
        else:
            violations.append((offset, reason, arguments))
    total = len(call_sites)
    report(f"client source: {source_path}")
    report(f"  {mutator_name}() call sites: {total}")
    report(f"  two-argument (url, options) call sites: {len(conforming)}")
    report(f"  non-conforming call sites: {len(violations)}")
    report(f"  unparsable {mutator_name} references: {len(unresolved)}")
    for offset, reason, arguments in violations[:MAX_REPORTED_HITS]:
        preview = WHITESPACE.sub(" ", ", ".join(arguments))[:CALL_SITE_PREVIEW_CHARS]
        report(f"  line {line_number(source, offset)}: {reason} -> {preview!r}")
    for offset, preview in unresolved[:MAX_REPORTED_HITS]:
        report(f"  line {line_number(source, offset)}: unparsable reference -> {preview!r}")
    if unresolved:
        failures.append(
            f"{len(unresolved)} {mutator_name} reference(s) could not be parsed as a call or a type reference; "
            f"the gate refuses to count a call site it cannot classify"
        )
    if total < min_call_sites:
        failures.append(f"generated client has {total} {mutator_name}() call site(s), expected at least {min_call_sites}")
    if violations:
        failures.append(
            f"{len(violations)} of {total} {mutator_name}() call site(s) do not use the two-argument "
            f"{mutator_name}(url, options) shape that dashboard/src/service/http.ts implements"
        )
    if required_path and required_path not in source:
        failures.append(f"generated client does not reference {required_path}; the smoke probe endpoint is missing")
    return failures, len(conforming)


def find_one_object_literals(source):
    hits = []
    for match in re.finditer(r"\(\s*\{", source):
        brace_index = match.end() - 1
        window = source[brace_index : brace_index + OBJECT_LITERAL_WINDOW]
        depth = 0
        end = None
        for position, char in enumerate(window):
            if char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    end = position
                    break
        if end is None:
            continue
        body = window[1:end]
        if OBJECT_KEY.search(body) and METHOD_KEY.search(body):
            hits.append(match.start())
    return hits


def check_bundle(bundle_dir, required_two_arg):
    failures = []
    index_html = bundle_dir / "index.html"
    if not index_html.is_file():
        failures.append(f"missing {index_html}")
    statics_dir = bundle_dir / "statics"
    api_chunks = sorted(statics_dir.glob("api-*.js")) if statics_dir.is_dir() else []
    if not api_chunks:
        chunk_names = sorted(path.name for path in statics_dir.glob("*.js")) if statics_dir.is_dir() else []
        failures.append(f"no statics/api-*.js chunk in {bundle_dir} (js chunks: {chunk_names})")
        return failures
    total_one_object = 0
    total_two_arg = 0
    for chunk in api_chunks:
        source = chunk.read_text(encoding="utf-8", errors="replace")
        one_object_hits = find_one_object_literals(source)
        two_arg = len(MINIFIED_TWO_ARG_CALL.findall(source))
        total_one_object += len(one_object_hits)
        total_two_arg += two_arg
        report(f"bundle chunk: {chunk.relative_to(bundle_dir)} ({chunk.stat().st_size} bytes)")
        report(f"  one-object mutator calls: {len(one_object_hits)}")
        report(f"  two-argument mutator calls: {two_arg}")
        for offset in one_object_hits[:MAX_REPORTED_HITS]:
            preview = source[max(0, offset - 40) : offset + 120]
            report(f"  offset {offset}: {preview!r}")
    if total_one_object:
        failures.append(
            f"built api chunk contains {total_one_object} one-object mutator call(s) carrying both url: and method: "
            f"keys; the generated client does not match the two-argument mutator"
        )
    if total_two_arg < required_two_arg:
        failures.append(
            f"built api chunk contains {total_two_arg} two-argument mutator call(s), expected at least {required_two_arg}"
        )
    return failures


def check_backend_log(log_path):
    if not log_path.is_file():
        return [f"backend log {log_path} does not exist"]
    lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
    offending = [line for line in lines if any(marker in line.lower() for marker in OBJECT_URL_MARKERS)]
    report(f"backend log: {log_path} ({len(lines)} lines), stringified-object request lines: {len(offending)}")
    for line in offending[:MAX_REPORTED_HITS]:
        report(f"  {line.strip()}")
    if offending:
        return [f"backend log records {len(offending)} request(s) whose path is a stringified object"]
    return []


def visible_text(dom):
    without_blocks = SCRIPT_OR_STYLE_BLOCK.sub(" ", dom)
    without_tags = ANY_TAG.sub(" ", without_blocks)
    return WHITESPACE.sub(" ", html.unescape(without_tags)).strip()


def find_chrome(explicit):
    if explicit:
        return explicit
    for command in CHROME_COMMANDS:
        found = shutil.which(command)
        if found:
            return found
    for candidate in MACOS_CHROME_PATHS:
        if candidate.is_file():
            return str(candidate)
    return None


class WebSocketError(Exception):
    pass


class WebSocketConnection:
    def __init__(self, url, connect_timeout):
        parts = urlsplit(url)
        host = parts.hostname or "127.0.0.1"
        port = parts.port or 80
        path = parts.path or "/"
        if parts.query:
            path = f"{path}?{parts.query}"
        self._socket = socket.create_connection((host, port), timeout=connect_timeout)
        self._socket.settimeout(1.0)
        self._receive_buffer = bytearray()
        self._send_lock = threading.Lock()
        self._handshake(host, port, path)

    def _handshake(self, host, port, path):
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        request = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "\r\n"
        )
        self._socket.sendall(request.encode("ascii"))
        deadline = time.monotonic() + 10
        while b"\r\n\r\n" not in self._receive_buffer:
            if time.monotonic() > deadline:
                raise WebSocketError("timed out waiting for the websocket handshake response")
            self._fill()
        header_end = self._receive_buffer.index(b"\r\n\r\n") + 4
        header = bytes(self._receive_buffer[:header_end]).decode("latin-1")
        del self._receive_buffer[:header_end]
        if "101" not in header.split("\r\n", 1)[0]:
            raise WebSocketError(f"websocket upgrade refused: {header.splitlines()[0] if header else header!r}")

    def _fill(self):
        try:
            chunk = self._socket.recv(65536)
        except TimeoutError:
            return
        if not chunk:
            raise WebSocketError("websocket closed by the peer")
        self._receive_buffer.extend(chunk)

    def _read_exactly(self, count):
        while len(self._receive_buffer) < count:
            self._fill()
        payload = bytes(self._receive_buffer[:count])
        del self._receive_buffer[:count]
        return payload

    def _send_frame(self, opcode, payload):
        header = bytearray()
        header.append(0x80 | opcode)
        length = len(payload)
        if length < 126:
            header.append(0x80 | length)
        elif length < 65536:
            header.append(0x80 | 126)
            header.extend(struct.pack("!H", length))
        else:
            header.append(0x80 | 127)
            header.extend(struct.pack("!Q", length))
        mask = os.urandom(4)
        header.extend(mask)
        masked = bytes(byte ^ mask[position % 4] for position, byte in enumerate(payload))
        with self._send_lock:
            self._socket.sendall(bytes(header) + masked)

    def send_text(self, text):
        self._send_frame(0x1, text.encode("utf-8"))

    def receive_text(self):
        message = bytearray()
        message_opcode = None
        while True:
            first, second = self._read_exactly(2)
            final = bool(first & 0x80)
            opcode = first & 0x0F
            length = second & 0x7F
            if length == 126:
                length = struct.unpack("!H", self._read_exactly(2))[0]
            elif length == 127:
                length = struct.unpack("!Q", self._read_exactly(8))[0]
            payload = self._read_exactly(length) if length else b""
            if opcode == 0x8:
                raise WebSocketError("websocket close frame received")
            if opcode == 0x9:
                self._send_frame(0xA, payload)
                continue
            if opcode == 0xA:
                continue
            if opcode in (0x1, 0x2):
                message_opcode = opcode
                message = bytearray(payload)
            else:
                message.extend(payload)
            if final:
                if message_opcode == 0x1:
                    return message.decode("utf-8", errors="replace")
                message = bytearray()
                message_opcode = None

    def close(self):
        try:
            self._socket.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self._socket.close()
        except OSError:
            pass


class CdpClient:
    def __init__(self, websocket_url, connect_timeout):
        self._connection = WebSocketConnection(websocket_url, connect_timeout)
        self._next_id = 0
        self._id_lock = threading.Lock()
        self._responses = {}
        self._responses_lock = threading.Lock()
        self._response_event = threading.Condition(self._responses_lock)
        self._event_handlers = []
        self._stopped = threading.Event()
        self._failure = None
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()

    def add_event_handler(self, handler):
        self._event_handlers.append(handler)

    def _read_loop(self):
        while not self._stopped.is_set():
            try:
                raw = self._connection.receive_text()
            except WebSocketError as error:
                self._failure = str(error)
                break
            except OSError as error:
                self._failure = str(error)
                break
            try:
                message = json.loads(raw)
            except ValueError:
                continue
            if "id" in message:
                with self._response_event:
                    self._responses[message["id"]] = message
                    self._response_event.notify_all()
                continue
            for handler in list(self._event_handlers):
                handler(message)
        with self._response_event:
            self._response_event.notify_all()

    def call(self, method, params=None, session_id=None, timeout=20):
        with self._id_lock:
            self._next_id += 1
            message_id = self._next_id
        payload = {"id": message_id, "method": method, "params": params or {}}
        if session_id:
            payload["sessionId"] = session_id
        self._connection.send_text(json.dumps(payload))
        deadline = time.monotonic() + timeout
        with self._response_event:
            while message_id not in self._responses:
                if self._failure:
                    raise WebSocketError(f"devtools connection lost: {self._failure}")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise WebSocketError(f"timed out after {timeout}s waiting for the {method} response")
                self._response_event.wait(min(remaining, 0.5))
            message = self._responses.pop(message_id)
        if "error" in message:
            raise WebSocketError(f"{method} failed: {message['error']}")
        return message.get("result", {})

    def close(self):
        self._stopped.set()
        self._connection.close()
        self._reader.join(timeout=5)


class ChromeProcess:
    def __init__(self, binary, extra_arguments):
        self.profile_dir = tempfile.mkdtemp(prefix="dashboard-smoke-")
        command = [
            binary,
            "--headless=new",
            "--disable-gpu",
            "--no-sandbox",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-extensions",
            "--disable-dev-shm-usage",
            "--disable-background-timer-throttling",
            "--disable-renderer-backgrounding",
            "--disable-backgrounding-occluded-windows",
            "--remote-allow-origins=*",
            "--remote-debugging-port=0",
            f"--user-data-dir={self.profile_dir}",
            "about:blank",
        ]
        command.extend(extra_arguments)
        self.stderr_path = Path(self.profile_dir) / "chrome-stderr.log"
        self._stderr_handle = self.stderr_path.open("wb")
        try:
            self.process = subprocess.Popen(
                command,
                stdout=subprocess.DEVNULL,
                stderr=self._stderr_handle,
                start_new_session=True,
            )
        except OSError:
            self._stderr_handle.close()
            shutil.rmtree(self.profile_dir, ignore_errors=True)
            raise
        self.process_group = self.process.pid
        self.cleanup_error = None
        self.probe_error = None

    def wait_for_devtools(self, timeout):
        port_file = Path(self.profile_dir) / "DevToolsActivePort"
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise WebSocketError(f"chrome exited early with code {self.process.returncode}: {self.stderr_tail()}")
            if port_file.is_file():
                content = port_file.read_text(encoding="utf-8", errors="replace").splitlines()
                if len(content) >= 2 and content[0].strip().isdigit():
                    return f"ws://127.0.0.1:{content[0].strip()}{content[1].strip()}"
            time.sleep(0.1)
        raise WebSocketError(f"chrome never published a devtools endpoint within {timeout}s: {self.stderr_tail()}")

    def stderr_tail(self, limit=600):
        try:
            self._stderr_handle.flush()
            return self.stderr_path.read_text(encoding="utf-8", errors="replace")[-limit:]
        except OSError:
            return ""

    def terminate(self):
        self._signal_group(signal.SIGTERM)
        if self.process.poll() is None:
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                pass
        deadline = time.monotonic() + 10
        while self._group_is_alive() and time.monotonic() < deadline:
            time.sleep(0.2)
        if self._group_is_alive():
            self._signal_group(signal.SIGKILL)
            deadline = time.monotonic() + 10
            while self._group_is_alive() and time.monotonic() < deadline:
                time.sleep(0.2)
        if self.process.poll() is None:
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
        if self._group_is_alive():
            detail = f" ({self.probe_error})" if self.probe_error else ""
            self.cleanup_error = f"chrome process group {self.process_group} was still alive after SIGKILL{detail}"
            report(self.cleanup_error)
        try:
            self._stderr_handle.close()
        except OSError:
            pass
        shutil.rmtree(self.profile_dir, ignore_errors=True)

    def _group_is_alive(self):
        try:
            os.killpg(self.process_group, 0)
        except ProcessLookupError:
            self.probe_error = None
            return False
        except OSError as error:
            self.probe_error = str(error)
            return True
        self.probe_error = None
        return True

    def _signal_group(self, which):
        try:
            os.killpg(self.process_group, which)
            return
        except ProcessLookupError:
            return
        except OSError as error:
            report(f"could not signal chrome process group {self.process_group}: {error}")
        try:
            if which == signal.SIGKILL:
                self.process.kill()
            else:
                self.process.terminate()
        except OSError as error:
            report(f"could not signal the chrome leader process: {error}")


class NetworkRecorder:
    def __init__(self):
        self.entries = []
        self.active = {}
        self.page_errors = []
        self.console_errors = []
        self.lock = threading.Lock()

    def handle(self, message):
        method = message.get("method", "")
        params = message.get("params", {})
        with self.lock:
            if method == "Network.requestWillBeSent":
                request_id = params.get("requestId")
                redirect = params.get("redirectResponse")
                previous = self.active.get(request_id)
                if redirect is not None and previous is not None:
                    previous["status"] = redirect.get("status")
                    previous["redirected"] = True
                request = params.get("request", {})
                entry = {
                    "requestId": request_id,
                    "url": request.get("url", ""),
                    "method": request.get("method", ""),
                    "resourceType": params.get("type", ""),
                    "status": None,
                    "error": None,
                    "redirected": False,
                }
                self.entries.append(entry)
                self.active[request_id] = entry
            elif method == "Network.responseReceived":
                entry = self.active.get(params.get("requestId"))
                if entry is not None:
                    entry["status"] = params.get("response", {}).get("status")
            elif method == "Network.loadingFailed":
                entry = self.active.get(params.get("requestId"))
                if entry is not None:
                    entry["error"] = params.get("errorText")
            elif method == "Runtime.exceptionThrown":
                details = params.get("exceptionDetails", {})
                text = details.get("exception", {}).get("description") or details.get("text") or "uncaught error"
                self.page_errors.append(WHITESPACE.sub(" ", str(text))[:300])
            elif method == "Log.entryAdded":
                entry = params.get("entry", {})
                if entry.get("level") == "error":
                    self.console_errors.append(WHITESPACE.sub(" ", str(entry.get("text", "")))[:300])

    def snapshot(self):
        with self.lock:
            return [dict(entry) for entry in self.entries]

    def object_url_requests(self):
        return [entry for entry in self.snapshot() if url_is_stringified_object(entry["url"])]


def url_is_stringified_object(url):
    lowered = url.lower()
    return any(marker in lowered for marker in OBJECT_URL_MARKERS)


def build_ready_expression(login_hash, login_selector):
    hash_literal = json.dumps(login_hash)
    selector_literal = json.dumps(login_selector)
    return (
        f"(function(){{try{{return location.hash.indexOf({hash_literal})===0"
        f"&&document.querySelector({selector_literal})!==null;}}catch(e){{return false;}}}})()"
    )


class BrowserRun:
    def __init__(self):
        self.ready = False
        self.timed_out = False
        self.final_ready = None
        self.capture_failed = False
        self.dom = ""
        self.location_hash = ""
        self.requests = []
        self.page_errors = []
        self.console_errors = []
        self.early_violation = False
        self.chrome_stderr = ""
        self.cleanup_error = None


def drive_browser(chrome_binary, url, ready_expression, ready_timeout, startup_timeout):
    recorder = NetworkRecorder()
    result = BrowserRun()
    chrome = ChromeProcess(chrome_binary, [])
    client = None
    try:
        websocket_url = chrome.wait_for_devtools(startup_timeout)
        client = CdpClient(websocket_url, connect_timeout=startup_timeout)
        client.add_event_handler(recorder.handle)
        target = client.call("Target.createTarget", {"url": "about:blank"})
        attached = client.call("Target.attachToTarget", {"targetId": target["targetId"], "flatten": True})
        session_id = attached["sessionId"]
        for domain in ("Network", "Page", "Runtime", "Log"):
            client.call(f"{domain}.enable", {}, session_id=session_id)
        client.call("Network.setCacheDisabled", {"cacheDisabled": True}, session_id=session_id)
        client.call("Page.navigate", {"url": url}, session_id=session_id)
        deadline = time.monotonic() + ready_timeout
        while time.monotonic() < deadline:
            if recorder.object_url_requests():
                result.early_violation = True
                break
            try:
                evaluated = client.call(
                    "Runtime.evaluate",
                    {"expression": ready_expression, "returnByValue": True, "awaitPromise": False},
                    session_id=session_id,
                    timeout=10,
                )
            except WebSocketError:
                time.sleep(0.25)
                continue
            if evaluated.get("result", {}).get("value") is True:
                result.ready = True
                break
            time.sleep(0.25)
        else:
            result.timed_out = True
        time.sleep(1.0)
        final_ready_ok, final_ready_value = evaluate_value(client, session_id, ready_expression)
        result.final_ready = final_ready_value if final_ready_ok else None
        dom_ok, dom_value = evaluate_value(client, session_id, "document.documentElement.outerHTML")
        hash_ok, hash_value = evaluate_value(client, session_id, "location.hash")
        result.dom = dom_value if isinstance(dom_value, str) else ""
        result.location_hash = hash_value if isinstance(hash_value, str) else ""
        result.capture_failed = not (final_ready_ok and dom_ok and hash_ok)
    finally:
        if client is not None:
            try:
                client.close()
            except OSError:
                pass
        result.chrome_stderr = chrome.stderr_tail()
        chrome.terminate()
        result.cleanup_error = chrome.cleanup_error
    result.requests = recorder.snapshot()
    result.page_errors = list(recorder.page_errors)
    result.console_errors = list(recorder.console_errors)
    return result


def evaluate_value(client, session_id, expression):
    try:
        evaluated = client.call(
            "Runtime.evaluate",
            {"expression": expression, "returnByValue": True, "awaitPromise": False},
            session_id=session_id,
            timeout=20,
        )
    except WebSocketError as error:
        report(f"could not evaluate {expression[:80]!r}: {error}")
        return False, None
    if evaluated.get("exceptionDetails"):
        report(f"evaluating {expression[:80]!r} threw in the page")
        return False, None
    return True, evaluated.get("result", {}).get("value")


def summarise_requests(requests, api_path):
    report(f"captured requests: {len(requests)}")
    for entry in requests:
        path = urlsplit(entry["url"]).path
        if path == api_path or url_is_stringified_object(entry["url"]) or entry["error"]:
            report(f"  {entry['method']} {entry['url']} -> status={entry['status']} error={entry['error']}")


def assert_behaviour(run, api_path, api_method, expected_status, login_hash):
    failures = []
    offending = [entry for entry in run.requests if url_is_stringified_object(entry["url"])]
    api_requests = [
        entry
        for entry in run.requests
        if urlsplit(entry["url"]).path == api_path and entry["method"].upper() == api_method.upper()
    ]
    report(f"requests to {api_method} {api_path}: {len(api_requests)}")
    report(f"requests with a stringified-object url: {len(offending)}")
    report(f"uncaught page errors: {len(run.page_errors)}")
    report(f"console errors: {len(run.console_errors)}")
    report(f"location.hash after load: {run.location_hash or '-'}")
    report(f"login-ready during the wait: {run.ready}; at final capture: {run.final_ready}")
    for entry in offending[:MAX_REPORTED_HITS]:
        report(f"  stringified-object request: {entry['method']} {entry['url']} -> status={entry['status']}")
    for message in run.page_errors[:MAX_REPORTED_HITS]:
        report(f"  page error: {message}")
    for message in run.console_errors[:MAX_REPORTED_HITS]:
        report(f"  console error: {message}")
    if offending:
        failures.append(
            f"{len(offending)} request(s) were issued with a stringified object as the url "
            f"(first: {offending[0]['url']}); the generated client is not calling the mutator correctly"
        )
    elif not run.ready:
        if run.timed_out:
            failures.append("the dashboard never reached its login-ready state before the timeout")
        else:
            failures.append("the dashboard never reached its login-ready state")
    elif run.final_ready is not True:
        failures.append(
            f"the dashboard reached its login-ready state but no longer satisfied it at final capture "
            f"(final_ready={run.final_ready})"
        )
    if run.capture_failed:
        failures.append("the browser could not be queried for the final page state; the run proves nothing")
    if run.cleanup_error:
        failures.append(run.cleanup_error)
    if not api_requests:
        failures.append(f"the dashboard never issued a {api_method} {api_path} request; the admin probe never ran")
    else:
        statuses = [entry["status"] for entry in api_requests]
        if expected_status not in statuses:
            failures.append(
                f"the unauthenticated {api_method} {api_path} request returned {statuses}, expected {expected_status}"
            )
    if run.page_errors:
        failures.append(f"the page raised {len(run.page_errors)} uncaught error(s): {run.page_errors[0]}")
    if run.location_hash and not run.location_hash.startswith(login_hash):
        failures.append(f"the router settled on {run.location_hash!r} instead of {login_hash!r}")
    return failures


def inspect_dom(dom):
    failures = []
    text = visible_text(dom).lower()
    hits = [marker for marker in NOT_FOUND_MARKERS if marker in text]
    report(f"dom characters: {len(dom)}")
    report(f"not-found markers: {hits if hits else 'none'}")
    report(f"visible text: {visible_text(dom)[:VISIBLE_TEXT_PREVIEW_CHARS]!r}")
    if not dom.strip():
        failures.append("the browser returned an empty DOM")
    if hits:
        failures.append(f"the rendered DOM shows the not-found route: {hits}")
    return failures


def run_static_mode(args):
    failures = []
    conforming_call_sites = 0
    if args.client_source:
        source_failures, conforming_call_sites = check_client_source(
            Path(args.client_source),
            args.mutator_name,
            args.api_path,
            args.min_call_sites,
        )
        failures.extend(source_failures)
    if args.image_bundle:
        bundle_dir = Path(args.image_bundle)
        if not bundle_dir.is_dir():
            report(f"bundle directory {bundle_dir} does not exist")
            return EXIT_USAGE
        required_two_arg = args.min_bundle_calls
        if conforming_call_sites:
            required_two_arg = max(required_two_arg, (conforming_call_sites * 4) // 5)
        report(f"bundle requires at least {required_two_arg} two-argument mutator call(s)")
        failures.extend(check_bundle(bundle_dir, required_two_arg))
    if args.backend_log:
        failures.extend(check_backend_log(Path(args.backend_log)))
    return finish(failures)


def run_render_mode(args):
    chrome_binary = find_chrome(args.chrome)
    if not chrome_binary:
        report("no headless Chrome found (google-chrome, chromium or the macOS app); pass --chrome <path>")
        return EXIT_USAGE
    if not args.backend_log:
        report("--backend-log is required in render mode")
        return EXIT_USAGE
    ready_expression = args.ready_expression or build_ready_expression(args.login_hash, args.login_selector)
    report(f"chrome: {chrome_binary}")
    report(f"url: {args.url}")
    report(f"ready expression: {ready_expression}")
    run = None
    for attempt in range(1, args.attempts + 1):
        report(f"browser attempt {attempt}/{args.attempts}")
        try:
            run = drive_browser(
                chrome_binary,
                args.url,
                ready_expression,
                args.ready_timeout,
                args.startup_timeout,
            )
        except WebSocketError as error:
            report(f"browser attempt {attempt} failed: {error}")
            if attempt == args.attempts:
                return finish([f"could not drive headless Chrome: {error}"])
            continue
        offending = [entry for entry in run.requests if url_is_stringified_object(entry["url"])]
        if run.ready or offending or attempt == args.attempts:
            break
        report("the dashboard was not ready and nothing was obviously broken; retrying once")
    if run is None:
        return finish(["headless Chrome produced no run"])
    if run.chrome_stderr.strip():
        report(f"chrome stderr tail: {run.chrome_stderr.strip()[-400:]!r}")
    if args.dom_out:
        Path(args.dom_out).write_text(run.dom, encoding="utf-8")
        report(f"dom written to {args.dom_out}")
    if args.request_log_out:
        payload = {
            "url": args.url,
            "ready": run.ready,
            "timedOut": run.timed_out,
            "locationHash": run.location_hash,
            "pageErrors": run.page_errors,
            "consoleErrors": run.console_errors,
            "requests": run.requests,
        }
        Path(args.request_log_out).write_text(json.dumps(payload, indent=2), encoding="utf-8")
        report(f"request log written to {args.request_log_out}")
    summarise_requests(run.requests, args.api_path)
    failures = assert_behaviour(run, args.api_path, args.api_method, args.api_status, args.login_hash)
    failures.extend(inspect_dom(run.dom))
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
    parser = argparse.ArgumentParser(
        description=(
            "Dashboard smoke gate. Render mode drives the built dashboard in headless Chrome over the DevTools "
            "protocol and asserts on the requests the generated API client actually issues. Static mode checks the "
            "generated client source and the built api chunk for the two-argument mutator contract."
        )
    )
    parser.add_argument("--client-source", metavar="FILE", help=f"generated orval client (e.g. {DEFAULT_CLIENT_SOURCE})")
    parser.add_argument("--image-bundle", metavar="DIR", help="built dashboard directory containing index.html and statics/")
    parser.add_argument("--url", metavar="URL", help="dashboard URL to drive with headless Chrome")
    parser.add_argument("--backend-log", metavar="FILE", help="backend log that must not record stringified-object paths")
    parser.add_argument("--chrome", metavar="PATH", help="Chrome binary (default: auto-detect)")
    parser.add_argument("--mutator-name", default=DEFAULT_MUTATOR_NAME, help="mutator function name in the generated client")
    parser.add_argument("--api-path", default=DEFAULT_API_PATH, help="admin endpoint the dashboard must call on load")
    parser.add_argument("--api-method", default=DEFAULT_API_METHOD, help="HTTP method expected on the admin endpoint")
    parser.add_argument("--api-status", type=int, default=DEFAULT_API_STATUS, help="status the unauthenticated admin call must return")
    parser.add_argument("--login-hash", default=DEFAULT_LOGIN_HASH, help="hash route the router must settle on when unauthenticated")
    parser.add_argument("--login-selector", default=DEFAULT_LOGIN_SELECTOR, help="CSS selector proving the login form rendered")
    parser.add_argument("--ready-expression", metavar="JS", help="override the readiness expression entirely")
    parser.add_argument("--ready-timeout", type=int, default=45, help="seconds to wait for the readiness expression")
    parser.add_argument("--startup-timeout", type=int, default=30, help="seconds to wait for the Chrome devtools endpoint")
    parser.add_argument("--attempts", type=int, default=2, help="browser attempts before a readiness timeout is fatal")
    parser.add_argument("--min-call-sites", type=int, default=50, help="minimum mutator call sites the generated client must contain")
    parser.add_argument("--min-bundle-calls", type=int, default=20, help="minimum two-argument mutator calls in the built api chunk")
    parser.add_argument("--dom-out", metavar="FILE", help="write the rendered DOM to this file")
    parser.add_argument("--request-log-out", metavar="FILE", help="write the captured request log to this file")
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.url and (args.client_source or args.image_bundle):
        parser.error("--url cannot be combined with --client-source or --image-bundle")
    if not args.url and not args.client_source and not args.image_bundle:
        parser.error("pass --url for render mode, or --client-source and/or --image-bundle for static mode")
    if args.url:
        return run_render_mode(args)
    return run_static_mode(args)


if __name__ == "__main__":
    sys.exit(main())
