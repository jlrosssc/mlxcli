"""Shared logic for mlxcli and mlxgui.

Both tools talk to the same local model servers (omlx, turbofieldfare-qwen)
and need the same answers to "is this an agentic
request", "what does this tool call actually do", and "which files count as
reviewable code". Keeping that logic in one place means a fix made here
applies to both interfaces at once, instead of the two independently
maintained copies drifting apart and re-accumulating the same bugs.

Constants and pure functions only — no Tkinter, no terminal I/O. Each caller
keeps its own approval UI (a keypress prompt for mlxcli, a modal dialog for
mlxgui) since those are fundamentally different interaction models.
"""
import ast
import contextlib
import copy
import fcntl
import hashlib
import importlib.util
import json
import logging
import os
import pathlib
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime
from logging.handlers import RotatingFileHandler

DEFAULT_DIR_CONFIG_PATH = pathlib.Path.home() / ".omlx" / "default_dir.txt"
DEFAULT_DIR_FALLBACK = pathlib.Path.home() / "LocalAI"

HOST_ALIASES_PATH = pathlib.Path.home() / ".omlx" / "host_aliases.json"
_HOST_ALIASES_CACHE = None


def load_host_aliases():
    """Non-secret connection metadata for named hosts (dad/hross/unity/...),
    keyed by lowercase alias. Actual credentials live in the macOS Keychain,
    never in this file — see keychain_get() below. Cached after first read
    since it's read on every tool call that might use an alias. Shared by
    mlxcli and mlxgui so both resolve the same aliases the same way."""
    global _HOST_ALIASES_CACHE
    if _HOST_ALIASES_CACHE is not None:
        return _HOST_ALIASES_CACHE
    try:
        _HOST_ALIASES_CACHE = json.loads(HOST_ALIASES_PATH.read_text())
    except Exception:
        _HOST_ALIASES_CACHE = {}
    return _HOST_ALIASES_CACHE


def keychain_get(account, service):
    """Fetch a secret from the macOS Keychain. Returns None (never raises)
    if the entry doesn't exist or `security` isn't available — callers fall
    back to their normal interactive prompt in that case, so a missing
    Keychain entry degrades gracefully instead of hard-failing."""
    try:
        p = subprocess.run(
            ["security", "find-generic-password", "-a", account, "-s", service, "-w"],
            capture_output=True, text=True, timeout=10)
        if p.returncode != 0:
            return None
        return p.stdout.rstrip("\n")
    except Exception:
        return None


def pla_admin_run(host, password, action):
    """Zyxel PLA6456 powerline adapter (web page only: no ssh, no shell). action = status | reboot.
    status: uptime, firmware, G.hn link rates. reboot: presses the page's Hardware Reset (restarts the powerline modem, NOT a
    factory reset; the web page stays up, the link drops for about a minute and re-forms). The adapter allows ONE logged-in client,
    so this always logs out; if someone else is logged in it retries briefly. Returns text for the model."""
    import http.cookiejar, html as _html
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    fetch = lambda path, data=None: op.open(f"http://{host}{path}", data=data, timeout=10).read().decode("utf-8", "replace")
    title = lambda pg: (re.search(r"<title>([^<]*)", pg) or [0, ""])[1]
    text_of = lambda pg: re.sub(r"\s+", " ", _html.unescape(re.sub(r"<[^>]+>", " ", re.sub(r"(?is)<script.*?</script>|<style.*?</style>|<!--.*?-->", " ", pg))))
    login = urllib.parse.urlencode({".CSRFTOKEN": "", ".REDIRECT": "/", ".PASSWORD": password}).encode()
    for attempt in range(3):
        try:
            page = fetch("/", login)
        except Exception as exc:
            return f"Error: adapter {host} not reachable ({exc.__class__.__name__})."
        if "405" not in title(page):
            break
        time.sleep(8)
    else:
        return f"Error: adapter {host} is busy (another client is logged in to its web page); try again shortly."
    if "Authentication" in title(page):
        return f"Error: login to {host} failed (wrong stored password)."
    try:
        dev = text_of(fetch("/device.html"))
        up = re.search(r"System uptime (\d+) days?, (\d+)h (\d+)m (\d+)s", dev)
        uptime = f"{up.group(1)}d {up.group(2)}h {up.group(3)}m" if up else "unknown"
        fw = (re.search(r"FW version\s*(\S+)", dev) or [0, "?"])[1]
        ghn = fetch("/ghn.html")
        def jsvar(name):
            m = re.search(r"var\s+%s\s*=\s*(?:new Array\(([^)]*)\)|['\"]([^'\"]*)['\"])" % name, ghn)
            if not m: return None
            return [x.strip().strip("'\"") for x in m.group(1).split(",")] if m.group(1) is not None else m.group(2)
        mv = jsvar("pcmastr")   # a comma-separated STRING of MACs, unlike pcptx/pcprx which are arrays
        macs = [m.strip() for m in (mv.split(",") if isinstance(mv, str) else (mv or [])) if m.strip()]
        tx, rx = jsvar("pcptx") or [], jsvar("pcprx") or []
        me = (jsvar("mymac") or "").lower()
        peers = [f"{m.lower()} TX {tx[i] if i < len(tx) else '?'} / RX {rx[i] if i < len(rx) else '?'} Mbps" for i, m in enumerate(macs) if i > 0 and m.lower() != me and m != "00:00:00:00:00:00"]
        summary = f"Zyxel PLA6456 {host}: uptime {uptime}, firmware {fw}, powerline peers: " + ("; ".join(peers) or "none seen")
        if action != "reboot":
            return summary
        fetch("/advanced.html", urllib.parse.urlencode({"CSRFTOKEN": "", "REDIRECT": "reset.html", "SYSTEM.GENERAL.HW_RESET": "1"}).encode())
        return summary + "\nHardware reset sent (powerline modem restarts; the link re-forms in about a minute). Check status again after ~90 s: uptime should be under 2 minutes."
    except Exception as exc:
        if action == "reboot" and exc.__class__.__name__ in ("RemoteDisconnected", "ConnectionResetError"):
            return "Hardware reset sent (the adapter closed the connection as it restarted, which is normal)."
        return f"Error talking to adapter {host}: {exc.__class__.__name__}: {exc}"
    finally:
        try:
            fetch("/top_nav.html", urllib.parse.urlencode({"CSRFTOKEN": "", "REDIRECT": "/", "LOG_OUT_OK": "Log Out"}).encode())
        except Exception:
            pass


