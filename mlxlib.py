"""Shared logic for mlxcli and mlxgui.

Both tools talk to the same local model servers (omlx, turbofieldfare,
turbofieldfare-qwen) and need the same answers to "is this an agentic
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
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime
from logging.handlers import RotatingFileHandler

DEFAULT_DIR_CONFIG_PATH = pathlib.Path.home() / ".omlx" / "default_dir.txt"
DEFAULT_DIR_FALLBACK = pathlib.Path.home() / "LocalAI"


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
MAX_RESPONSE_TOKENS = 2500
MAX_CONTEXT_CHARS = 60000
MAX_TOOL_STEPS = 16

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
        "description": "Read a text file (truncated to 8000 chars).",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string"}}, "required": ["path"]}}},
    {"type": "function", "function": {
        "name": "write_file",
        "description": "Write a new file or overwrite an existing file. Shows the user a preview and asks for approval before writing.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string"},
            "content": {"type": "string"}}, "required": ["path", "content"]}}},
]


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


def is_code_request(text):
    lowered = text.lower()
    code_terms = (
        "script", "code", "program", "python", "bash", "shell", "function",
        "create a", "write a", "generate a",
    )
    return any(term_present(term, lowered) for term in code_terms)


def requires_agentic_execution(text):
    """Identify requests where a prose-only answer would falsely imply completion."""
    lowered = (text or "").lower()
    action_terms = (
        "create", "write", "save", "run", "execute", "test", "export",
        "generate files", "place the files", "put the files", "verify",
        "confirm that", "list its byte size", "share the final files",
        "open", "show", "view", "look at", "check", "list", "read",
        "update", "modify", "edit", "add", "change", "build", "set up",
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


def should_auto_enable_agentic(text, messages=None):
    """Detect requests that require local tools without changing the user's mode setting."""
    lowered = (text or "").lower()
    local_target = (
        bool(re.search(r"(?:^|\s)(?:~|/|\./|\.\./)[^\s]+", lowered))
        or any(term_present(term, lowered) for term in (
            "downloads", "download folder", "local file", "local folder", "filesystem",
            "file system", "directory", "folder", "repository", "repo", "working tree",
            "/users/",
            "spreadsheet", "workbook", "excel",
            "script", "project", "app", "application", "program", "python", "database",
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
        "run": target and any(term_present(term, lowered) for term in ("run", "execute", "test")),
        "verify": target and any(term_present(term, lowered) for term in ("verify", "inspect", "confirm", "byte size", "exists")),
    }


def tool_result_failed(result):
    lowered = (result or "").lower()
    match = re.search(r"exit_code=(-?\d+)", lowered)
    return (match and int(match.group(1)) != 0) or any(
        term in lowered for term in ("error:", "not found", "timed out", "user declined")
    )


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
    p = pathlib.Path(raw).expanduser()
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


KNOWN_TOOL_NAMES = ("run_command", "read_file", "write_file", "python_interpreter")


def _unique_call_id(prefix):
    """A globally-unique synthetic tool_call id, not just unique within one
    parser invocation. Using a plain per-call-list counter (call_0, call_1, ...)
    meant the same id recurred across different turns of the same
    conversation, since each parser call restarts counting from 0 — and the
    server's own history validator rejects a conversation containing a
    repeated tool_call id with "invalid or duplicate historical tool call",
    confirmed directly from a live run."""
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


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


# Conventional filenames checked (in order) for project-local instructions —
# lets a user persist project-specific guidance ("write directly to this path,
# don't stage in Downloads first") once, instead of repeating it every prompt.
PROJECT_NOTES_FILENAMES = (".mlxcli-notes.md", "AGENTS.md", "CLAUDE.md")


def suggest_better_backend(backend, text):
    """Return a short suggestion if the current backend is known — from this
    project's own direct testing, not speculation — to be unreliable for the
    kind of request about to be sent, or None if nothing applies. Deliberately
    narrow and evidence-based: a topic-based "this model is better for X"
    router isn't something we have real grounds to build, but "Gemma's
    tool-call decoder reliably fails on this server for agentic requests" is a
    confirmed, reproduced finding, not a guess. This never switches anything
    automatically — local model switches cost real time (stopping one server,
    loading another), so the choice stays with the user; this only surfaces
    the suggestion at the moment it'd actually matter."""
    if backend == "turbofieldfare" and requires_agentic_execution(text):
        return ("Gemma has shown unreliable tool-calling on this local server for agentic "
                "requests (confirmed: tool-call parsing failures during testing). "
                "Consider /backend turbofieldfare-qwen for this one.")
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