def request(url, key, path, payload=None):
    # Real browser UA: some targets (e.g. Cloudflare-fronted Home Assistant
    # instances hit via ha_api) block urllib's default "Python-urllib/x.y"
    # UA outright (HTTP 403, Cloudflare error 1010) even with a valid token.
    headers = {"Content-Type": "application/json",
               "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                              "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    return urllib.request.Request(
        url + path,
        data=json.dumps(payload).encode() if payload is not None else None,
        headers=headers)


def api(url, key, path, payload=None):
    with urllib.request.urlopen(request(url, key, path, payload), timeout=900) as r:
        return json.load(r)


def rag_remote_config():
    """Return optional LAN RAG configuration used by both clients.

    RAG_URL is intentionally separate from OMLX_URL: it points at the document
    repository, not the model server. RAG_COLLECTION scopes retrieval to one
    topic while allowing multiple topics to share the same RAG deployment.
    """
    url = os.environ.get("RAG_URL", "").strip().rstrip("/")
    key = os.environ.get("RAG_API_KEY", "").strip()
    collection = os.environ.get("RAG_COLLECTION", "").strip()
    return url, key, collection


def rag_remote_request(method, path, key, payload=None, body=None, content_type=None, timeout=60):
    data = body
    headers = {"Accept": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    elif content_type:
        headers["Content-Type"] = content_type
    request = urllib.request.Request(path, data=data, headers=headers, method=method)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        raw = response.read()
    return json.loads(raw.decode("utf-8")) if raw else {}


def rag_remote_search(query, url=None, key=None, collection=None, limit=4):
    configured_url, configured_key, configured_collection = rag_remote_config()
    url = (url or configured_url).rstrip("/")
    key = key if key is not None else configured_key
    collection = collection if collection is not None else configured_collection
    params = {"query": query, "limit": str(limit)}
    if collection:
        params["collection"] = collection
    endpoint = f"{url}/search?{urllib.parse.urlencode(params)}"
    return rag_remote_request("GET", endpoint, key)


def rag_remote_list(url=None, key=None, collection=None, limit=100):
    configured_url, configured_key, configured_collection = rag_remote_config()
    url = (url or configured_url).rstrip("/")
    key = key if key is not None else configured_key
    collection = collection if collection is not None else configured_collection
    params = {"limit": str(limit)}
    if collection:
        params["collection"] = collection
    endpoint = f"{url}/documents?{urllib.parse.urlencode(params)}"
    return rag_remote_request("GET", endpoint, key)


def rag_remote_get_document(document_id, url=None, key=None):
    """Fetch a document's full, unchunked stored content (not search snippets) —
    needed for exact extraction (e.g. a precise Bible verse range) rather than
    keyword-similarity search."""
    configured_url, configured_key, _ = rag_remote_config()
    url = (url or configured_url).rstrip("/")
    key = key if key is not None else configured_key
    return rag_remote_request("GET", f"{url}/documents/{document_id}", key)


def esv_passage(reference, api_key=None):
    """On-demand ESV passage lookup via api.esv.org — fetched fresh each call,
    never cached or stored, per Crossway's API terms (personal/non-commercial
    use). Returns the passage text, or raises on a missing/invalid key or a
    reference the API can't parse."""
    key = api_key or os.environ.get("ESV_API_KEY", "").strip()
    if not key:
        raise RuntimeError("ESV_API_KEY is not set")
    params = urllib.parse.urlencode({
        "q": reference,
        "include-headings": "true",
        "include-footnotes": "false",
        "include-verse-numbers": "true",
        "include-short-copyright": "true",
    })
    request = urllib.request.Request(
        f"https://api.esv.org/v3/passage/text/?{params}",
        headers={"Authorization": f"Token {key}"})
    with urllib.request.urlopen(request, timeout=20) as response:
        data = json.loads(response.read().decode("utf-8"))
    passages = data.get("passages") or []
    if not passages:
        raise RuntimeError(f"ESV API returned no passage for {reference!r} — check the reference")
    return data.get("canonical", reference), passages[0]


_WEB_SEARCH_RESULT_RE = re.compile(
    r'<a[^>]+class="result__a"[^>]+href="(?P<url>[^"]+)"[^>]*>(?P<title>.*?)</a>.*?'
    r'<a[^>]+class="result__snippet"[^>]*>(?P<snippet>.*?)</a>',
    re.DOTALL)


def _strip_html_tags(fragment):
    return re.sub(r"<[^>]+>", "", fragment).replace("&#x27;", "'").replace("&amp;", "&").strip()


def web_search(query, max_results=5):
    """General internet search via DuckDuckGo's no-JS HTML endpoint -- chosen
    specifically because it needs no API key/signup, matching this codebase's
    existing "no credentials required" pattern for everything else. Returns a
    list of {title, url, snippet} dicts, or raises on a network failure (the
    caller is responsible for approval-gating this before it's ever invoked,
    since unlike every other tool here it reaches beyond the user's own
    machines and services)."""
    max_results = max(1, min(int(max_results or 5), 10))
    params = urllib.parse.urlencode({"q": query})
    headers = {
        "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36"),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }
    req = urllib.request.Request(f"https://html.duckduckgo.com/html/?{params}", headers=headers)
    with urllib.request.urlopen(req, timeout=20) as resp:
        body = resp.read().decode("utf-8", errors="replace")
    results = []
    for match in _WEB_SEARCH_RESULT_RE.finditer(body):
        url = match.group("url")
        if url.startswith("//duckduckgo.com/l/?"):
            # DDG's HTML endpoint wraps result links in its own redirector
            # (//duckduckgo.com/l/?uddg=<encoded-real-url>&...) -- unwrap it
            # so the model gets the real destination, not a tracking link.
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
            url = qs.get("uddg", [url])[0]
        results.append({
            "title": _strip_html_tags(match.group("title")),
            "url": url,
            "snippet": _strip_html_tags(match.group("snippet")),
        })
        if len(results) >= max_results:
            break
    return results


def rag_remote_collections(url=None, key=None):
    """List every collection on the RAG server, with each one's document count."""
    configured_url, configured_key, _ = rag_remote_config()
    url = (url or configured_url).rstrip("/")
    key = key if key is not None else configured_key
    return rag_remote_request("GET", f"{url}/collections", key)


def _multipart_upload(path, title, metadata):
    boundary = uuid.uuid4().hex
    filename = pathlib.Path(path).name
    payload = pathlib.Path(path).read_bytes()
    parts = []
    def field(name, value):
        parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n{value}\r\n".encode())
    field("title", title or filename)
    field("metadata", json.dumps(metadata, ensure_ascii=False))
    parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{filename}\"\r\nContent-Type: application/octet-stream\r\n\r\n".encode())
    parts.append(payload)
    parts.append(f"\r\n--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def rag_remote_upload(path, url=None, key=None, collection=None, title=None):
    configured_url, configured_key, configured_collection = rag_remote_config()
    url = (url or configured_url).rstrip("/")
    key = key if key is not None else configured_key
    collection = collection if collection is not None else configured_collection
    body, content_type = _multipart_upload(path, title, {"collection": collection} if collection else {})
    return rag_remote_request("POST", f"{url}/documents", key, body=body, content_type=content_type, timeout=900)


def rag_remote_update(document_id, content, url=None, key=None, collection=None, title=None, filename="manual-update"):
    configured_url, configured_key, configured_collection = rag_remote_config()
    url = (url or configured_url).rstrip("/")
    key = key if key is not None else configured_key
    collection = collection if collection is not None else configured_collection
    return rag_remote_request("PUT", f"{url}/documents/{document_id}", key, payload={
        "title": title or filename, "content": content,
        "metadata": {"collection": collection} if collection else {},
    })


def rag_remote_delete(document_id, url=None, key=None):
    configured_url, configured_key, _ = rag_remote_config()
    url = (url or configured_url).rstrip("/")
    key = key if key is not None else configured_key
    return rag_remote_request("DELETE", f"{url}/documents/{document_id}", key)


def load_default_dir():
    """The default directory mlxcli/mlxgui save to and search under when the
    user doesn't give an explicit path. Configurable via save_default_dir
    (persisted in DEFAULT_DIR_CONFIG_PATH); falls back to ~/LocalAI. Created
    on disk if it doesn't exist yet, so callers can always treat it as real."""
    try:
        raw = DEFAULT_DIR_CONFIG_PATH.read_text().strip()
        path = pathlib.Path(raw).expanduser() if raw else DEFAULT_DIR_FALLBACK
    except Exception:
        path = DEFAULT_DIR_FALLBACK
    try:
        path.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    return path


def save_default_dir(path):
    path = pathlib.Path(path).expanduser().resolve(strict=False)
    path.mkdir(parents=True, exist_ok=True)
    DEFAULT_DIR_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    DEFAULT_DIR_CONFIG_PATH.write_text(str(path) + "\n")
    return path

MAX_FILE_CHARS = 8000
MAX_HISTORY_TURNS = 12
MAX_RESPONSE_TOKENS = 8000
MAX_CONTEXT_CHARS = 60000
# Single source of truth for the server's --max-context launch value, so the
# launch args and any client-side context-budget logic can't drift apart.
# 65536 (the next documented tier) hangs this machine (18GB RAM): the process
# goes into uninterruptible sleep, thrashing on expert-weight disk I/O
# instead of generating. 32768 is the highest tier confirmed to work here.
SERVER_MAX_CONTEXT_TOKENS = 32768
# 16 was too tight for a real multi-file task with iterative test-driven
# fixes: writing 4 files (rules/render/main/tests) plus several read-fix-
# rerun cycles hit the cap mid-review on a legitimately succeeding task,
# not a runaway loop. Doubled to 32, then a larger project (maze/collision/
# AI logic across multiple files, with heavier test-iteration) hit that cap
# too — even with in-turn context compaction keeping it safely under the
# token ceiling the whole time, confirming it was genuinely doing more
# legitimate work, not looping. Doubled again for the same reason as before:
# bound an actual infinite loop, not real multi-file iteration.
MAX_TOOL_STEPS = 64

# TurboFieldfareServer defaults repetition_penalty to 1 (i.e. off) when a
# request doesn't specify one — confirmed in its own source
# (Sources/TurboFieldfareServer/Core/OpenAIModels.swift: `request.repetitionPenalty ?? 1`).
# Neither mlxcli nor mlxgui were ever setting it, which is the direct, confirmed
# cause of a local model getting stuck regenerating an identical block verbatim
# dozens of times (nothing was discouraging the repeat). 1.15 is a
# commonly-used value that suppresses loops without over-penalizing the
# legitimately repeated tokens ordinary code contains (braces, keywords, etc.).
DEFAULT_REPETITION_PENALTY = 1.15

# Per-backend sampling overrides, editable from mlxcli (/modelsettings) and
# mlxgui (Model Settings...). Unset keys fall back to these defaults, which
# mirror what TurboFieldfareServer itself defaults to when a field is absent
# (Sources/TurboFieldfareServer/Core/OpenAIModels.swift) so "no override
# saved yet" behaves identically to today.
MODEL_SETTINGS_PATH = pathlib.Path.home() / ".omlx" / "model_settings.json"

MODEL_SETTING_DEFAULTS = {
    "temperature": 0.2,
    "top_p": 0.95,
    "top_k": 64,
    "repetition_penalty": DEFAULT_REPETITION_PENALTY,
    "max_tokens": MAX_RESPONSE_TOKENS,
}

MODEL_SETTING_BOUNDS = {
    "temperature": (0.0, 2.0),
    "top_p": (0.01, 1.0),
    "top_k": (1, 256),
    "repetition_penalty": (1.0, 2.0),
    "max_tokens": (64, 8000),
}


def load_model_settings(backend):
    """Saved overrides for `backend`, merged over MODEL_SETTING_DEFAULTS."""
    settings = dict(MODEL_SETTING_DEFAULTS)
    try:
        saved = json.loads(MODEL_SETTINGS_PATH.read_text())
        for key, value in (saved.get(backend) or {}).items():
            if key in settings:
                settings[key] = value
    except Exception:
        pass
    return settings


def save_model_settings(backend, settings):
    all_settings = {}
    try:
        all_settings = json.loads(MODEL_SETTINGS_PATH.read_text())
    except Exception:
        pass
    all_settings[backend] = {
        key: value for key, value in settings.items() if key in MODEL_SETTING_DEFAULTS
    }
    MODEL_SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    MODEL_SETTINGS_PATH.write_text(json.dumps(all_settings, indent=2) + "\n")


def clamp_model_setting(key, value):
    lo, hi = MODEL_SETTING_BOUNDS.get(key, (None, None))
    if lo is None:
        return value
    return max(lo, min(hi, value))


# Remembers the path (not the content) of the last file mlxcli/mlxgui wrote,
# so a later "update it" / "fix the header" / "resave" request can be
# resolved to a real path without the user re-pasting it, and without
# keeping the file's content sitting in the conversation context. Survives
# /clear and app restarts since it's a tiny file on disk, not chat history.
LAST_ARTIFACT_PATH = pathlib.Path.home() / ".omlx" / "last_artifact.json"


def record_last_artifact(path, task_summary):
    try:
        LAST_ARTIFACT_PATH.parent.mkdir(parents=True, exist_ok=True)
        LAST_ARTIFACT_PATH.write_text(json.dumps({
            "path": str(path),
            "task": (task_summary or "").strip()[:300],
            "timestamp": datetime.now().isoformat(timespec="seconds"),
        }, indent=2) + "\n")
    except Exception:
        pass


def load_last_artifact():
    try:
        data = json.loads(LAST_ARTIFACT_PATH.read_text())
        # A path that's since been moved/deleted is worse than no note at all —
        # it tells the model to read_file a location that no longer exists,
        # confusing an otherwise unrelated turn with a dead reference.
        if data.get("path") and pathlib.Path(data["path"]).exists():
            return data
    except Exception:
        pass
    return None


ERROR_LOG_PATH = pathlib.Path.home() / ".omlx" / "error.log"

_error_logger = None


def _get_error_logger():
    global _error_logger
    if _error_logger is not None:
        return _error_logger
    ERROR_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("mlxcli.errors")
    logger.setLevel(logging.ERROR)
    logger.propagate = False
    if not logger.handlers:
        # 1MB per file, 2 backups kept (~3MB / roughly thousands of entries)
        # — enough to debug a session after the fact without growing unbounded.
        handler = RotatingFileHandler(ERROR_LOG_PATH, maxBytes=1_000_000, backupCount=2, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(message)s", "%Y-%m-%d %H:%M:%S"))
        logger.addHandler(handler)
    _error_logger = logger
    return logger


def log_error(source, message):
    """Append a timestamped error to ~/.omlx/error.log. Best-effort — a logging
    failure should never interrupt the actual chat/tool flow that hit the error."""
    try:
        _get_error_logger().error("[%s] %s", source, str(message).strip()[:2000])
    except Exception:
        pass


def tail_error_log(n=20):
    """Last n log lines, oldest first. Returns [] if the log doesn't exist yet."""
    try:
        lines = ERROR_LOG_PATH.read_text(encoding="utf-8", errors="replace").splitlines()
        return lines[-n:]
    except Exception:
        return []


def last_artifact_system_note():
    """A short system-message reminding the model where the last saved file
    lives, without loading its content — read_file can pull it in on demand
    if the user's next request seems to reference it."""
    artifact = load_last_artifact()
    if not artifact:
        return None
    return (
        f"Most recently saved/edited local file: {artifact['path']} "
        f"(from: \"{artifact['task']}\"). If the user's next request sounds like it refers "
        f"to that file (\"update it\", \"fix the header\", \"regenerate it\", \"amend\", "
        f"\"resave\", no explicit path given), read that file first with read_file to see "
        f"its current contents before making changes, rather than asking the user for the "
        f"path again."
    )


CONVERTIBLE = {".docx", ".pdf", ".pptx", ".xlsx", ".doc"}

REVIEWABLE_TEXT = {
    ".txt", ".md", ".markdown", ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg",
    ".conf", ".py", ".js", ".ts", ".tsx", ".jsx", ".java", ".go", ".rs", ".sh",
    ".zsh", ".bash", ".env", ".xml", ".html", ".css", ".sql", ".csv",
    # Swift/Objective-C (Xcode/iOS/macOS projects), plus the other common
    # languages a local coding model is realistically asked to work in.
    ".swift", ".m", ".mm", ".h", ".hpp", ".c", ".cpp", ".cc", ".cs",
    ".kt", ".kts", ".rb", ".php", ".vue", ".svelte", ".plist",
}

TOOLS = [
    {"type": "function", "function": {
        "name": "run_command",
        "description": "Run a shell command; returns stdout+stderr (truncated).",
        "parameters": {"type": "object", "properties": {
            "command": {"type": "string"}}, "required": ["command"]}}},
    {"type": "function", "function": {
        "name": "read_file",
        "description": "Read a text file on this Mac (any path, not limited to the sandbox). Returns at most "
                        "8000 chars; the result says which lines you got and how many the file has. For logs "
                        "and other files that grow at the end, the newest entries are at the END: use "
                        "tail_lines to read them. Use start_line to read further into a long file.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string"},
            "start_line": {"type": "integer", "description": "1-based line to start from (default 1)"},
            "tail_lines": {"type": "integer", "description": "read only the last N lines instead"}},
            "required": ["path"]}}},
    {"type": "function", "function": {
        "name": "write_file",
        "description": "Write a new file or overwrite an existing file. Shows the user a preview and asks for approval before writing.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string"},
            "content": {"type": "string"}}, "required": ["path", "content"]}}},
    {"type": "function", "function": {
        "name": "ssh_run",
        "description": "Run a shell command on a remote host over SSH, using the password already "
                        "established for that host this session (prompts once and caches it if this "
                        "is the first command to that host). Prefer this over run_command with a "
                        "manually-constructed `ssh user@host ...` string — that path tries local SSH "
                        "key auth, which may not be authorized on the remote host even when password "
                        "auth works fine, and it can't reuse the password already cached here. `host` "
                        "also accepts a saved alias name (e.g. \"dad\", \"hross\") instead of "
                        "user@hostname — aliased hosts resolve their address and password from local "
                        "storage automatically, no credentials needed in the call. If the command needs "
                        "sudo on an aliased host, put the literal placeholder {{SUDO_PASSWORD}} where the "
                        "password would go (e.g. \"echo '{{SUDO_PASSWORD}}' | sudo -S -p '' <cmd>\") — it "
                        "is substituted from storage at execution time; never ask the user for this "
                        "password or invent one.",
        "parameters": {"type": "object", "properties": {
            "host": {"type": "string", "description": "user@hostname, user@ip, or a saved alias name"},
            "command": {"type": "string", "description": "the shell command to run on that host"}},
            "required": ["host", "command"]}}},
    {"type": "function", "function": {
        "name": "ssh_read",
        "description": "Read a text file from a remote host over SSH, using the password already "
                        "established for that host this session. Prefer this over run_command/"
                        "ssh_run with a manually-constructed cat command. `host` also accepts a saved "
                        "alias name (e.g. \"dad\", \"hross\") — resolved automatically, no credentials needed.",
        "parameters": {"type": "object", "properties": {
            "host": {"type": "string", "description": "user@hostname, user@ip, or a saved alias name"},
            "path": {"type": "string", "description": "absolute path on the remote host"}},
            "required": ["host", "path"]}}},
    {"type": "function", "function": {
        "name": "ssh_write",
        "description": "Write a new file or overwrite an existing file on a remote host over SSH, "
                        "using the password already established for that host this session. Shows "
                        "the user a preview and asks for approval before writing, same as write_file. "
                        "`host` also accepts a saved alias name (e.g. \"dad\", \"hross\") — resolved "
                        "automatically, no credentials needed.",
        "parameters": {"type": "object", "properties": {
            "host": {"type": "string", "description": "user@hostname, user@ip, or a saved alias name"},
            "path": {"type": "string", "description": "absolute path on the remote host"},
            "content": {"type": "string"}}, "required": ["host", "path", "content"]}}},
    {"type": "function", "function": {
        "name": "ha_api",
        "description": "Call a Home Assistant instance's REST API on a saved alias (e.g. \"unity\", or "
                        "\"dad\" — which is also reachable this way in addition to ssh_run/ssh_read/"
                        "ssh_write). The auth token is resolved from local storage automatically — never "
                        "ask the user for it. To list or COUNT entities of one kind, use path "
                        "/api/states?domain=<domain>&state=<state> (state is optional): this tool filters and "
                        "counts for you and its result starts with the exact COUNT line, so never count by hand. "
                        "Example: disabled automations = /api/states?domain=automation&state=off (an automation "
                        "is \"on\" when enabled and \"off\" when disabled). Plain /api/states returns only "
                        "per-domain counts. Other paths: GET /api/states/<entity_id> for one entity; "
                        "GET /api/config for instance info; POST /api/services/<domain>/<service> with `data` "
                        "as the JSON body to call a service. Never write scripts against localhost:8123.",
        "parameters": {"type": "object", "properties": {
            "host": {"type": "string", "description": "saved alias name, e.g. \"unity\" or \"dad\" — same "
                                                        "field name as ssh_run/ssh_read/ssh_write"},
            "method": {"type": "string", "description": "GET or POST (default GET)"},
            "path": {"type": "string", "description": "API path, e.g. /api/states/sensor.example"},
            "data": {"type": "object", "description": "JSON body for POST calls (e.g. service data)"}},
            "required": ["host", "path"]}}},
    {"type": "function", "function": {
        "name": "ha_lovelace",
        "description": "Read or change a Home Assistant DASHBOARD (Lovelace views and cards) on a saved alias such as "
                        "\"unity\" or \"dad\". This is the ONLY correct way to edit a dashboard: HA keeps dashboards in "
                        "memory and overwrites hand edits to /config/.storage/lovelace*, and ha_api (REST) cannot touch "
                        "them, so never use sed/awk/jq/python on those files. The auth token is resolved from local "
                        "storage automatically. Workflow: (1) action=\"list\" shows each view's path, title and card "
                        "count; (2) action=\"get\" with `view` (the view's path, e.g. \"hvac\") returns that whole view as "
                        "JSON; (3) change the JSON yourself (move a card into another card's `cards` list, reorder, "
                        "delete, etc.); (4) action=\"set_view\" with `view` and `view_config` = the COMPLETE modified "
                        "view object (it replaces the view; must contain \"path\" and a \"cards\" or \"sections\" list). "
                        "set_view backs up the whole dashboard first, saves, then re-reads to verify, and reports the "
                        "backup path. `dashboard` is optional (a dashboard's url_path such as \"map\"); omit it for the "
                        "host's main dashboard.",
        "parameters": {"type": "object", "properties": {
            "host": {"type": "string", "description": "saved alias name, e.g. \"unity\" or \"dad\""},
            "action": {"type": "string", "description": "list, get, or set_view"},
            "view": {"type": "string", "description": "the view's path (e.g. \"hvac\"); needed for get and set_view"},
            "view_config": {"type": "object", "description": "set_view only: the complete new view object"},
            "dashboard": {"type": "string", "description": "optional dashboard url_path; omit for the main dashboard"}},
            "required": ["host", "action"]}}},
    {"type": "function", "function": {
        "name": "pla_admin",
        "description": "Check or reboot a powerline adapter on a saved alias of type pla_web: \"extender_base\" (192.168.1.176, the "
                        "far end next to the 3000) or \"extender_den\" (192.168.1.177, the master next to the router). These adapters "
                        "have NO ssh/shell, only a web page, so ssh_run cannot reach them: use this tool. action=\"status\" (default) "
                        "returns uptime, firmware and the powerline link rates. action=\"reboot\" presses the adapter's Hardware Reset "
                        "(restarts the powerline modem, never a factory reset; needs the user's approval; the link is down about a "
                        "minute). Credentials are resolved from the Keychain automatically: never ask the user for a password.",
        "parameters": {"type": "object", "properties": {
            "host": {"type": "string", "description": "saved alias name: extender_base or extender_den"},
            "action": {"type": "string", "description": "status (default) or reboot"}},
            "required": ["host"]}}},
    {"type": "function", "function": {
        "name": "web_search",
        "description": "Search the public internet and return a list of result titles, URLs, and snippets. "
                        "This leaves the local machine and always requires the user's explicit approval before "
                        "it runs. Only call this when the user has actually asked for something that needs "
                        "current, external, or general-internet information (e.g. today's news, a product's "
                        "current price, a fact you don't have and can't get from a local tool) -- never call it "
                        "by default, speculatively, or just to double-check something you can already answer.",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string", "description": "the search query"},
            "max_results": {"type": "integer", "description": "how many results to return (default 5, max 10)"}},
            "required": ["query"]}}},
    # ask_user is a deliberately separate concern from the approve()/
    # request_tool_approval() gate that already sits in front of every other
    # tool call (run_command, write_file, ssh_*, ...): that gate is
    # permission (may this action run at all?) and the model never even sees
    # it -- it's a human-side check that happens transparently around
    # exec_tool/execute_tool. ask_user is structure/logic (which of several
    # interpretations, what's the right approach?) and the model calls it
    # directly, on purpose, only when clarify mode is on. Keep these two
    # asking-the-user paths separate rather than merging them -- e.g. never
    # route ask_user answers through ALWAYS_APPROVED, and never have this
    # tool ask a permission-shaped question the approval gate already
    # covers.
    {"type": "function", "function": {
        "name": "ask_user",
        "description": "Pause and ask the human a direct clarifying question, then wait for their answer "
                        "before proceeding. Only call this when a system note in this conversation says clarify "
                        "mode is on -- otherwise use your own best judgment instead and never call this tool. "
                        "Even when it is on, use it sparingly: only for a "
                        "genuine fork in the road where guessing wrong would produce the wrong outcome (which "
                        "of several plausible interpretations, the exact scope of a destructive or hard-to-"
                        "reverse action, a required detail that's actually missing) -- not for routine "
                        "ambiguity you can reasonably resolve yourself. This is for deciding WHAT to build "
                        "or HOW to interpret the request -- never for asking permission to use one of your "
                        "other tools (writing a file, running a command, etc.). Permission for those is "
                        "handled automatically and separately, outside this conversation; you do not need "
                        "the human's go-ahead to call them, so never use ask_user as a substitute for just "
                        "calling the tool the task actually needs.",
        "parameters": {"type": "object", "properties": {
            "question": {"type": "string", "description": "the specific question to ask the human"},
            "options": {"type": "array", "items": {"type": "string"},
                        "description": "optional short list of concrete choices, if this is a pick-one decision"}},
            "required": ["question"]}}},
]


# Deterministic, no-model-call safety net for ssh_run: catches well-known
# destructive command shapes (rm -rf on a broad path, git reset --hard, git
# clean -f, terraform destroy, docker volume rm, SQL DROP/TRUNCATE) before
# run_ssh_command() ever sends them to a remote host. Pure string/regex
# checks — no model turn, no network round-trip, so a bad command is caught
# even if the model itself didn't flag it as risky. The rm/git-clean checks
# tokenize the flag argument instead of enumerating exact flag orderings
# (e.g. "-rf" vs "-fr" vs "-r -f" vs "--recursive --force" all mean the same
# thing) — a hand-enumerated alternation list is exactly the kind of thing
# that quietly misses a real-world variant. Overridable only by an explicit,
# deliberate keyword in the user's own message ("discard", "wipe", "start
# fresh", "force", "really do it") — a false negative here is worse than a
# false positive, since the user can just rephrase to get past a block that
# was actually fine.
# Prefixes conventionally understood as ephemeral/scratch space — safe to
# treat as low-risk even under rm -rf. Everything else absolute or
# home-relative is treated as dangerous: a false negative on a real remote
# host costs far more than an easily-overridden false positive.
_SAFE_RM_PREFIXES = ("/tmp/", "/var/tmp/", "/private/tmp/", "/private/var/tmp/")
_SAFE_RM_EXACT = ("/tmp", "/var/tmp", "/private/tmp", "/private/var/tmp")


def _rm_path_is_dangerous(path):
    if path in (".", "..", "*", "/", "~"):
        return True
    if path in _SAFE_RM_EXACT:
        return False
    if path.startswith(_SAFE_RM_PREFIXES):
        return False
    return path.startswith("/") or path.startswith("~")


def _rm_is_dangerous(cmd):
    """True if `cmd` contains an `rm` call combining recursive+force flags
    (any spelling/order) against a path outside known-safe scratch space."""
    for match in re.finditer(r"\brm\s+([^\n;|&]*)", cmd):
        has_recursive = has_force = False
        path_tokens = []
        for token in match.group(1).split():
            if token in ("--recursive", "-r", "-R"):
                has_recursive = True
            elif token == "--force":
                has_force = True
            elif token.startswith("-") and not token.startswith("--"):
                if any(c in token for c in "rR"):
                    has_recursive = True
                if "f" in token:
                    has_force = True
            elif not token.startswith("-"):
                path_tokens.append(token)
        if has_recursive and has_force and any(_rm_path_is_dangerous(p) for p in path_tokens):
            return True
    return False


def _git_clean_is_dangerous(cmd):
    """True if `cmd` contains a `git clean` call with a force flag in any
    spelling (-f, -fd, -fdx, --force, ...) — force alone already deletes
    untracked files regardless of -d/-x."""
    for match in re.finditer(r"\bgit\s+clean\s+([^\n;|&]*)", cmd):
        for token in match.group(1).split():
            if token == "--force":
                return True
            if token.startswith("-") and not token.startswith("--") and "f" in token:
                return True
    return False


_DESTRUCTIVE_COMMAND_CHECKS = [
    (_rm_is_dangerous, "rm -rf (or equivalent) on a root/home/top-level path — irreversible deletion"),
    (_git_clean_is_dangerous, "git clean with a force flag — deletes untracked files"),
    (lambda c: re.search(r"\bgit\s+reset\s+--hard\b", c) is not None,
     "git reset --hard — discards local commits and uncommitted changes"),
    (lambda c: re.search(r"\bterraform\s+destroy\b", c) is not None,
     "terraform destroy — tears down provisioned infrastructure"),
    (lambda c: re.search(r"\bdocker\s+volume\s+rm\b", c) is not None,
     "docker volume rm — removes persisted volume data"),
    (lambda c: (re.search(r"\b(DROP\s+TABLE|DROP\s+DATABASE|TRUNCATE\s+TABLE)\b", c, re.IGNORECASE) is not None
                and re.search(r"\b(mysql|psql|sqlite3|mongosh|mongo|redis-cli)\b", c, re.IGNORECASE) is not None),
     "SQL DROP/TRUNCATE passed to a database client — destroys table or database contents"),
]

_OVERRIDE_INTENT_KEYWORDS = ("discard", "wipe", "start fresh", "force", "really do it")


def is_destructive_command(cmd):
    """Return (True, description) if cmd matches a known destructive shape,
    else (False, None). Pure lexical check — no model or network call."""
    cmd = cmd or ""
    for check, description in _DESTRUCTIVE_COMMAND_CHECKS:
        if check(cmd):
            return True, description
    return False, None


def has_override_intent(user_message):
    """Whether the user's latest message contains an explicit, deliberate
    override keyword permitting a destructive command to proceed anyway."""
    lowered = (user_message or "").lower()
    return any(keyword in lowered for keyword in _OVERRIDE_INTENT_KEYWORDS)


_CLARIFY_INTENT_KEYWORDS = ("clarify", "ask me if unsure", "check with me first", "ask before")

CLARIFY_MODE_SYSTEM_NOTE = (
    "The user's request invited you to check in before making risky guesses about the "
    "STRUCTURE of the outcome -- what to build, or how to interpret an underspecified "
    "part of the request. Before proceeding, use the ask_user tool for any genuinely "
    "ambiguous or high-stakes decision point of that kind -- which of several plausible "
    "interpretations, the exact scope of a destructive or hard-to-reverse action, a "
    "required detail that's actually missing -- rather than silently picking one and "
    "hoping it's right. Ask one focused question at a time and wait for the answer "
    "before continuing. Do not overuse it: routine ambiguity you can reasonably resolve "
    "yourself, or a decision with no real downside if guessed wrong, doesn't need a "
    "question. This is a separate, unrelated concern from permission to use your other "
    "tools (writing a file, running a command, etc.) -- that permission is handled "
    "automatically outside this conversation regardless of clarify mode, so never call "
    "ask_user to ask whether you're allowed to do something; just do it."
)


# Auto-enabled counterpart of CLARIFY_MODE_SYSTEM_NOTE: turned on for interactive
# agentic turns without the user typing a keyword (the user asked for this
# 2026-09-28, after a "is my hourly email automation running?" request with no
# hint of where to look ran 100+ blind ls/find/ha_api calls without ever asking).
# Narrower than the keyword version on purpose: its main job is one question up
# front when the request doesn't say WHERE to look or WHAT exactly to do, before
# a broad search starts -- not a license to check in on every step.
CLARIFY_AUTO_SYSTEM_NOTE = (
    "Clarify mode is on for this request. Before doing the work, check two things:\n"
    "1. Can the request reasonably be read in two or more ways that would give a DIFFERENT result? "
    "Typical forks: a counting, numbering or ordering rule (what counts as the 1st, 2nd, 3rd item; "
    "whether a repeating sequence restarts or carries on; what happens to an item that is skipped), "
    "which of several files or items is meant, which output format or layout is wanted, or the exact "
    "scope of a change that is hard to undo.\n"
    "2. Does the request leave out WHERE the thing lives (which machine, service, file, folder or config), "
    "so that finding it would take more than one quick, targeted check?\n"
    "If either is true and earlier messages don't already settle it, call ask_user ONCE before starting: "
    "say in a sentence what you are unsure about and give the concrete readings as options. A question "
    "costs the user seconds; building the wrong thing or running dozens of speculative searches costs "
    "minutes. If only one reading makes sense, the difference is cosmetic, or the user already answered "
    "it, do not ask; just proceed. Ask at most twice per request, one question at a time. Never use "
    "ask_user to ask permission to run a tool -- that is handled separately and automatically."
)


# Short reminder attached to the end of the user's own message (on the copy sent to the
# model, never the saved history). Local models weigh the latest user turn far more than
# one paragraph inside a long merged system block, which is where the notes above end up.
CLARIFY_USER_REMINDER = (
    "\n\n[Note: clarify mode is on. If this request can be read more than one way in a way "
    "that changes the result, call ask_user with the options before doing the work.]"
)


def apply_clarify_mode(messages, clarify_mode, anchor_index=None):
    """Return a copy of an outgoing request with clarify instructions added, or the same
    list when clarify_mode is off. The note goes LAST among the system messages (after
    every other note) and a one-line reminder is appended to a user message: the one at
    anchor_index (in `messages`) when given, else the latest one. Callers in a tool loop
    pass the turn's original user message as the anchor -- following the latest user
    message moved the reminder whenever the loop injected a nudge, which edited an
    earlier message and threw away the server's prompt cache for the whole prompt.
    clarify_mode: "auto" (switched on by the app) or any other truthy value (the user's
    own message asked for it, see has_clarify_intent)."""
    if not clarify_mode:
        return messages
    note = CLARIFY_AUTO_SYSTEM_NOTE if clarify_mode == "auto" else CLARIFY_MODE_SYSTEM_NOTE
    out = list(messages)
    last_system = max((i for i, m in enumerate(out) if m.get("role") == "system"), default=-1)
    out.insert(last_system + 1, {"role": "system", "content": note})
    if anchor_index is not None:
        # Shifted by one if the note was inserted at or before it.
        i = anchor_index + 1 if last_system + 1 <= anchor_index else anchor_index
        m = out[i] if 0 <= i < len(out) else {}
        if m.get("role") == "user" and isinstance(m.get("content"), str):
            out[i] = dict(m, content=m["content"] + CLARIFY_USER_REMINDER)
            return out
    for i in range(len(out) - 1, -1, -1):
        m = out[i]
        if m.get("role") == "user" and isinstance(m.get("content"), str):
            out[i] = dict(m, content=m["content"] + CLARIFY_USER_REMINDER)
            break
    return out


def has_clarify_intent(user_message):
    """Whether the user's latest message asked mlxcli to pause and check in on
    ambiguous/critical decisions instead of the local model just guessing.
    Opt-in per request via a keyword, not a standing mode -- most requests
    don't want every minor ambiguity escalated back to the human, so this
    (unlike agentic mode) is never auto-detected from the task's content,
    only from an explicit keyword the user chose to include."""
    lowered = (user_message or "").lower()
    return any(keyword in lowered for keyword in _CLARIFY_INTENT_KEYWORDS)


def term_present(term, lowered_text):
    """Match a keyword as a real word, not a substring buried inside an unrelated
    word — e.g. "test" inside a path like "testLocalAI", or "put" inside "input".
    Terms that are themselves punctuation-anchored (like ".py") skip the leading
    boundary check (the "." already prevents most false positives there), but
    still require a trailing boundary — short extensions like ".c" or ".m" are
    otherwise a real risk of matching inside an unrelated ".com"/".me"/etc."""
    if term.replace(" ", "").isalnum():
        return bool(re.search(r"(?<![A-Za-z0-9])" + re.escape(term) + r"(?![A-Za-z0-9])", lowered_text))
    return bool(re.search(re.escape(term) + r"(?![A-Za-z0-9])", lowered_text))


# --- SemIf-assisted routing (added 2026-09-23) -------------------------------
# Strengthens is_code_request and should_auto_enable_agentic with a real
# local-model judgment: TheoLeeCJ/SemIf's technique (github.com/TheoLeeCJ/SemIf)
# run natively via ~/.omlx/semif/semif_mlx.py on Qwen3.5-4B-4bit. Unlike the
# earlier jev_mlx.py attempt (a hand-applied LoRA adapter, reverted after
# validation showed no improvement), this reads option-letter logits directly
# off a stock, untrained instruct model in a single forward pass -- no
# adapter, no correctness-porting risk. Validated on a 30/10-case hand-built
# test set before wiring in (2026-09-23): should_auto_enable_agentic went
# from 0.567 accuracy (keyword-only, recall 0.133) to 0.767 (recall 0.533)
# with unchanged perfect precision; is_code_request went from 0.900 to a
# perfect 1.000. requires_agentic_execution was deliberately NOT wired --
# SemIf scored worse than keyword there (0.625 vs 0.875) on the same test
# methodology, so that function is untouched, pure keyword logic below.
_SEMIF_PATH = "/Users/dad/.omlx/semif"
_SEMIF_MODEL_PATH = "/Users/dad/.omlx/semif/base-model-4b"
_SEMIF_CONFIDENCE_FLOOR = 0.1  # below this the model is essentially a coin flip; don't trust it
_semif_instance = None
_semif_load_failed = False


def _get_semif():
    global _semif_instance, _semif_load_failed
    if _semif_load_failed:
        return None
    if _semif_instance is None:
        try:
            if _SEMIF_PATH not in sys.path:
                sys.path.insert(0, _SEMIF_PATH)
            import semif_mlx
            _semif_instance = semif_mlx.SemIfMLX(_SEMIF_MODEL_PATH)
        except Exception as exc:
            log_error("semif_classifier", f"failed to load SemIf classifier: {exc}")
            _semif_load_failed = True
            return None
    return _semif_instance


def _semif_route(text, question, keyword_result):
    """Trust a confident SemIf answer; fall back to the keyword heuristic
    unchanged whenever SemIf is unavailable, errors, or is at a near-coin-flip
    (confidence below _SEMIF_CONFIDENCE_FLOOR) -- no interactive prompt this
    time, unlike the reverted jev_mlx attempt: the validated accuracy here was
    strong enough on its own that added human-in-the-loop friction isn't
    justified by evidence, only by caution."""
    semif = _get_semif()
    if semif is None:
        return keyword_result
    try:
        result = semif.ask_noul(text or "", question)
    except Exception as exc:
        log_error("semif_classifier", f"ask_noul failed: {exc}")
        return keyword_result
    if result["confidence"] < _SEMIF_CONFIDENCE_FLOOR:
        return keyword_result
    return result["probability_yes"] > 0.5
# --- end SemIf-assisted routing -----------------------------------------------


def _keyword_is_code_request(text):
    lowered = text.lower()
    code_terms = (
        "script", "code", "program", "python", "bash", "shell", "function",
        "create a", "write a", "generate a",
    )
    return any(term_present(term, lowered) for term in code_terms)


def is_code_request(text):
    keyword_result = _keyword_is_code_request(text)
    return _semif_route(
        text,
        "Is this request specifically asking to write, generate, or produce code, a script, or a program?",
        keyword_result,
    )


def requires_agentic_execution(text):
    """Identify requests where a prose-only answer would falsely imply completion."""
    lowered = (text or "").lower()
    action_terms = (
        "create", "write", "save", "run", "execute", "test", "export",
        "generate files", "place the files", "put the files", "verify",
        "confirm that", "list its byte size", "share the final files",
        "open", "show", "view", "look at", "check", "list", "read",
        "update", "modify", "edit", "add", "change", "build", "set up",
        "find", "locate", "search",
    )
    target_terms = (
        "file", "files", "directory", "folder", "path", "script",
        "downloads", "readme", "project",
        "spreadsheet", "workbook", "excel",
        "app", "application", "program", "python", "database",
        "website", "webpage", "web app", "api", "server", "notebook",
        "presentation", "slides", "document", "game",
        # Any extension mlxcli/mlxgui already knows how to read, convert, or
        # review is itself an unambiguous local-target signal — one source of
        # truth instead of a hand-maintained duplicate subset that drifts.
        *REVIEWABLE_TEXT, *CONVERTIBLE,
    )
    # A literal filesystem path (e.g. pasted from Finder or a prior tool result) is
    # itself an unambiguous signal to verify against the real filesystem, regardless
    # of which verb (if any) accompanies it.
    has_path_literal = bool(re.search(r"(?:^|\s)(~/\S+|/[A-Za-z0-9_][^\s]*)", text or ""))
    return has_path_literal or (
        any(term_present(term, lowered) for term in action_terms)
        and any(term_present(term, lowered) for term in target_terms)
    )


def _keyword_should_auto_enable_agentic(text, messages=None):
    """Detect requests that require local tools without changing the user's mode setting."""
    lowered = (text or "").lower()
    local_target = (
        bool(re.search(r"(?:^|\s)(?:~|/|\./|\.\./)[^\s]+", lowered))
        or any(term_present(term, lowered) for term in (
            "downloads", "download folder", "local file", "local folder", "filesystem",
            "file system", "file", "files", "directory", "folder", "repository", "repo", "working tree",
            "/users/",
            "spreadsheet", "workbook", "excel",
            "script", "project", "app", "application", "program", "database",
            "website", "webpage", "web app", "api", "server", "notebook",
            "presentation", "slides", "document", "game",
            *REVIEWABLE_TEXT, *CONVERTIBLE,
        ))
    )
    operation = any(term_present(term, lowered) for term in (
        "list", "show", "find", "search", "read", "open", "inspect", "review", "compare",
        "check", "audit", "look up", "lookup", "largest", "smallest", "size", "space", "run", "execute",
        "create", "write", "edit", "modify", "update", "add", "change", "save", "export",
        "clean", "tidy", "organize", "organise", "sort out", "declutter",
        "free up", "back up", "backup", "what's using", "whats using", "what's taking",
        "build", "set up",
    ))
    if local_target and operation:
        return True
    if messages and any(term_present(term, lowered) for term in ("file", "files", "creation date", "created", "metadata", "timestamp")):
        recent = "\n".join(
            (message.get("content") or "") for message in messages[-6:]
            if message.get("role") in {"user", "assistant"}
        ).lower()
        return any(term in recent for term in ("downloads", "local file", "file listing", "largest files"))
    return False


def should_auto_enable_agentic(text, messages=None):
    keyword_result = _keyword_should_auto_enable_agentic(text, messages)
    return _semif_route(
        text,
        "Does fulfilling this request require using local tools such as reading/writing files, "
        "running shell commands, or inspecting the filesystem?",
        keyword_result,
    )


# Words that make a following "run"/"test" a noun ("the last run", "a test") and words that make the
# sentence a question about what already happened ("did it run", "has the job run").
_RUN_NOUN_BEFORE = {"the", "a", "an", "that", "this", "each", "every", "last", "latest", "previous", "next",
                    "first", "recent", "hourly", "daily", "nightly", "weekly", "scheduled", "test", "dry", "trial",
                    "its", "his", "her", "their", "our", "my", "your", "unit", "integration", "smoke"}
_PAST_QUESTION = {"did", "does", "do", "has", "have", "had", "was", "were", "is", "are", "when", "whether"}


def _verb_requested(term, lowered):
    """Whether `term` ("run", "execute", "test") appears as something the user wants DONE, not a noun or a
    question about the past. A bare word match made "when did the job last run, and did that run
    succeed?" demand a run_command, so a question answered by one read_file was forced into four
    pointless commands and 8 minutes (seen 2026-09-28)."""
    # Clause by clause (sentence punctuation, "and", "then"), so "what's in the log? run the script" and
    # "read the file and run it" still count, but a question word anywhere earlier in the same clause
    # rules it out however long the path in between ("is the script in ~/a/b/c.py run by cron?").
    for clause in re.split(r"[.?!;,](?=\s|$)|\band\b|\bthen\b", lowered):
        words = re.findall(r"[a-z0-9_']+", clause)
        for i, w in enumerate(words):
            if w != term:
                continue
            if (words[i - 1] if i else "") in _RUN_NOUN_BEFORE:
                continue
            if any(x in _PAST_QUESTION for x in words[:i]):
                continue
            return True
    return False


def execution_contract(text):
    lowered = (text or "").lower()
    # A literal filesystem path is itself an unambiguous target, same as in
    # requires_agentic_execution — a request built entirely around an absolute
    # path (very common: paths pasted from Finder or a prior tool result) won't
    # necessarily contain the literal word "file" or "path" anywhere in the text.
    has_path_literal = bool(re.search(r"(?:^|\s)(~/\S+|/[A-Za-z0-9_][^\s]*)", text or ""))
    target = has_path_literal or any(term_present(term, lowered) for term in ("file", "files", "directory", "folder", "path", "csv", "script", "downloads"))
    return {
        "write": target and any(term_present(term, lowered) for term in ("create", "write", "save", "generate", "place", "put", "update", "modify", "edit", "add", "change")),
        "run": target and any(_verb_requested(term, lowered) for term in ("run", "execute", "test")),
        "verify": target and any(term_present(term, lowered) for term in ("verify", "inspect", "confirm", "byte size", "exists")),
    }


def read_text_window(text, args, max_chars=None):
    """The part of a file read_file returns, with a header saying exactly what was
    shown. Used to be a bare text[:8000] with a "[truncated]" tail, so a model
    reading a log (newest entries at the END) only ever saw the oldest ones, with
    no way to ask for more -- seen 2026-09-28 reporting wrong "last run" times
    from a 16K-char log whose recent runs were all past the cut."""
    max_chars = max_chars or MAX_FILE_CHARS
    lines = text.splitlines(keepends=True)
    total = len(lines)

    def _int(v):
        try:
            return int(v)
        except (TypeError, ValueError):
            return None

    tail = _int(args.get("tail_lines"))
    start = _int(args.get("start_line"))
    if tail and tail > 0:
        first = max(total - tail, 0)
        chunk = lines[first:]
        # Keep the END when a tail window is still too big.
        body = "".join(chunk)
        if len(body) > max_chars:
            body = body[-max_chars:]
            first = total - body.count("\n") - (0 if body.endswith("\n") else 1)
        shown_from, shown_to = first + 1, total
    else:
        first = max((start or 1) - 1, 0)
        body, shown_to = "", first
        for line in lines[first:]:
            if len(body) + len(line) > max_chars:
                break
            body += line
            shown_to += 1
        if not body and first < total:  # a single line longer than the budget
            body, shown_to = lines[first][:max_chars], first + 1
        shown_from = first + 1
    if total == 0:
        return "[empty file]"
    if shown_from <= 1 and shown_to >= total:
        return body
    more = []
    if shown_from > 1:
        more.append(f"lines 1-{shown_from - 1} not shown (start_line={max(shown_from - 200, 1)} to go back)")
    if shown_to < total:
        more.append(f"lines {shown_to + 1}-{total} not shown (start_line={shown_to + 1} for the next part, "
                    f"tail_lines=N for the end)")
    return (f"[showing lines {shown_from}-{shown_to} of {total}; " + "; ".join(more) + "]\n" + body)


# --- macOS Seatbelt sandbox (the default; the Linux `container` sandbox is the fallback) ------------
# Modeled on Codex CLI: commands can READ anything on the Mac but can only WRITE inside the working
# folder (plus temp dirs). The container sandbox could only see the working folder, so the model read
# empty results about ~/Library as facts and "installed" files that never left the container
# (2026-09-28). With Seatbelt, reads are real and a write outside the folder fails with a real
# "Operation not permitted".
SANDBOX_EXEC = "/usr/bin/sandbox-exec"


def seatbelt_available():
    return os.path.exists(SANDBOX_EXEC) and os.environ.get("MLXCLI_SANDBOX", "").lower() != "container"


def seatbelt_profile(workdir):
    home = pathlib.Path.home()
    writable = [pathlib.Path(workdir).resolve(), pathlib.Path("/private/tmp"), pathlib.Path("/private/var/folders"),
                pathlib.Path("/dev"), home / ".cache"]
    paths = " ".join('(subpath "%s")' % str(p).replace("\\", "\\\\").replace('"', '\\"') for p in writable)
    return f"(version 1)(allow default)(deny file-write* (require-not (require-any {paths})))"


# Commands that change the Mac's state WITHOUT writing a file, so Seatbelt's write rule can't stop them
# (verified 2026-09-28: `defaults write` succeeds from inside the sandbox). Refused in the sandbox with
# an honest "not done" instead.
_MAC_STATE_CHANGERS = (
    (r"\bdefaults\s+(-currentHost\s+)?(write|delete|import|rename)\b", "changes macOS settings (defaults write/delete)"),
    (r"\blaunchctl\s+(load|unload|bootstrap|bootout|kickstart|kill|enable|disable|remove|submit|start|stop|"
     r"setenv|unsetenv|config|reboot)\b", "loads, unloads or controls launchd jobs"),
    (r"\bosascript\b", "runs AppleScript, which can control apps and the system"),
    (r"(^|[;&|(]\s*|\bthen\s+|\bdo\s+)open\s", "opens apps or files in the GUI"),
    (r"\b(killall|pkill)\b", "kills running processes"),
    (r"\bbrew\s+(install|uninstall|upgrade|reinstall|remove|rm|services|link|unlink|tap|untap|cleanup|autoremove)\b",
     "installs or changes Homebrew packages or services"),
    (r"\bsudo\b", "needs administrator rights"),
    (r"\b(shutdown|reboot|halt)\b", "shuts down or restarts the Mac"),
    (r"\bcrontab\b(?!\s+-l\b)", "changes the crontab"),
    (r"\bpmset\s+(?!-g\b)", "changes power settings"),
    (r"\b(networksetup\s+-set|scutil\s+--set|systemsetup\s+-set|nvram\s+\w+=|tccutil\s+reset)",
     "changes system or network settings"),
    (r"\bdscl\s+\S+\s+-(create|delete|passwd|append|merge)", "changes user accounts"),
    (r"\bsecurity\s+(add|delete|import|set|unlock|create|remove)", "changes the keychain"),
    (r"\bsoftwareupdate\s+(-i|--install|-a|--all)", "installs software updates"),
    (r"\btmutil\s+(delete|enable|disable|start|stop|setdestination|thin|exclude|include)", "changes Time Machine"),
    (r"\bdiskutil\s+(erase|partition|unmount|mount|eject|rename|apfs|repair)", "changes disks or volumes"),
)


def mac_state_change(cmd):
    """Why a sandboxed shell command would change Mac state outside the file system, or None."""
    for pattern, why in _MAC_STATE_CHANGERS:
        if re.search(pattern, cmd or ""):
            return why
    return None


# --- Checks run on every file write_file produces (like Aider's auto-lint) --------------------------
try:
    import yaml as _yaml

    class _YamlAnyTagLoader(_yaml.SafeLoader):
        """SafeLoader that accepts app-specific tags (Home Assistant's !include, !secret, ...)."""

    _YamlAnyTagLoader.add_multi_constructor("!", lambda loader, suffix, node: None)
except ImportError:
    _yaml = None


def validate_written_file(path):
    """A problem with a file that was just written, as a short message, or None if it checks out (or no
    checker applies). Catches files that "were written successfully" but won't load: a plist launchd
    rejects, JSON/YAML that won't parse, a shell script with a syntax error, Python with undefined names."""
    path = pathlib.Path(path)
    ext = path.suffix.lower()

    def _run(args):
        try:
            p = subprocess.run(args, capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.TimeoutExpired):
            return None
        return None if p.returncode == 0 else ((p.stdout + p.stderr).strip()[:600] or f"exit code {p.returncode}")

    try:
        if ext == ".plist":
            problem = _run(["/usr/bin/plutil", "-lint", str(path)]) if os.path.exists("/usr/bin/plutil") else None
            return f"plutil -lint failed: {problem}" if problem else None
        if ext == ".json":
            json.loads(path.read_text())
            return None
        if ext in (".yaml", ".yml") and _yaml is not None:
            _yaml.load(path.read_text(), Loader=_YamlAnyTagLoader)
            return None
        if ext == ".toml":
            import tomllib
            tomllib.loads(path.read_text())
            return None
        if ext in (".sh", ".bash"):
            problem = _run(["/bin/bash", "-n", str(path)])
            return f"bash -n (syntax check) failed: {problem}" if problem else None
        if ext == ".zsh":
            problem = _run(["/bin/zsh", "-n", str(path)])
            return f"zsh -n (syntax check) failed: {problem}" if problem else None
        if ext == ".py" and importlib.util.find_spec("pyflakes"):
            try:
                p = subprocess.run([sys.executable, "-m", "pyflakes", str(path)], capture_output=True, text=True,
                                   timeout=30)
            except (OSError, subprocess.TimeoutExpired):
                return None
            # Only real errors: undefined names break at run time; unused imports etc. are just noise.
            bad = [ln for ln in (p.stdout + p.stderr).splitlines() if "undefined name" in ln]
            return "pyflakes: " + "; ".join(bad[:5]) if bad else None
    except Exception as exc:  # parse errors from json/yaml/toml
        return f"{ext[1:]} does not parse: {str(exc)[:400]}"
    return None


# --- Notes files the model sees every turn (like Codex's AGENTS.md / Gemini CLI's GEMINI.md) ---------
USER_NOTES_PATH = pathlib.Path.home() / ".omlx" / "notes.md"
NOTES_MAX_CHARS = 4000


def notes_system_text(dirs=()):
    """Standing notes for the model: the user's own ~/.omlx/notes.md (facts about their setup, e.g. where
    scheduled jobs live) plus a project notes file (.mlxcli-notes.md / AGENTS.md / CLAUDE.md) from each
    of `dirs`. Read fresh every call so edits take effect immediately. Empty string when there are none."""
    parts, seen = [], set()
    try:
        text = USER_NOTES_PATH.read_text(errors="replace").strip()
        if text:
            parts.append(f"User notes (from {USER_NOTES_PATH}; standing facts about this user's setup -- "
                         f"use them before searching):\n{text[:NOTES_MAX_CHARS]}")
    except OSError:
        pass
    for d in dirs:
        if not d:
            continue
        notes_path, notes_text = find_project_notes(d)
        if notes_text and notes_path.resolve() not in seen:
            seen.add(notes_path.resolve())
            parts.append(f"Project notes from {notes_path}:\n{notes_text[:NOTES_MAX_CHARS]}")
    return "\n\n".join(parts)


# --- Read before overwrite (like Claude Code): an existing file may only be overwritten after the model
# has seen its current contents. Without this a small model "fixes" a document by rewriting it from
# memory of what it wrote earlier, quietly undoing other edits or dropping content.
_KNOWN_FILES = {}


def _file_stamp(path):
    try:
        st = pathlib.Path(path).stat()
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        return None


def note_file_known(path):
    """Record that the model has the current contents of `path` (it just read or wrote it)."""
    stamp = _file_stamp(path)
    if stamp:
        _KNOWN_FILES[str(pathlib.Path(path).expanduser().resolve())] = stamp


def overwrite_needs_read(path):
    """An error message if `path` exists and the model hasn't read it since it last changed, else None."""
    target = pathlib.Path(path).expanduser().resolve()
    stamp = _file_stamp(target)
    if stamp is None or not target.is_file():
        return None  # new file: nothing to lose
    known = _KNOWN_FILES.get(str(target))
    if known == stamp:
        return None
    why = "has changed since you last read it" if known else "already exists and you have not read it in this session"
    return (f"Error: NOT written. {target} {why}. Read it with read_file first, then write your change based on "
            f"its CURRENT contents (keep everything the user didn't ask to change).")


def loaded_file_message(path, label, text, extra=""):
    """(message content, display label, chars loaded) for a local file the user loaded into the chat.

    Says where the content came from -- a local file on this Mac, its full path and size -- so the model
    can tell it apart from text pasted from the web or a remote host, and can go back to the file. Used to
    be a bare text[:8000] with "[truncated to 8000 chars]", so anything past that was simply gone, with no
    hint that the rest existed or where to find it."""
    p = pathlib.Path(path).expanduser().resolve()
    converted = label != p.name   # e.g. "x.docx (converted to markdown)", filtered xlsx rows
    lines = text.count("\n") + (1 if text and not text.endswith("\n") else 0)
    source = f"loaded by the user from the local file {p} on this Mac"
    if converted:
        source += f", converted from {p.suffix or 'its original format'} to text"
    header = f"Contents of {label} ({source}; {len(text):,} chars, {lines:,} lines{extra})"
    partial = len(text) > MAX_FILE_CHARS
    if not converted:
        body = read_text_window(text, {})
        hint = (f"\n[Only part of the file is loaded. To read more, call read_file with path \"{p}\" and "
                f"start_line, or tail_lines for the end.]") if partial else ""
    else:
        body = text[:MAX_FILE_CHARS]
        hint = (f"\n[Only the first {MAX_FILE_CHARS:,} of {len(text):,} chars are loaded. read_file can't read "
                f"the rest of a {p.suffix or 'converted'} file as text: if you need a later part, tell the user "
                f"which part and ask them to /paste it.]") if partial else ""
    loaded = min(len(text), MAX_FILE_CHARS)
    if not converted and not partial and not extra:
        note_file_known(p)   # the model has seen the whole current file, same as a full read_file
    display = f"{label} from {p}" + (f" [first {loaded:,} of {len(text):,} chars loaded; the model knows where "
                                     f"the rest is]" if partial else "")
    return f"{header}:{hint}\n\n{body}", display, loaded


def compact_call_arguments(fn, args_str, note):
    """Replacement arguments for a compacted tool call that still say WHAT it acted on. Dropping the whole
    argument string also dropped the file path, so a few turns later the model no longer knew which
    document it had created and couldn't find it to fix it."""
    keep = {"__compacted__": True, "fn": fn, "note": note}
    try:
        args = json.loads(args_str)
    except (TypeError, ValueError):
        args = None
    if isinstance(args, dict):
        for key in ("path", "host", "target", "method", "action", "url"):
            if isinstance(args.get(key), str) and args[key]:
                keep[key] = args[key][:300]
        if isinstance(args.get("command"), str):
            keep["command"] = args["command"][:200]
    return json.dumps(keep)


def python_code_writes_files(code):
    """Whether python_interpreter code writes to the filesystem (those must go through write_file).
    Blocks WRITES only: the old check refused any code containing "open(", so reads like
    json.load(open(p)) were refused too and the model couldn't analyze a data file in Python."""
    return bool(re.search(r"""\bopen\([^)]*?(?:,\s*|mode\s*=\s*)['"][^'"]*[wax+]""", code or "")
                or any(term in (code or "") for term in ("write_text(", "write_bytes(", "makedirs(", "mkdir(",
                                                         "os.remove(", "unlink(", "rmtree(", "shutil.move(",
                                                         "shutil.copy", "os.rename(")))


def clip_output(out, max_chars=None):
    """Command output that fits the budget, keeping its head AND tail and saying
    so. Plain out[:8000] silently dropped the end (a long `find`, a log `cat`, a
    test run's final summary) with no sign anything was missing."""
    max_chars = max_chars or MAX_FILE_CHARS
    if len(out) <= max_chars:
        return out
    half = max_chars // 2
    omitted = len(out) - 2 * half
    return (out[:half] + f"\n[... {omitted:,} chars of output omitted from the middle; narrow the command "
            f"(grep, head, tail) if you need that part ...]\n" + out[-half:])


_HTTP_FAILURE = re.compile(r"(^|\n)(\[source: [^\]]*\]\s*)?http [45]\d\d from ")


def tool_result_failed(result):
    lowered = (result or "").lower()
    match = re.search(r"exit_code=(-?\d+)", lowered)
    return bool((match and int(match.group(1)) != 0) or any(
        term in lowered for term in ("error:", "not found", "timed out", "user declined")
    ) or _HTTP_FAILURE.search(lowered))


def python_syntax_error(source):
    """None if source parses as valid Python; otherwise a short "line N: message"
    description of the SyntaxError. Catches a write_file call that got cut off
    mid-generation (e.g. hit max_tokens mid-string) before it's reported as a
    successful write."""
    try:
        ast.parse(source)
        return None
    except SyntaxError as e:
        return f"line {e.lineno}: {e.msg}"


def missing_local_imports(py_path):
    """Top-level import names in py_path that look like local sibling modules
    (not stdlib, not installed) but have no matching file next to py_path —
    the exact shape of `from gui import X` when gui.py was never written.
    Returns a sorted list of missing names; [] if the file parses clean or
    doesn't parse at all (a syntax error is reported separately)."""
    py_path = pathlib.Path(py_path)
    try:
        tree = ast.parse(py_path.read_text(errors="replace"))
    except Exception:
        return []
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module:
                names.add(node.module.split(".")[0])
    missing = []
    for name in sorted(names):
        if name in sys.stdlib_module_names:
            continue
        try:
            found = importlib.util.find_spec(name) is not None
        except Exception:
            found = True  # ambiguous — don't flag a name we can't resolve cleanly
        if found:
            continue
        sibling_file = py_path.parent / f"{name}.py"
        sibling_pkg = py_path.parent / name / "__init__.py"
        if not sibling_file.exists() and not sibling_pkg.exists():
            missing.append(name)
    return missing


def find_entry_point_candidates(py_paths):
    """Which of these just-written .py files look like a runnable entry point
    (has `if __name__ == "__main__":`) — for offering a post-build smoke test.
    Deduplicates and skips paths that no longer exist."""
    candidates = []
    seen = set()
    for raw in py_paths:
        path = pathlib.Path(raw)
        key = str(path)
        if key in seen or not path.exists():
            continue
        seen.add(key)
        try:
            text = path.read_text(errors="replace")
        except Exception:
            continue
        if re.search(r'if\s+__name__\s*==\s*[\'"]__main__[\'"]\s*:', text):
            candidates.append(path)
    return candidates


def smoke_test_python_app(path, timeout=4):
    """Launch `python3 path` briefly to catch startup crashes that py_compile
    can't see — missing runtime dependencies, import-time exceptions, etc.
    A process still running after `timeout` seconds is treated as a pass (it
    started without crashing and is presumably sitting in an event loop) and
    gets terminated; a clean exit(0) within the window is also a pass; a
    nonzero exit is a fail, returned with the captured stderr tail."""
    path = pathlib.Path(path)
    try:
        proc = subprocess.Popen(
            [sys.executable, str(path)],
            cwd=str(path.parent),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
    except Exception as e:
        return False, f"Could not launch: {e}"
    try:
        _, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.terminate()
        try:
            proc.wait(timeout=2)
        except Exception:
            proc.kill()
        return True, f"Still running after {timeout}s with no crash (likely a GUI/event-loop app) — terminated for the test."
    if proc.returncode == 0:
        return True, "Exited cleanly (code 0)."
    tail = "\n".join((stderr or "").strip().splitlines()[-15:])
    return False, f"Exited with code {proc.returncode}.\n{tail}"


def run_verify_command(command, timeout=60):
    """Run a shell command as a correctness check — e.g. after a headless
    --run task claims completion, prove it rather than trust the model's own
    claim. Returns (passed, output_tail): passed is True only on exit code 0;
    output_tail is the last ~40 lines of combined stdout+stderr, meant to be
    fed straight back to the model as a concrete failure signal to fix."""
    try:
        proc = subprocess.run(
            command, shell=True, cwd=str(load_default_dir()),
            capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return False, f"Verification command timed out after {timeout}s."
    except Exception as e:
        return False, f"Could not run verification command: {e}"
    output = (proc.stdout or "") + (proc.stderr or "")
    tail = "\n".join(output.strip().splitlines()[-40:])
    return proc.returncode == 0, tail


def _maybe_archive_verify_script(command, history_dir):
    """If `command` is exactly a runner plus one script file ("bash X.sh" or
    "python3 X.py"), copy that script into history_dir so a later --verify
    also re-runs it as a regression check. Content-hash deduped — running the
    same check again doesn't pile up duplicate copies. Silently does nothing
    for inline/complex commands, since those can't be safely re-invoked out
    of their original context."""
    parts = command.split()
    if len(parts) != 2:
        return
    runner, script = parts
    if runner not in ("bash", "sh", "python3", "python"):
        return
    script_path = pathlib.Path(script)
    if not script_path.is_file():
        return
    try:
        content = script_path.read_bytes()
    except Exception:
        return
    digest = hashlib.sha256(content).hexdigest()[:12]
    dest = pathlib.Path(history_dir) / f"{script_path.stem}_{digest}{script_path.suffix}"
    if dest.exists():
        return
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(content)
        dest.chmod(0o755)
    except Exception:
        pass


def run_verify_suite(command, history_dir=None, timeout=60):
    """Run `command` (the current task's own correctness check) plus every
    .sh/.py script already accumulated in history_dir (regression checks from
    earlier steps in this same project) — a step that breaks something two
    steps back gets caught here instead of only surfacing when a human
    happens to rerun an old check by hand. On a full pass, also archives
    `command` into history_dir (if it's a simple runner+script form) so it
    joins the regression suite for whatever comes next.

    Returns (passed, output) — passed only if the main command AND every
    archived script all exit 0; output concatenates every failure's tail."""
    passed, output = run_verify_command(command, timeout=timeout)
    if not history_dir:
        return passed, output

    failures = [] if passed else [f"[current check] {output}"]
    history_path = pathlib.Path(history_dir)
    if history_path.is_dir():
        for script in sorted(history_path.glob("*")):
            if script.suffix not in (".sh", ".py"):
                continue
            runner = "bash" if script.suffix == ".sh" else sys.executable
            regression_ok, regression_output = run_verify_command(f"{runner} {script}", timeout=timeout)
            if not regression_ok:
                failures.append(f"[regression: {script.name}] {regression_output}")

    if failures:
        return False, "\n\n".join(failures)

    _maybe_archive_verify_script(command, history_dir)
    return True, output


def resolve_output_path(raw):
    # A relative path (no directory given) defaults to the configured default
    # directory for convenience. An absolute path is honored as-is — the
    # diff/preview + approval prompt in write_file is the actual safety gate,
    # not a fixed directory restriction, since the latter silently blocks
    # legitimate writes to project files elsewhere.
    # Strip stray whitespace first: a model-emitted path with a leading space
    # (seen in practice) makes Path.is_absolute() return False for an
    # otherwise-absolute path, so the default dir gets wrongly prepended,
    # producing a garbled nested path instead of the intended location.
    p = pathlib.Path(str(raw).strip()).expanduser()
    if not p.is_absolute():
        p = load_default_dir() / p
    return p.resolve(strict=False)


def normalize_tool_name(name):
    return str(name or "").strip().lower().replace("/", ":").replace(".", ":").rsplit(":", 1)[-1]


def infer_command_cwd(command):
    """Keep relative script outputs beside an absolute local input file."""
    for raw in re.findall(r"(?<![A-Za-z0-9])(/Users/[^\s'\"`]+)", command or ""):
        path = pathlib.Path(raw.rstrip(".,:;()"))
        if path.suffix.lower() in {".csv", ".tsv", ".json", ".xlsx", ".txt"} and path.exists():
            return str(path.parent)
    return None


def detect_repetition_loop(text):
    """True if the tail of `text` looks like a degenerate generation loop — the
    same block of text repeated verbatim three times in a row. Local models
    occasionally get stuck in this failure mode (observed: a Qwen backend
    repeating an identical "<antThinking>...</antThinking>" block, ~220+ chars
    each, dozens of times rather than producing a real response). Catching it
    lets a turn abort early instead of burning the full token budget
    generating garbage, every retry, for a prompt the model has gotten stuck on.

    The minimum block size (100 chars) is deliberately well above a single
    line of ordinary code: legitimate SwiftUI/JSX/CSS-style code very
    routinely repeats a short line verbatim three-plus times in a row on
    purpose (e.g. four consecutive `GridItem(.flexible()),` entries for a
    4-column grid, ~43 chars each) — that's correct output, not a stuck model,
    and a lower floor here false-positived on exactly that during testing.

    Checks block sizes from large to small so a big repeated unit is found
    before a smaller coincidental repeat inside it gets matched instead."""
    stripped = (text or "").strip()
    if len(stripped) < 300:
        return False
    # Start a few chars above the naive len//3 estimate: stripping leading/
    # trailing whitespace off the whole text can shave a char or two off just
    # the first/last repeat, nudging the true period slightly above len//3.
    for block_len in range(len(stripped) // 3 + 5, 99, -1):
        tail = stripped[-block_len * 3:]
        if len(tail) < block_len * 3:
            continue
        a, b, c = tail[:block_len], tail[block_len:block_len * 2], tail[block_len * 2:]
        if a == b == c and a.strip():
            return True
    return False


BUILTIN_TOOL_NAMES = ("run_command", "read_file", "write_file", "python_interpreter",
                       "ssh_run", "ssh_read", "ssh_write", "ha_api", "ha_lovelace", "pla_admin", "web_search", "ask_user")
KNOWN_TOOL_NAMES = BUILTIN_TOOL_NAMES


PLUGIN_DIR = pathlib.Path.home() / ".omlx" / "plugins"
_PLUGIN_REGISTRY = None


def load_plugin_tools():
    """Load user-supplied tool plugins from ~/.omlx/plugins/*.py. Each plugin
    file must define TOOL_SCHEMA (same shape as one entry in TOOLS, i.e.
    {"type": "function", "function": {"name", "description", "parameters"}})
    and a run(args) function returning the tool's result string. It may also
    define APPROVAL_CATEGORY (str, defaults to the tool name -- used the same
    way as the category argument to mlxcli's approve()) and REQUIRES_APPROVAL
    (bool, default True -- set False only for a plugin with no side effects
    and nothing leaving the local machine).

    A plugin that fails to import, or is missing TOOL_SCHEMA/run, or claims a
    name already used by a builtin or another plugin, is skipped with a
    warning rather than aborting startup -- one broken plugin file should
    never take down the whole CLI/GUI. This is how new tool capabilities get
    added without editing mlxcli/mlxgui.py/mlxlib.py directly."""
    registry = {}
    if not PLUGIN_DIR.is_dir():
        return registry
    for path in sorted(PLUGIN_DIR.glob("*.py")):
        if path.name.startswith("_"):
            continue
        try:
            spec = importlib.util.spec_from_file_location(f"omlx_plugin_{path.stem}", path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            schema = module.TOOL_SCHEMA
            run = module.run
            name = schema["function"]["name"]
            if name in BUILTIN_TOOL_NAMES or name in registry:
                print(f"[plugin] {path.name}: tool name '{name}' already in use, skipping", file=sys.stderr)
                continue
            registry[name] = {
                "schema": schema,
                "run": run,
                "category": getattr(module, "APPROVAL_CATEGORY", name),
                "requires_approval": bool(getattr(module, "REQUIRES_APPROVAL", True)),
            }
        except Exception as exc:
            print(f"[plugin] failed to load {path.name}: {exc}", file=sys.stderr)
    return registry


def plugin_tools():
    """Cached plugin registry (loaded once per process). Extends
    KNOWN_TOOL_NAMES in place so the text-fallback tool-call parsers below
    (parse_bare_json_tool_call, parse_xml_tag_tool_call, parse_attr_tag_tool_call)
    also recognize plugin tool names from backends with weaker structured
    function-calling support, not just the primary JSON tool_calls path."""
    global _PLUGIN_REGISTRY, KNOWN_TOOL_NAMES
    if _PLUGIN_REGISTRY is None:
        _PLUGIN_REGISTRY = load_plugin_tools()
        if _PLUGIN_REGISTRY:
            KNOWN_TOOL_NAMES = BUILTIN_TOOL_NAMES + tuple(_PLUGIN_REGISTRY.keys())
    return _PLUGIN_REGISTRY


def all_tool_schemas():
    """TOOLS plus every loaded plugin's schema -- what actually gets sent to
    the model as the available tool list."""
    return TOOLS + [p["schema"] for p in plugin_tools().values()]


def _unique_call_id(prefix):
    """A globally-unique synthetic tool_call id, not just unique within one
    parser invocation. Using a plain per-call-list counter (call_0, call_1, ...)
    meant the same id recurred across different turns of the same
    conversation, since each parser call restarts counting from 0 — and the
    server's own history validator rejects a conversation containing a
    repeated tool_call id with "invalid or duplicate historical tool call",
    confirmed directly from a live run."""
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def repair_truncated_json(text):
    """Best-effort recovery for a tool-call arguments string that's valid
    JSON except for being cut off mid-way (a stream_error breaking the
    response mid-tool-call, a max_tokens cutoff, a provider that reused a
    streaming index across two different tool calls and glued their
    fragments together) — closes an unterminated string and/or unclosed
    braces/brackets, then re-checks that the result actually parses.

    Deliberately conservative: only ever ADDS closing punctuation at the
    end, never rewrites/removes anything from the interior, so it can't
    turn a well-formed-but-semantically-wrong payload into something that
    looks superficially more valid than it is. Returns the parsed dict on
    success, or None if the string still isn't recoverable this way (the
    caller's existing "fall back to an empty dict rather than crash"
    behavior is unaffected either way)."""
    if not text or not text.strip():
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    fixed = text
    in_string = False
    escaped = False
    depth_stack = []
    for ch in fixed:
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch in "{[":
            depth_stack.append(ch)
        elif ch in "}]":
            if depth_stack:
                depth_stack.pop()

    if in_string:
        fixed += '"'
    for opener in reversed(depth_stack):
        fixed += "}" if opener == "{" else "]"

    if fixed == text:
        return None  # nothing to add — the JSON was just plain broken, not truncated
    try:
        return json.loads(fixed)
    except json.JSONDecodeError:
        return None


def parse_bare_json_tool_call(content):
    """Some backends occasionally emit a tool call as plain text: the bare
    function name on its own line, followed by a raw JSON object of arguments —
    not this project's `call:name{...}` text-adapter syntax (see
    parse_text_tool_calls in mlxcli/mlxgui), and not a real structured
    tool_calls API response either. Observed directly from a Qwen backend:
    'write_file\\n{"path": "...", "content": "..."}'. Recognize it anyway
    rather than discarding an otherwise well-formed call just because of its
    shape — the alternative is the model's whole response getting treated as
    prose, retried, and often regenerating the same large output again."""
    if not content:
        return []
    calls = []
    for m in re.finditer(r"\b(" + "|".join(KNOWN_TOOL_NAMES) + r")\b", content):
        name = m.group(1)
        brace_pos = content.find("{", m.end())
        if brace_pos == -1:
            continue
        # Only the tool name and whitespace/newlines may separate it from the
        # opening brace — avoids matching the word "write_file" turning up
        # incidentally in ordinary prose elsewhere in the response.
        if content[m.end():brace_pos].strip():
            continue
        try:
            args, _ = json.JSONDecoder().raw_decode(content[brace_pos:])
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(args, dict):
            continue
        calls.append({"id": _unique_call_id("bare_call"), "type": "function",
                      "function": {"name": name, "arguments": json.dumps(args)}})
    return calls


def parse_xml_tag_tool_call(content):
    """Some backends occasionally emit a tool call as XML-style tags instead of
    real structured tool_calls or this project's other text-adapter conventions:
    <toolname><argname>value</argname></toolname>. Observed directly from a Qwen
    backend for read_file and run_command, repeated across every retry of a
    stuck turn. Recognize it so a well-formed attempt doesn't get discarded as
    prose and force yet another expensive regeneration."""
    if not content:
        return []
    calls = []
    for name in KNOWN_TOOL_NAMES:
        for m in re.finditer(rf"<{name}>(.*?)</{name}>", content, re.DOTALL | re.IGNORECASE):
            inner = m.group(1)
            args = {}
            for arg_match in re.finditer(r"<(\w+)>\s*(.*?)\s*</\1>", inner, re.DOTALL):
                args[arg_match.group(1)] = arg_match.group(2).strip()
            if args:
                calls.append({"id": _unique_call_id("xml_call"), "type": "function",
                              "function": {"name": name, "arguments": json.dumps(args)}})
    return calls


_TOOL_NAME_ALIASES = {"shell": "run_command", "bash": "run_command", "cmd": "run_command",
                       "shell_command": "run_command", "exec": "run_command"}


def parse_attr_tag_tool_call(content):
    """Some backends emit a tool call as `<tool_call><function=NAME>
    <parameter=ARGNAME>value</parameter>...</function></tool_call>` -- the
    argument name and (for the function tag) the tool name live in the tag's
    attribute-like suffix after `=`, not as a nested child tag the way
    parse_xml_tag_tool_call expects. Observed directly from an Ornith
    backend: `<function=tool_command><parameter=command>...</parameter>
    <parameter=name>shell</parameter></function>` -- Ornith's own function
    name (`tool_command`) isn't in KNOWN_TOOL_NAMES at all; the *real*
    intended tool is named via a `name` parameter inside the call ("shell"),
    a wrapper convention distinct from every other backend this project has
    seen. Recognized here via a small alias table rather than silently
    dropping every one of Ornith's tool-call attempts as unparseable prose."""
    if not content:
        return []
    calls = []
    for block in re.finditer(r"<tool_call>(.*?)</tool_call>", content, re.DOTALL | re.IGNORECASE):
        for fm in re.finditer(r"<function=([A-Za-z0-9_]+)>(.*?)</function>", block.group(1),
                               re.DOTALL | re.IGNORECASE):
            raw_name, inner = fm.group(1), fm.group(2)
            args = {}
            for pm in re.finditer(r"<parameter=([A-Za-z0-9_]+)>\s*(.*?)\s*</parameter>", inner,
                                   re.DOTALL | re.IGNORECASE):
                args[pm.group(1)] = pm.group(2).strip()
            name = raw_name if raw_name in KNOWN_TOOL_NAMES else None
            if name is None and "name" in args:
                name = _TOOL_NAME_ALIASES.get(args.pop("name").strip().lower())
            if name is None:
                # The model invented the wrapper name directly as the function
                # tag itself (e.g. <function=bash>) instead of Ornith's usual
                # <function=tool_command><parameter=name>shell</parameter>...
                # wrapper -- same alias table, just consulted against the tag
                # rather than an inner "name" parameter.
                name = _TOOL_NAME_ALIASES.get(raw_name.strip().lower())
            if name == "run_command" and ("host" in args or "target" in args):
                # A remote host/target parameter alongside a shell/bash/cmd-style
                # name means ssh_run was actually intended -- aliasing to plain
                # run_command here would silently run the command on this Mac
                # instead of the named remote host, which is a worse failure
                # than refusing to parse at all.
                name = "ssh_run"
            if name is None:
                continue  # can't confidently resolve which real tool this was
            calls.append({"id": _unique_call_id("attr_call"), "type": "function",
                          "function": {"name": name, "arguments": json.dumps(args)}})
    return calls


def _coerce_python_literal(raw):
    if raw == "True":
        return True
    if raw == "False":
        return False
    if raw == "None":
        return None
    try:
        return float(raw) if "." in raw else int(raw)
    except ValueError:
        return raw


def _parse_python_call_args(text, pos):
    """Parse `key='value', key2=123)` starting right after the opening paren,
    tracking quote state so a comma or paren *inside* a quoted value (very
    likely in a write_file `content` argument full of code) doesn't get
    mistaken for the argument separator or the call's closing paren.
    Returns (args_dict, end_pos), or (None, pos) if the syntax doesn't hold up."""
    args = {}
    n = len(text)
    i = pos
    while i < n:
        while i < n and text[i] in " \t\r\n,":
            i += 1
        if i < n and text[i] == ")":
            return args, i + 1
        key_start = i
        while i < n and (text[i].isalnum() or text[i] == "_"):
            i += 1
        if i == key_start:
            return None, pos
        key = text[key_start:i]
        while i < n and text[i] in " \t\r\n":
            i += 1
        if i >= n or text[i] != "=":
            return None, pos
        i += 1
        while i < n and text[i] in " \t\r\n":
            i += 1
        if i >= n:
            return None, pos
        if text[i] in "'\"":
            quote = text[i]
            i += 1
            value_chars = []
            while i < n and text[i] != quote:
                if text[i] == "\\" and i + 1 < n:
                    escapes = {"n": "\n", "t": "\t", "r": "\r", "'": "'", '"': '"', "\\": "\\"}
                    value_chars.append(escapes.get(text[i + 1], text[i + 1]))
                    i += 2
                else:
                    value_chars.append(text[i])
                    i += 1
            if i >= n:
                return None, pos
            i += 1
            args[key] = "".join(value_chars)
        else:
            val_start = i
            while i < n and text[i] not in ",)":
                i += 1
            args[key] = _coerce_python_literal(text[val_start:i].strip())
    return None, pos


def parse_python_call_tool_call(content):
    """Some backends occasionally emit a tool call as Python-style function-call
    syntax instead of real structured tool_calls or this project's other
    text-adapter conventions: name(key='value', key2='value2'). Observed
    directly from a Qwen backend for read_file and run_command. Recognize it
    so a well-formed attempt doesn't get discarded as prose — the alternative
    the model reached for once several attempts of this went unrecognized was
    to give up and hallucinate an unrelated excuse ("agentic mode is
    disabled") for why nothing was happening."""
    if not content:
        return []
    calls = []
    for name in KNOWN_TOOL_NAMES:
        for m in re.finditer(rf"\b{name}\s*\(", content):
            args, _end = _parse_python_call_args(content, m.end())
            if args is None:
                continue
            calls.append({"id": _unique_call_id("pycall"), "type": "function",
                          "function": {"name": name, "arguments": json.dumps(args)}})
    return calls


BACKUP_DIR = pathlib.Path.home() / ".omlx" / "backups"


def backup_before_overwrite(target):
    """Save a timestamped copy of an existing file before it gets overwritten, so
    a bad agentic edit can always be recovered. Backups are centralized under
    ~/.omlx/backups (flattened path + timestamp) rather than left beside the
    original file, so project directories don't accumulate stray .bak files that
    an IDE might pick up. Returns the backup path, or None if there was nothing
    to back up or the backup couldn't be written (never blocks the write itself)."""
    if not target.exists():
        return None
    try:
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        flat_name = str(target).lstrip("/").replace("/", "_")
        stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
        backup_path = BACKUP_DIR / f"{flat_name}.{stamp}.bak"
        shutil.copy2(target, backup_path)
        return backup_path
    except Exception:
        return None


@contextlib.contextmanager
def caffeinate_guard():
    """Prevent idle sleep for exactly the duration of an in-flight turn (a local
    generation, including any agentic tool-call retries, can run for minutes),
    so it can't get killed by the Mac going to sleep. Scoped to the active
    request/turn only, not the whole app session, via a `caffeinate` child
    process that starts on entry and is normally stopped on exit via the
    try/finally below — but that cleanup is cooperative Python code, which a
    `kill -9` on this process skips entirely, orphaning caffeinate to block
    idle sleep forever. `-w <our own pid>` is the OS-level backstop: caffeinate
    watches that pid directly and exits on its own the moment it's gone, no
    cooperation required, so even a hard kill can't leak it.

    `-d` (prevent display sleep) is included alongside `-i` (prevent system
    idle sleep) — `-i` alone still lets the screen go dark, and on macOS a
    dark screen can trigger more aggressive background-process power
    management (GPU clock/App-Nap-style throttling) even while the system
    stays technically awake, independent of whatever's actually driving any
    given slow generation. Costs nothing to also hold off display sleep for
    a turn that's already holding off system sleep."""
    proc = None
    try:
        proc = subprocess.Popen(["caffeinate", "-d", "-i", "-w", str(os.getpid())])
    except Exception:
        proc = None
    try:
        yield
    finally:
        if proc is not None:
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except Exception:
                proc.kill()


SERVER_BUSY_LOCK_PATH = pathlib.Path.home() / ".omlx" / "mlx_server_busy.lock"


@contextlib.contextmanager
def server_busy_guard(on_wait=None):
    """Hold an OS-level exclusive advisory lock for exactly the duration of
    an in-flight chat_turn — the real model-server request/response, plus
    any agentic tool-call retries within that same turn — not the whole
    mlxcli process. An interactive REPL sitting idle at its prompt holds
    this for none of that time, only the turns where it's actually waiting
    on the model server. Anything that only checks "is an mlxcli process
    still running" (e.g. pgrep) can't distinguish that from a session that
    will sit open all day; checking this lock instead gets the real signal.

    Exclusive means a second concurrent chat_turn (interactive session and
    a headless job both firing at once) blocks here instead of both hitting
    the single-threaded model server at the same time — cleaner than
    letting them contend at the network layer. flock is released by the
    kernel the instant this process exits or dies for any reason (including
    kill -9), so it can never leak stale like a PID-file convention could.

    on_wait, if given, is called once the moment the lock turns out to
    already be held, and again every 30s while still waiting, each time
    with a short human-readable message. Without this, a caller blocked
    here looks identical to one that's genuinely hung -- zero CPU, zero
    output, no way to tell "another session is mid-turn" from "something
    is stuck" -- which cost a real multi-hour wait before the cause (an
    unrelated stuck session still holding this same lock) was found by
    manually sampling the process. mlxcli passes print; mlxgui passes its
    own thread-safe status() callback."""
    SERVER_BUSY_LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    lock_file = open(SERVER_BUSY_LOCK_PATH, "w")
    try:
        try:
            fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            if on_wait:
                on_wait("Waiting for another mlxcli/mlxgui session to finish using the model server...")
            waited = 0
            while True:
                try:
                    fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    time.sleep(1)
                    waited += 1
                    if on_wait and waited % 30 == 0:
                        on_wait(f"Still waiting on the model server lock ({waited}s so far)...")
        yield
    finally:
        fcntl.flock(lock_file, fcntl.LOCK_UN)
        lock_file.close()


def server_busy():
    """Non-blocking check: is some chat_turn currently holding the lock?
    Used by external tooling (e.g. a job queue) that wants to know whether
    the model server is actually in use right now, not just whether an
    mlxcli process happens to still be open."""
    SERVER_BUSY_LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    lock_file = open(SERVER_BUSY_LOCK_PATH, "w")
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(lock_file, fcntl.LOCK_UN)
        return False
    except BlockingIOError:
        return True
    finally:
        lock_file.close()


# Conventional filenames checked (in order) for project-local instructions —
# lets a user persist project-specific guidance ("write directly to this path,
# don't stage in Downloads first") once, instead of repeating it every prompt.
PROJECT_NOTES_FILENAMES = (".mlxcli-notes.md", "AGENTS.md", "CLAUDE.md")


def suggest_better_backend(backend, text):
    """Return a short suggestion if the current backend is known — from this
    project's own direct testing, not speculation — to be unreliable for the
    kind of request about to be sent, or None if nothing applies. Deliberately
    narrow and evidence-based, not a topic-based "this model is better for X"
    router. No cases currently apply: the previous one (Gemma's unreliable
    tool-calling on this server) no longer applies now that the Gemma backend
    has been removed. Kept as a hook for future evidence-based findings rather
    than deleted outright."""
    return None


def find_project_notes(start_dir=None):
    """Look for a project-local instructions file in the given directory
    (default: cwd), trying each conventional filename in turn. Returns
    (path, text) for the first one found, or (None, None)."""
    base = pathlib.Path(start_dir or pathlib.Path.cwd())
    for name in PROJECT_NOTES_FILENAMES:
        candidate = base / name
        if candidate.exists() and candidate.is_file():
            try:
                return candidate, candidate.read_text(errors="replace").strip()
            except Exception:
                continue
    return None, None


def compute_repo_update_status(root, timeout=10):
    """Best-effort, read-only check for new commits on a git repo's remotes.

    Fetches (doesn't merge/pull) `origin/<current-branch>` and, if present,
    `upstream/main`, then reports how many commits each is ahead of HEAD.
    Never raises — returns None on any failure (offline, not a repo, no
    remote named that way, etc.) so it can't block or break startup.

    Shared by mlxcli and mlxgui — this only checks and fetches; nothing is
    pulled, merged, or rebuilt automatically. Any actual update should be
    merged on a separate branch, built, and tested on an alternate port
    before touching a running server.
    """
    if not (root / ".git").exists():
        return None

    def run(*args):
        return subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True, text=True, timeout=timeout,
        )

    try:
        branch_proc = run("rev-parse", "--abbrev-ref", "HEAD")
        branch = branch_proc.stdout.strip()
        if branch_proc.returncode != 0 or not branch:
            return None
        # A detached HEAD (an engine built from a checked-out upstream commit, as in the update worktree) is compared with origin/main.
        label, track = ("detached HEAD", "main") if branch == "HEAD" else (branch, branch)
        remotes = set(run("remote").stdout.split())
        result = {"root": str(root), "branch": label, "remotes": {}}
        for remote, ref in (("origin", track), ("upstream", "main")):
            if remote not in remotes:
                continue
            run("fetch", remote, ref, "--quiet")            # offline: fall back to the remote ref as of the last successful fetch
            count = run("rev-list", "--count", f"HEAD..{remote}/{ref}").stdout.strip()
            if count.isdigit() and int(count) > 0:          # silent when up to date
                result["remotes"][f"{remote}/{ref}"] = int(count)
        return result if result["remotes"] else None
    except Exception:
        return None


def compute_model_update_status(repo_id, cache_path, timeout=10):
    """Best-effort, read-only check for upstream changes to a Hugging Face
    model repo since the last time this was checked from here.

    Unlike compute_repo_update_status (a local git clone with real commit
    history to diff against), an installed converted model has no such
    history -- it's a binary snapshot, not a repo. This instead caches the
    upstream HF repo's current commit sha in *cache_path* on first check,
    and reports a change on a later check that sees a different one. First
    call on a given repo_id always returns None (nothing to compare against
    yet) and just records the baseline.

    Never raises -- returns None on any failure (offline, HF API down,
    repo_id typo, etc.) so it can't block or break startup. Only reads HF's
    public repo metadata API; nothing is downloaded, converted, or applied
    automatically.
    """
    try:
        req = urllib.request.Request(
            f"https://huggingface.co/api/models/{repo_id}",
            headers={"Accept": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read())
        remote_sha = data.get("sha")
        last_modified = data.get("lastModified")
        if not remote_sha:
            return None
    except Exception:
        return None

    cached = {}
    try:
        if cache_path.exists():
            cached = json.loads(cache_path.read_text())
    except Exception:
        cached = {}

    previous = cached.get(repo_id, {})
    previous_sha = previous.get("sha")

    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cached[repo_id] = {
            "sha": remote_sha,
            "last_modified": last_modified,
            "checked": datetime.now().isoformat(),
        }
        cache_path.write_text(json.dumps(cached, indent=2))
    except Exception:
        pass

    if previous_sha and previous_sha != remote_sha:
        return {
            "repo_id": repo_id,
            "previous_sha": previous_sha,
            "new_sha": remote_sha,
            "last_modified": last_modified,
        }
    return None


def parse_pinned_model_sources(turbo_root):
    """Read tools/prepare_agentworld.py's MODELS table -- the source of truth for which upstream Hugging Face
    repo/commit each locally-converted TinyTitan/turbo model (qwen36, ornith15, katcoder, ...) was actually built
    from, since the local .gturbo files carry no such record themselves. Returns {key: (repo_id, commit)}, or {}
    if the script is missing, moved, or its MODELS table isn't the plain dict literal expected. This only reads
    and parses the file with `ast` -- it is never imported or executed."""
    import ast
    script = turbo_root / "tools" / "prepare_agentworld.py"
    try:
        tree = ast.parse(script.read_text(), filename=str(script))
    except Exception:
        return {}
    for node in ast.walk(tree):
        if (isinstance(node, ast.Assign) and len(node.targets) == 1
                and getattr(node.targets[0], "id", None) == "MODELS"):
            try:
                table = ast.literal_eval(node.value)
                if isinstance(table, dict):
                    return table
            except Exception:
                pass
            return {}
    return {}


def compute_pinned_model_update_status(repo_id, pinned_commit, timeout=10):
    """Best-effort, read-only check for whether a Hugging Face model repo's current default-branch commit differs
    from a fixed pinned commit. Unlike compute_model_update_status (which compares against whatever this tool
    happened to see on a PREVIOUS run), the baseline here is the actual commit the local build was converted
    from, so the first call already means something. Never raises -- returns None on any failure (offline, HF
    API down, repo_id typo, etc.) or when the pinned commit is still current."""
    try:
        req = urllib.request.Request(
            f"https://huggingface.co/api/models/{repo_id}",
            headers={"Accept": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read())
        latest_sha = data.get("sha")
        last_modified = data.get("lastModified")
    except Exception:
        return None
    if not latest_sha or latest_sha == pinned_commit:
        return None
    return {"repo_id": repo_id, "pinned": pinned_commit, "latest": latest_sha, "last_modified": last_modified}


# --- tokenmax per-request handoff (added 2026-09-24) --------------------------
# tokenmax is chosen JOB BY JOB: a request that starts with "/tokenmax", "using tokenmax,",
# "use/with/via/through tokenmax", or "tokenmax:" / "tokenmax," is handed to the tokenmax planner
# (Claude plans, local KAT does the small checkable work, read-only tools fetch data).
# Everything else in the same session is handled normally. Same trigger in mlxcli (REPL and --run) and mlxgui.
TOKENMAX_TRIGGER = re.compile(
    r"^\s*(?:/tokenmax\b|(?:using|use|with|via|through)\s+tokenmax\b|tokenmax\s*[:,])[\s,:;]*(?:to\s+)?(.*)$", re.I | re.S)
TOKENMAX_USAGE = (
    "usage: /tokenmax [--share|--local-only|--claude|--dry-run|--confirm] your request\n"
    "       or start any request with:  using tokenmax, <your request>\n"
    "Private by default: Claude (Haiku) sees only a redacted description; the local model and read-only tools do the "
    "small, checkable work. Add --share to let Claude see your data and take over steps the local model cannot do.\n"
    "Each run is ONE SELF-CONTAINED REQUEST, not a conversation: it does not remember anything after it finishes. If "
    "the plan needs something from you first, it will ask (once, right there); otherwise, to continue afterwards, "
    "run tokenmax again with the follow-up written out in full, the same way you'd start any new request.")


def tokenmax_request(text):
    """The request text after a tokenmax trigger (may be empty), or None if this input is not a tokenmax job."""
    m = TOKENMAX_TRIGGER.match(text or "")
    return m.group(1).strip() if m else None


TOKENMAX_BACKENDS = ("turbofieldfare-katcoder", "turbofieldfare-qwen", "turbofieldfare-ornith")


def tokenmax_argv(rest, backend=None, model=None):
    """Build the tokenmax command line for one job. Returns None when there is no request (show TOKENMAX_USAGE).

    Any TurboFieldfare backend (KAT, Qwen, Ornith) and any model on it, including the "-fast" variant, is
    passed through so tokenmax uses what mlxcli has selected. oMLX is not used by tokenmax."""
    exe = shutil.which("tokenmax") or str(pathlib.Path.home() / "bin" / "tokenmax")
    flags = []
    while rest.startswith("--"):
        head, _, tail = rest.partition(" ")
        flags.append(head)
        rest = tail.strip()
        if head in ("--backend", "--model", "--planner-model", "--exec-model") and rest:
            val, _, tail = rest.partition(" ")
            flags.append(val)
            rest = tail.strip()
    if not rest:
        return None
    if "--backend" not in flags and backend in TOKENMAX_BACKENDS:
        flags += ["--backend", backend]
    if "--model" not in flags and model and backend in TOKENMAX_BACKENDS:
        flags += ["--model", model]
    return [exe] + flags + [rest]


def model_for_tools(model):
    """The TinyTitan server's "<model>-fast" alias strips the system prompt, tool definitions and tool-call history
    from every request (chat-only speed), so an agentic request sent to it can never call a tool. Requests that
    carry tools must use the base model id; the same weights are loaded either way."""
    return model[:-len("-fast")] if model and model.endswith("-fast") else model


def ha_states_view(method, api_path, result, args, target):
    """Compact, exactly-counted view of GET /api/states for the ha_api tool (None = not applicable, use the raw JSON).

    The full state list is far larger than the model's window: a truncated dump never even reached the automations,
    so the model could not count them and started inventing scripts. Filtering and counting are done here."""
    if method != "GET" or api_path.rstrip("/") != "/api/states" or not isinstance(result, list):
        return None
    dom = str(args.get("domain") or "").strip().lower().rstrip(".")
    st = str(args.get("state") or "").strip()
    if not dom:
        counts = {}
        for e in result:
            d = str(e.get("entity_id", "")).split(".", 1)[0]
            counts[d] = counts.get(d, 0) + 1
        lines = [f"{len(result)} entities total. Per-domain counts (call again with `domain` "
                 f"(and optionally `state`) to list or count one domain):"]
        lines += [f"  {d}: {n}" for d, n in sorted(counts.items(), key=lambda kv: -kv[1])]
        return f"[source: {target}]\n" + "\n".join(lines)
    dom_all = [e for e in result if str(e.get("entity_id", "")).startswith(dom + ".")]
    match = [e for e in dom_all if not st or str(e.get("state")) == st]
    states = {}
    for e in dom_all:
        k = str(e.get("state"))
        states[k] = states.get(k, 0) + 1
    head = (f"COUNT: {len(match)} '{dom}' entities" + (f" in state '{st}'" if st else "") +
            f" (of {len(dom_all)} '{dom}' entities total; by state: "
            + ", ".join(f"{k}={v}" for k, v in sorted(states.items())) + ")")
    body = [f"{e['entity_id']}: {e.get('state')}"
            + (f" ({e['attributes']['friendly_name']})" if (e.get("attributes") or {}).get("friendly_name") else "")
            for e in sorted(match, key=lambda e: e["entity_id"])]
    text = head + "\n" + "\n".join(body)
    note = "" if len(text) <= MAX_FILE_CHARS else "\n[list truncated; the COUNT line above is complete and exact]"
    return f"[source: {target}]\n" + text[:MAX_FILE_CHARS] + note


def ha_split_states_query(api_path, args):
    """('/api/states?domain=automation&state=off') -> ('/api/states', {'domain': 'automation', 'state': 'off'}).
    Kept out of the tool schema on purpose: extra tool parameters made the model's tool-call JSON malformed under the
    server's repetition penalty. The query is applied locally; Home Assistant itself never sees it."""
    from urllib.parse import urlsplit, parse_qs
    parts = urlsplit(api_path or "")
    filt = dict(args) if isinstance(args, dict) else {}
    if parts.path.rstrip("/") == "/api/states" and parts.query:
        q = parse_qs(parts.query)
        for k in ("domain", "state"):
            if q.get(k):
                filt[k] = q[k][0]
        return "/api/states", filt
    return api_path, filt


# --- local fast lane (added 2026-09-25) ---------------------------------------------------------------------------
# Default for self-contained prompts (code, logic, formulas, explanations): ONE small no-tools call to the local model with
# the recent conversation included -- no tool prompt (~5,000-17,000 tokens), no Claude. Anything that touches files, hosts,
# URLs or the web, follows a tool-using turn, or fails the cheap checks below uses the normal full agent instead.
TOKENLOCAL_TRIGGER = re.compile(
    r"^\s*(?:/tokenlocal\b|(?:using|use|with|via|through)\s+tokenlocal\b|tokenlocal\s*[:,])[\s,:;]*(?:to\s+)?(.*)$", re.I | re.S)
TOKENLOCAL_USAGE = ("usage: /tokenlocal your request   (or start any request with:  tokenlocal, <your request>)\n"
                    "Forces the full local agent (tools, files, hosts) and skips the fast lane. Nothing is sent to Claude.")
FAST_LANE_INSTRUCTIONS = ("Answer the request below directly and completely, using the conversation above only if it is relevant. Follow any "
                          "output-format instruction exactly (for example 'just the number', 'only the code', 'two sentences'). If asked for "
                          "code, reply with exactly one fenced code block and nothing else unless told otherwise. You have no tools here. If you "
                          "are not confident you can answer correctly, or the request needs files, the web or a machine you cannot reach, begin "
                          "your whole reply with the exact token [[UNSURE]]. Factual grounding: never invent a specific-sounding name, statistic, percentage, "
                          "date, quote, link or citation to make an answer sound more complete; if you do not know something, or required input "
                          "is missing, say so plainly instead of guessing.")
_FL_RESOURCE = re.compile(r"(?:(?<=\s)|^)~?/[\w.@%+=,~-]+(?:/[\w.@%+=,~-]*)*|https?://|\b\w+\.(?:py|txt|csv|json|md|pdf|docx?|xlsx?|yaml|yml|sh|log|swift|js|ts|html|zip)\b", re.I)
_FL_ACTION = re.compile(
    r"\b(save|open|edit|delete|remove|rename|move|copy|install|uninstall|download|upload|restart|reboot|connect|ssh|ping|scan|commit|push|deploy|"
    r"email|schedule|remind me|search (the )?(web|internet|online)|google|look ?up|latest|news|weather|stock price|price of|today'?s|right now|"
    r"currently|status of|check (the|my|if|whether)|find (the|my|all)|list (the|my|all)|read (the|my)|show me (the|my)|file|files|folder|"
    r"director(y|ies)|repo(sitory)?|clipboard|screenshot|attachment|spreadsheet|home assistant|router|server|network|wifi|wi-fi|terminal|"
    r"command line|shell|processes|disk|memory usage)\b", re.I)


def tokenlocal_request(text):
    """The request after a tokenlocal trigger (may be empty), or None if this input is not a tokenlocal request."""
    m = TOKENLOCAL_TRIGGER.match(text or "")
    return m.group(1).strip() if m else None


def _last_turn_used_tools(messages):
    return any(m.get("role") == "tool" or m.get("tool_calls") for m in (messages or [])[-12:])


def fast_lane_ok(text, messages=None):
    """True when this prompt is self-contained and safe to answer with one no-tools local call."""
    t = (text or "").strip()
    if not t or t.startswith("/") or len(t) > 12000: return False
    if _FL_RESOURCE.search(t) or _FL_ACTION.search(t): return False
    if _last_turn_used_tools(messages): return False          # follow-ups to a tool-using turn keep their tools
    try:
        aliases = json.loads((pathlib.Path.home() / ".omlx" / "host_aliases.json").read_text())
    except Exception:
        aliases = {}
    for name in aliases:
        if name.isdigit():
            if re.search(rf"\b{name}\b", t) and re.search(r"\b(router|server|host|extension)\b", t, re.I): return False
        elif re.search(rf"\b{re.escape(name)}\b", t, re.I): return False
    try:
        if should_auto_enable_agentic(t, messages): return False
    except Exception:
        return False
    return True


def fast_lane_messages(messages, user):
    hist, total = [], 0
    for m in (messages or [])[-8:]:
        c = m.get("content")
        if m.get("role") in ("user", "assistant") and isinstance(c, str) and c.strip() and not m.get("tool_calls"):
            hist.append({"role": m["role"], "content": c[:2000]}); total += min(len(c), 2000)
    while total > 6000 and hist: total -= len(hist.pop(0)["content"])
    while hist and hist[0]["role"] != "user": hist.pop(0)
    return hist + [{"role": "user", "content": FAST_LANE_INSTRUCTIONS + "\n\nRequest:\n" + user}]


def fast_lane_problems(request, text, finish):
    """Cheap, deterministic reasons NOT to trust a fast-lane answer (empty list = accept)."""
    p, t = [], (text or "").strip()
    if not t: p.append("empty answer")
    if t.startswith("[[UNSURE]]"): p.append("the model said it was unsure or needs tools")
    if finish == "length": p.append("answer cut off")
    if re.search(r"(?i)\b(i (can'?t|cannot|don'?t have|do not have) (access|browse|see|open)|as an ai (language )?model)\b", t): p.append("the model said it lacks access")
    if len(t) > 200 and re.search(r"(.{20,}?)\1{3,}", t, re.S): p.append("answer looped on itself")
    if re.search(r"\bpython\b", request, re.I) and t:
        import ast
        m = re.search(r"```(?:python|py)?\n(.*?)```", t, re.S); code = m.group(1) if m else t
        try:
            ast.parse(code)
            for fn in re.findall(r"\bfunction\s+([A-Za-z_]\w*)\s*\(", request):
                if not re.search(rf"\bdef\s+{fn}\b", code): p.append(f"code does not define {fn}()")
        except SyntaxError as e:
            p.append(f"code has a syntax error ({e.msg})")
    return p


def fast_lane_complete(url, key, model, backend, messages, user):
    """(text, in_tokens, out_tokens, problems). Short answers get a 3-sample majority vote (a cheap check on arithmetic/logic)."""
    from collections import Counter
    st = load_model_settings(backend)
    def call(temp, max_tokens):
        payload = {"model": model, "messages": fast_lane_messages(messages, user), "max_tokens": max_tokens, "stream": False,
                   "temperature": temp, "top_p": st["top_p"], "top_k": st["top_k"], "repetition_penalty": st["repetition_penalty"]}
        d = api(url, key, "/v1/chat/completions", payload)
        ch = (d.get("choices") or [{}])[0]
        txt = re.sub(r"<think>.*?</think>", "", (ch.get("message") or {}).get("content") or "", flags=re.S).strip()
        u = d.get("usage") or {}
        return txt, ch.get("finish_reason"), int(u.get("prompt_tokens") or 0), int(u.get("completion_tokens") or 0)
    text, fin, tin, tout = call(0.2, 1500)
    problems = fast_lane_problems(user, text, fin)
    # A small model asked for an "exact" statistic invents a different plausible number every time it is asked (measured: five
    # different Fort Mill census figures in five tries, none correct). Specific figures in a prose answer must therefore recur in a
    # repeated answer, or the answer is rejected and the request goes to the full agent, which can look things up.
    if not problems and len(text) > 40 and "```" not in text:
        def figs(t):
            keep = lambda n: len(n.replace(",", "").split(".")[0]) >= 3
            mine = {n.replace(",", "") for n in re.findall(r"\d[\d,]*\d(?:\.\d+)?", t) if keep(n)}
            return mine - {n.replace(",", "") for n in re.findall(r"\d[\d,]*\d", user)}    # numbers the user supplied do not count
        mine = figs(text)
        if mine:
            others = []
            for _ in range(3):
                try:
                    t2, _f, i2, o2 = call(0.9, 300); others.append(figs(t2)); tin += i2; tout += o2
                except Exception:
                    pass
            # a figure the model really knows recurs; a guessed one does not. Require it in a majority of the repeat answers.
            need = 2 if len(others) >= 3 else max(1, len(others))
            if others and any(sum(n in o for o in others) < need for n in mine):
                problems = ["the specific figures did not recur when asked again (likely guessed)"]
    if not problems and len(text) <= 40:
        norm = lambda x: re.sub(r"[^\w]+", " ", x.lower()).strip()
        samples = [text]
        for _ in range(2):
            try:
                t2, _f, i2, o2 = call(0.8, 200); samples.append(t2); tin += i2; tout += o2
            except Exception:
                pass
        best, n = Counter(norm(x) for x in samples).most_common(1)[0]
        if n >= 2: text = next(x for x in samples if norm(x) == best)
        else: problems = ["repeated answers disagreed"]
    return text, tin, tout, problems


def drop_orphan_tool_messages(messages):
    """Remove `tool` messages that don't answer an earlier, still-unanswered assistant tool call.

    Trimming a long history from the front can cut between an assistant message's tool_calls and the tool results
    that follow it, leaving a `tool` message with no matching call. The server rejects the whole request with
    400 "tool result must reference one unresolved call", and because the history is resent every turn it
    keeps failing until /clear. Dropping the orphaned results (the call that produced them is already gone)
    keeps the rest of the conversation valid."""
    known, resolved, out = set(), set(), []
    for m in messages:
        if m.get("role") == "assistant":
            for tc in m.get("tool_calls") or []:
                known.add(tc.get("id"))
        elif m.get("role") == "tool":
            tid = m.get("tool_call_id")
            if tid not in known or tid in resolved:
                continue
            resolved.add(tid)
        out.append(m)
    return out


# In-turn context compaction (shared by mlxcli and mlxgui). Tool rounds more recent than
# KEEP_RECENT_TOOL_ROUNDS are never touched; tool-call arguments shorter than
# COMPACT_MIN_CHARS aren't worth replacing.
KEEP_RECENT_TOOL_ROUNDS = 4
COMPACT_MIN_CHARS = 300
COMPACT_EXCERPT_CHARS = 500


def _compacted_result(content):
    """An older in-turn tool result, shrunk but not erased: its first and last
    COMPACT_EXCERPT_CHARS chars under a header. Replacing results with a bare
    "[compacted: N chars omitted -- already applied earlier this turn]" threw away
    the evidence itself (a log's contents, a listing), so the model either re-ran
    the same reads over and over or answered from a fuzzy memory of them (seen
    2026-09-28: repeated ls/find right after each compaction, and invented log
    timestamps). None when there's nothing worth shrinking."""
    if len(content) <= 2 * COMPACT_EXCERPT_CHARS + 600:
        return None
    omitted = len(content) - 2 * COMPACT_EXCERPT_CHARS
    return (f"[compacted to save context: this earlier result was {len(content):,} chars; its first and last "
            f"{COMPACT_EXCERPT_CHARS} are kept below. Re-run the tool only if you need the omitted middle.]\n"
            + content[:COMPACT_EXCERPT_CHARS] + f"\n[... {omitted:,} chars omitted ...]\n"
            + content[-COMPACT_EXCERPT_CHARS:])


def compact_turn_messages(messages, turn_start_index, keep_recent_rounds=KEEP_RECENT_TOOL_ROUNDS):
    """FIFO-compress older tool interactions within the CURRENT turn only.

    Never touches messages before turn_start_index (prior-turn history is
    trim()'s job, between turns). Within this turn, replaces the bulky
    content of tool calls/results older than the most recent
    keep_recent_rounds with a short marker, preserving the record that the
    action happened (so the model doesn't re-do it — the separate
    seen_tool_calls dedup cache also guards against that independently)
    without carrying its full payload. Compresses, does not delete: message
    order and count are unchanged. Returns (messages, chars_saved).
    """
    unit_starts = [
        i for i in range(turn_start_index, len(messages))
        if messages[i].get("role") == "assistant" and messages[i].get("tool_calls")
    ]
    if len(unit_starts) <= keep_recent_rounds:
        return messages, 0

    to_compact_starts = unit_starts[:-keep_recent_rounds]
    chars_saved = 0
    new_messages = list(messages)

    for start in to_compact_starts:
        assistant_msg = new_messages[start]
        tool_call_ids = {tc.get("id") for tc in (assistant_msg.get("tool_calls") or [])}

        compacted_calls = []
        assistant_changed = False
        for tc in assistant_msg.get("tool_calls") or []:
            args_str = tc.get("function", {}).get("arguments") or ""
            if len(args_str) > COMPACT_MIN_CHARS and '"__compacted__"' not in args_str:
                tc = copy.deepcopy(tc)
                fn_name = tc.get("function", {}).get("name", "?")
                tc["function"]["arguments"] = compact_call_arguments(
                    fn_name, args_str, f"{len(args_str):,} chars omitted — already applied earlier this turn")
                chars_saved += len(args_str) - len(tc["function"]["arguments"])
                assistant_changed = True
            compacted_calls.append(tc)
        if assistant_changed:
            assistant_msg = dict(assistant_msg)
            assistant_msg["tool_calls"] = compacted_calls
            new_messages[start] = assistant_msg

        for i in range(start + 1, len(new_messages)):
            m = new_messages[i]
            if m.get("role") != "tool":
                break  # end of this unit's tool results
            if m.get("tool_call_id") not in tool_call_ids:
                continue
            content = m.get("content") or ""
            if not content.startswith("[compacted"):
                shrunk = _compacted_result(content)
                if shrunk is not None:
                    new_m = dict(m)
                    new_m["content"] = shrunk
                    new_messages[i] = new_m
                    chars_saved += len(content) - len(shrunk)

    return new_messages, chars_saved


SUPERSEDED_MARKER = "[superseded to save context:"


def compact_superseded_results(messages, turn_start_index):
    """Replace this turn's tool payloads that a LATER one in the same turn made
    redundant, so no live information is lost -- unlike compact_turn_messages,
    which shrinks by age. Superseded means:
      - a read_file result when the same path+range was read again later, or the
        path was successfully written later (the old contents are stale);
      - a write_file call's content when the same path was successfully written again;
      - any tool result byte-identical to a later call of the same tool+args.
    Run only when compaction is already due: every edit to an earlier message
    invalidates the server's prompt cache (it only reuses an exact prefix), so
    freeing extra room in the same pass as the age-based compaction means the next
    few rounds append without edits and hit the cache. Returns (messages, chars_saved)."""
    calls = []  # (assistant_index, call_index, fn, args_str, args, result_index, result_content)
    for i in range(turn_start_index, len(messages)):
        m = messages[i]
        if m.get("role") != "assistant" or not m.get("tool_calls"):
            continue
        for ci, tc in enumerate(m["tool_calls"]):
            fn = tc.get("function", {}).get("name", "")
            args_str = tc.get("function", {}).get("arguments") or ""
            try:
                args = json.loads(args_str)
            except (TypeError, ValueError):
                args = {}
            if not isinstance(args, dict):
                args = {}
            result_index = next((j for j in range(i + 1, len(messages))
                                 if messages[j].get("role") == "tool"
                                 and messages[j].get("tool_call_id") == tc.get("id")), None)
            result = messages[result_index].get("content") or "" if result_index is not None else ""
            calls.append((i, ci, fn, args_str, args, result_index, result))

    def read_key(args):
        return (args.get("path"), args.get("start_line"), args.get("tail_lines"))

    new_messages = list(messages)
    chars_saved = 0
    for n, (ai, ci, fn, args_str, args, ri, result) in enumerate(calls):
        later = calls[n + 1:]
        path = args.get("path")
        stale_result = stale_args = None
        if fn == "read_file" and path:
            if any(l[2] == "read_file" and read_key(l[4]) == read_key(args) for l in later):
                stale_result = "the same file was read again later in this turn; use that newer copy"
            elif any(l[2] == "write_file" and l[4].get("path") == path and not tool_result_failed(l[6])
                     for l in later):
                stale_result = "this file was rewritten later in this turn, so these contents are out of date"
        elif fn == "write_file" and path and not tool_result_failed(result):
            if any(l[2] == "write_file" and l[4].get("path") == path and not tool_result_failed(l[6])
                   for l in later):
                stale_args = "this file was written again later in this turn; that later write is current"
        if stale_result is None and result and any(l[2] == fn and l[3] == args_str and l[6] == result
                                                   for l in later):
            stale_result = "a later identical call in this turn returned this exact same result"
        if stale_result and ri is not None and len(result) > COMPACT_MIN_CHARS \
                and not result.startswith(("[compacted", SUPERSEDED_MARKER)):
            marker = f"{SUPERSEDED_MARKER} {stale_result}.]"
            new_messages[ri] = dict(new_messages[ri], content=marker)
            chars_saved += len(result) - len(marker)
        if stale_args and len(args_str) > COMPACT_MIN_CHARS and '"__compacted__"' not in args_str:
            msg = dict(new_messages[ai])
            msg["tool_calls"] = list(msg["tool_calls"])
            tc = copy.deepcopy(msg["tool_calls"][ci])
            tc["function"]["arguments"] = compact_call_arguments(fn, args_str, stale_args)
            msg["tool_calls"][ci] = tc
            new_messages[ai] = msg
            chars_saved += len(args_str) - len(tc["function"]["arguments"])
    return new_messages, chars_saved


def compact_turn_for_context(messages, turn_start_index):
    """The in-turn compaction pass both apps run once the prompt nears the context
    limit: age-based shrinking (compact_turn_messages) plus superseded payloads
    (compact_superseded_results) in ONE pass, so the prompt prefix changes once
    per batch instead of once per round. Returns (messages, chars_saved)."""
    messages, saved_by_age = compact_turn_messages(messages, turn_start_index)
    messages, saved_superseded = compact_superseded_results(messages, turn_start_index)
    return messages, saved_by_age + saved_superseded


# ---------------------------------------------------------------------------
# Apple Intelligence side chat (on-device Foundation Models via ~/bin/applefm)
# ---------------------------------------------------------------------------
APPLEFM_BIN = os.path.expanduser("~/bin/applefm")
APPLE_REFINER_INSTRUCTIONS = (
    "You help the user write prompts for another, larger local LLM. Rewrite the user's request as one precise, "
    "self-contained prompt. Preserve every literal filename, path, number, quoted value and requested output format. "
    "Do not invent facts or inputs. If the user then asks for changes, apply them and return the full updated prompt. "
    "If the user asks a question about the prompt instead, answer briefly. "
    "When you give a prompt, output only the prompt text with no preamble."
)


class AppleChat:
    """Persistent side-chat session with Apple's on-device model."""

    def __init__(self, instructions=APPLE_REFINER_INSTRUCTIONS):
        self.instructions = instructions
        self.proc = None
        self._lock = threading.Lock()

    def _rpc(self, req, timeout=90):
        import json as _json
        with self._lock:
            if self.proc is None or self.proc.poll() is not None:
                if not os.access(APPLEFM_BIN, os.X_OK):
                    raise RuntimeError("applefm helper not installed")
                self.proc = subprocess.Popen(
                    [APPLEFM_BIN], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL, text=True, bufsize=1,
                )
            self.proc.stdin.write(_json.dumps(req) + "\n")
            self.proc.stdin.flush()
            box = {}
            t = threading.Thread(target=lambda: box.setdefault("line", self.proc.stdout.readline()), daemon=True)
            t.start()
            t.join(timeout)
            if "line" not in box or not box["line"]:
                self.close()
                raise RuntimeError("Apple Intelligence did not respond")
            resp = _json.loads(box["line"])
        if not resp.get("ok"):
            raise RuntimeError(resp.get("error", "unknown error"))
        return resp.get("text", "")

    def available(self):
        try:
            return self._rpc({"op": "status"}, timeout=15) == "available"
        except Exception:
            return False

    def reset(self):
        self._rpc({"op": "reset", "instructions": self.instructions})

    def ask(self, prompt, first=False):
        req = {"op": "ask", "prompt": prompt, "instructions": self.instructions}
        if first:
            self.reset()
        return self._rpc(req).strip()

    def close(self):
        p, self.proc = self.proc, None
        if p and p.poll() is None:
            try:
                p.kill()
            except Exception:
                pass


def apple_context(messages, extras=None, budget=6000):
    """Compact view of the mlx session for Apple's ~4K-token window.

    Header (working dir, model, system-prompt opening) + recent turns: the newest turn
    is kept nearly whole, older turns shrink to a one-line gist, newest first until the budget is spent.
    """
    extras = extras or {}
    head = []
    for label in ("model", "cwd", "files"):
        if extras.get(label):
            head.append(f"{label}: {extras[label]}")
    if messages and messages[0].get("role") == "system":
        sysmsg = " ".join((messages[0].get("content") or "").split())[:400]
        if sysmsg:
            head.append(f"main model's system prompt (start): {sysmsg}")
    header = "\n".join(head)
    room = max(0, budget - len(header) - 200)
    turns = [m for m in messages if m.get("role") in ("user", "assistant") and (m.get("content") or "").strip()]
    out = []
    for n, m in enumerate(reversed(turns)):
        text = " ".join((m["content"]).split()) if n else m["content"].strip()
        limit = 2200 if n == 0 else (900 if n == 1 else 250)
        if len(text) > limit:
            text = text[:limit] + " ..."
        entry = f"[{m['role']}] {text}"
        if len(entry) > room:
            break
        room -= len(entry) + 1
        out.append(entry)
        if len(out) >= 12:
            break
    body = "\n".join(reversed(out))
    return (header + "\n\n" if header else "") + body


def apple_refine_first_prompt(raw_request, context=""):
    """Build the opening message for an Apple refinement chat (context pre-budgeted by apple_context)."""
    if context:
        return (f"Session context (oldest first; use it to resolve references like 'these' or 'above' and to give "
                f"advice that fits what the user is doing):\n{context[-6500:]}\n\n"
                f"Request to turn into a precise prompt:\n{raw_request}")
    return f"Request to turn into a precise prompt:\n{raw_request}"
