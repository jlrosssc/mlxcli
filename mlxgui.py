#!/usr/bin/env python3
"""Lightweight Tkinter chat GUI for oMLX with Markdown file import."""
import difflib
import json
import os
import pathlib
import queue
import re
import shlex
import shutil
import subprocess
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
import urllib.error
import urllib.parse
import urllib.request
import zipfile
import hashlib
import html
from html.parser import HTMLParser
from datetime import datetime
from xml.sax.saxutils import escape
from tkinter import filedialog, messagebox, simpledialog, ttk
try:
    from docx_export import markdown_to_docx as formatted_markdown_to_docx
except ImportError:
    formatted_markdown_to_docx = None
from mlxlib import (
    DOWNLOADS, MAX_FILE_CHARS, MAX_HISTORY_TURNS, MAX_RESPONSE_TOKENS,
    MAX_CONTEXT_CHARS, MAX_TOOL_STEPS, CONVERTIBLE, REVIEWABLE_TEXT, TOOLS,
    is_code_request, should_auto_enable_agentic as gui_should_auto_enable_agentic,
    requires_agentic_execution as gui_requires_agentic_execution,
    execution_contract as gui_execution_contract, tool_result_failed as gui_tool_failed,
    resolve_output_path, normalize_tool_name as gui_normalize_tool_name,
    infer_command_cwd as gui_infer_command_cwd, term_present as gui_term_present,
    backup_before_overwrite, find_project_notes, PROJECT_NOTES_FILENAMES,
    detect_repetition_loop as gui_detect_repetition_loop,
    parse_bare_json_tool_call as gui_parse_bare_json_tool_call,
    parse_xml_tag_tool_call as gui_parse_xml_tag_tool_call,
    parse_python_call_tool_call as gui_parse_python_call_tool_call,
    DEFAULT_REPETITION_PENALTY, _unique_call_id as gui_unique_call_id,
)


SETTINGS = pathlib.Path.home() / ".omlx" / "settings.json"
SYSTEM_PROMPT_PATH = pathlib.Path.home() / ".omlx" / "mlx_system_prompt.txt"
GUI_DEFAULTS_PATH = pathlib.Path.home() / ".omlx" / "mlxgui_defaults.json"
SESSIONS_DIR = pathlib.Path.home() / ".omlx" / "sessions"
OMLX_BIN = pathlib.Path.home() / ".omlx" / "bin" / "omlx"
BACKEND_PATH = pathlib.Path.home() / ".omlx" / "mlx_backend.txt"
MODELS_ROOT = pathlib.Path.home() / "Models"
TURBO_ROOT = MODELS_ROOT / "turbo-fieldfare"
TURBO_SERVER_BIN = TURBO_ROOT / ".build" / "release" / "TurboFieldfareServer"
TURBO_MODEL_DIR = TURBO_ROOT / "scratch" / "gemma4.gturbo"
TURBO_SERVER_LOG = pathlib.Path.home() / ".omlx" / "turbofieldfare-server.log"
TURBO_QWEN_ROOT = MODELS_ROOT / "turbo-fieldfare-qwen"
TURBO_QWEN_SERVER_BIN = TURBO_QWEN_ROOT / ".build" / "release" / "TurboFieldfareServer"
TURBO_QWEN_MODEL_DIR = TURBO_QWEN_ROOT / "scratch" / "qwen36.gturbo"
TURBO_QWEN_SERVER_LOG = pathlib.Path.home() / ".omlx" / "turbofieldfare-qwen-server.log"
TURBO_STATUS_APP = pathlib.Path.home() / "Applications" / "Turbo Status.app"
RESOURCE_REFRESH_MS = 5000
RAG_MAX_FILES = 64
RAG_MAX_CHUNKS = 4
RAG_CHUNK_CHARS = 1800
RAG_COMPARE_FILE_LIMIT = 12
RAG_COMPARE_CHARS = 700
RAG_CACHE_DIRNAME = ".mlxgui_rag_cache"
RAG_CACHE_INDEX = "index.json"
URL_MAX_FETCHES = 2
URL_MAX_CHARS = 12000
CONVERT_MODES = ("auto", "all", "off")
SUPPORTED_BACKENDS = ("omlx", "turbofieldfare", "turbofieldfare-qwen")
DEFAULT_SYSTEM = (
    "You are a concise assistant running locally on the user's Mac.\n"
    "Do not reveal hidden reasoning, internal planning, or chain-of-thought.\n"
    "Treat the user's supplied text, numbers, filenames, and codes as authoritative literal data. "
    "Do not silently correct, normalize, truncate, expand, or substitute them unless explicitly asked.\n"
    "When a request refers to content 'below', 'above', or 'in the pasted list', verify that the content is actually present. "
    "If required input is missing, say exactly what is missing instead of guessing.\n"
    "When a request names a category or target (e.g. 'my photos', 'my documents', 'my files') without stating where it lives, "
    "ask which folder or path to use. Do not silently reuse a location mentioned in an earlier, unrelated request.\n"
    "For file and spreadsheet tasks, use the named local resource when available, preserve text-formatted numeric values, "
    "and follow requested columns, order, and output shape exactly. Distinguish source data from user-provided input data.\n"
    "For exact-match tasks, compare the complete requested key and apply only the explicitly stated normalization rules; "
    "never match a convenient suffix or a similar-looking value.\n"
    "For local file listings or size rankings, use a filename-safe approach such as find with stat and sort by bytes; "
    "do not parse ls output with positional awk fields because filenames may contain spaces.\n"
    "For file metadata such as creation dates, use stat on the exact files and report the filesystem values; "
    "do not infer dates from filenames or conversation text.\n"
    "For code requests, give a short practical note and the final code block only unless the user asks for explanation.\n"
    "Save created files in ~/Downloads unless the user gives another path.\n"
    "For local file tasks, use the available tools and never claim a file was created, run, inspected, verified, or shared without tool evidence.\n"
    "When running a local script against an absolute input file, use that input file's directory as the working directory unless another one is explicitly requested. Resolve relative outputs beside the input file.\n"
    "For multi-file creation, use write_file once per file; do not use python_interpreter to embed filesystem writes.\n"
    "Keep replies brief and avoid repeating large imported text unless asked.\n"
    "Respect local LLM memory limits: summarize when possible, use only relevant imported context, and suggest Clear when old context is no longer needed.\n"
    "Be direct, respectful, and practical."
)
PRESET_SYSTEMS = {
    "Built-in Default": DEFAULT_SYSTEM,
    "Concise, Efficient Agent Conversation": (
        "You are a concise, efficient assistant running locally on the user's Mac.\n"
        "Do not reveal hidden reasoning, internal planning, or chain-of-thought.\n"
        "Answer with the smallest complete response that solves the request.\n"
        "For code requests, give the final code block first with at most one short note.\n"
        "Avoid restating the user's request or repeating large imported text.\n"
        "Use bullets only when they improve scanning.\n"
        "Ask a clarifying question only when the missing detail blocks useful work.\n"
        "Respect local LLM memory limits: use only relevant imported context, summarize instead of quoting, and suggest Clear when old context is no longer needed.\n"
        "Save created files in ~/Downloads unless the user gives another path.\n"
        "Be direct, respectful, and practical."
    ),
    "Code-Focused": (
        "You are a concise coding assistant running locally on the user's Mac.\n"
        "Do not reveal hidden reasoning, internal planning, or chain-of-thought.\n"
        "For code requests, provide runnable code and only the explanation needed to use it.\n"
        "Prefer simple, efficient standard-library solutions unless a dependency is clearly better.\n"
        "Mention important assumptions and edge cases briefly.\n"
        "Respect local LLM memory limits and avoid repeating large context.\n"
        "Save created files in ~/Downloads unless the user gives another path."
    ),
    "Document Drafting": (
        "You are a concise writing assistant running locally on the user's Mac.\n"
        "Do not reveal hidden reasoning, internal planning, or chain-of-thought.\n"
        "Produce clean Markdown suitable for DOCX export.\n"
        "Use clear headings, short paragraphs, and practical formatting.\n"
        "Avoid long quoted source text unless the user asks for it.\n"
        "Respect local LLM memory limits by summarizing imported material.\n"
        "Save created files in ~/Downloads unless the user gives another path."
    ),
}
CODE_REQUEST_SYSTEM = (
    "This turn is a code/script request. Do not include hidden reasoning, planning, or analysis. "
    "Start with the final code block. After the code, include only requested output or one short usage note. "
    "If the user asks to list generated values, list them after the code without explaining your process."
)
RAG_USE_SYSTEM = (
    "Local reference excerpts from the selected chat RAG folder are included for this turn. "
    "Use those excerpts as your available file context. Do not say you cannot access files or folders directly "
    "if RAG excerpts are present. Do not ask the user to paste or re-share documents that are already represented "
    "in the provided RAG excerpts."
)
URL_USE_SYSTEM = (
    "Fetched web page content is included for this turn. Use only the retrieved page content for URL summary "
    "or title requests. If URL retrieval fails, say so explicitly and do not guess."
)


def load_cfg():
    url = os.environ.get("OMLX_URL", "http://localhost:8000")
    key = os.environ.get("OMLX_API_KEY", "")
    if not key and SETTINGS.exists():
        try:
            settings = json.loads(SETTINGS.read_text())
            key = settings.get("auth", {}).get("api_key", "")
            port = settings.get("server", {}).get("port", 8000)
            if "OMLX_URL" not in os.environ:
                url = f"http://localhost:{port}"
        except Exception:
            pass
    return url, key


def load_backend():
    selected = os.environ.get("MLXCLI_BACKEND", "").strip().lower()
    if selected in SUPPORTED_BACKENDS:
        return selected
    try:
        selected = BACKEND_PATH.read_text().strip().lower()
        if selected in SUPPORTED_BACKENDS:
            return selected
    except Exception:
        pass
    return "omlx"


def save_backend(backend):
    BACKEND_PATH.parent.mkdir(parents=True, exist_ok=True)
    BACKEND_PATH.write_text(backend + "\n")


def is_turbo_backend(backend):
    return backend in ("turbofieldfare", "turbofieldfare-qwen")


def backend_label(backend):
    return {
        "omlx": "oMLX",
        "turbofieldfare": "TurboFieldfare (Gemma 4)",
        "turbofieldfare-qwen": "TurboFieldfare Qwen (Qwen 3.6)",
    }.get(backend, backend)


def backend_description(backend):
    return {
        "omlx": "General / Flexible",
        "turbofieldfare": "Gemma 4 / General",
        "turbofieldfare-qwen": "Qwen 3.6 / Coding",
    }.get(backend, "")


def backend_paths(backend):
    if backend == "turbofieldfare-qwen":
        return {
            "root": TURBO_QWEN_ROOT,
            "server_bin": TURBO_QWEN_SERVER_BIN,
            "model_dir": TURBO_QWEN_MODEL_DIR,
            "log": TURBO_QWEN_SERVER_LOG,
            "url": "http://127.0.0.1:8081",
        }
    if backend == "turbofieldfare":
        return {
            "root": TURBO_ROOT,
            "server_bin": TURBO_SERVER_BIN,
            "model_dir": TURBO_MODEL_DIR,
            "log": TURBO_SERVER_LOG,
            "url": "http://127.0.0.1:8080",
        }
    return {}


def load_backend_cfg(backend):
    if backend == "omlx":
        url, key = load_cfg()
        return url, key
    paths = backend_paths(backend)
    return os.environ.get(
        "TURBOFIELDFARE_QWEN_URL" if backend == "turbofieldfare-qwen" else "TURBOFIELDFARE_URL",
        paths["url"],
    ), os.environ.get("TURBOFIELDFARE_API_KEY", "")


def load_system_prompt():
    try:
        saved = SYSTEM_PROMPT_PATH.read_text().strip()
        if saved:
            return saved
    except Exception:
        pass
    return DEFAULT_SYSTEM


def save_system_prompt(text):
    SYSTEM_PROMPT_PATH.parent.mkdir(parents=True, exist_ok=True)
    SYSTEM_PROMPT_PATH.write_text(text.strip() + "\n")


def load_gui_defaults():
    defaults = {"convert_mode": "auto"}
    try:
        saved = json.loads(GUI_DEFAULTS_PATH.read_text())
        if saved.get("convert_mode") in CONVERT_MODES:
            defaults["convert_mode"] = saved["convert_mode"]
    except Exception:
        pass
    return defaults


def save_gui_defaults(defaults):
    GUI_DEFAULTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    GUI_DEFAULTS_PATH.write_text(json.dumps(defaults, indent=2) + "\n")


def request(url, key, path, payload=None):
    return urllib.request.Request(
        url.rstrip("/") + path,
        data=json.dumps(payload).encode() if payload is not None else None,
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {key}"},
    )


def api(url, key, path, payload=None):
    with urllib.request.urlopen(request(url, key, path, payload), timeout=900) as resp:
        return json.load(resp)


def server_up(url, key):
    try:
        api(url, key, "/v1/models")
        return True
    except Exception:
        return False


def stop_omlx():
    if not OMLX_BIN.exists():
        return False
    try:
        result = subprocess.run([str(OMLX_BIN), "stop"], capture_output=True, text=True, timeout=30)
    except Exception:
        return False
    output = (result.stdout or "") + (result.stderr or "")
    return result.returncode == 0 and ("stop" in output.lower() or not output.strip())


def stop_turbo(backend):
    paths = backend_paths(backend)
    if not paths:
        return False
    pattern = str(paths["model_dir"])
    try:
        first = subprocess.run(["pkill", "-f", pattern], capture_output=True, text=True, timeout=15)
        time.sleep(1)
        probe = subprocess.run(["pgrep", "-f", pattern], capture_output=True, text=True, timeout=15)
        if probe.returncode == 0:
            second = subprocess.run(["pkill", "-9", "-f", pattern], capture_output=True, text=True, timeout=15)
            return second.returncode == 0
        return first.returncode == 0
    except Exception:
        return False


def stop_other_backend(target_backend, status):
    if target_backend == "omlx":
        stopped = stop_turbo("turbofieldfare") or stop_turbo("turbofieldfare-qwen")
        status("Stopped TurboFieldfare" if stopped else "TurboFieldfare was not running")
    elif target_backend == "turbofieldfare":
        stopped_omlx = stop_omlx()
        stopped_qwen = stop_turbo("turbofieldfare-qwen")
        status("Stopped oMLX and Turbo Qwen" if stopped_omlx or stopped_qwen else "Other backends were not running")
    else:
        stopped_omlx = stop_omlx()
        stopped_gemma = stop_turbo("turbofieldfare")
        status("Stopped oMLX and Turbo Gemma" if stopped_omlx or stopped_gemma else "Other backends were not running")


def ensure_turbo_status_app():
    try:
        if TURBO_STATUS_APP.exists():
            subprocess.run(["open", "-g", str(TURBO_STATUS_APP)], capture_output=True, text=True)
    except Exception:
        pass


def ensure_server(backend, url, key, status):
    if is_turbo_backend(backend):
        ensure_turbo_status_app()
    if server_up(url, key):
        return True
    if is_turbo_backend(backend):
        paths = backend_paths(backend)
        if not paths["server_bin"].exists():
            status(f"{backend_label(backend)} server is not built")
            return False
        if not paths["model_dir"].exists():
            status(f"{backend_label(backend)} model is not installed")
            return False
        status(f"Starting {backend_label(backend)}...")
        paths["log"].parent.mkdir(parents=True, exist_ok=True)
        log_handle = open(paths["log"], "a")
        log_handle.write(f"\n=== launch {datetime.now().isoformat()} ===\n")
        log_handle.flush()
        subprocess.Popen(
            [str(paths["server_bin"]), "--model", str(paths["model_dir"]), "--port", url.rsplit(":", 1)[-1]],
            cwd=paths["root"], stdout=log_handle, stderr=log_handle,
        )
    else:
        status("Launching oMLX...")
        subprocess.run(["open", "-a", "oMLX"], capture_output=True, text=True)
        if OMLX_BIN.exists():
            status("Starting oMLX server...")
            subprocess.run([str(OMLX_BIN), "start", "--timeout", "60"], capture_output=True, text=True)
    for _ in range(30):
        time.sleep(2)
        if server_up(url, key):
            status(f"{backend_label(backend)} is up")
            return True
    status(f"{backend_label(backend)} server not reachable")
    return False


def list_models(url, key):
    data = api(url, key, "/v1/models")
    return [m["id"] for m in data.get("data", [])]


def model_tags(model_id):
    name = model_id.lower()
    tags = []
    if "coder" in name or "code" in name:
        tags.append("Coding")
    if "gpt-oss" in name or "reason" in name or "r1" in name:
        tags.append("Reasoning")
    if "vision" in name or "vl" in name or "llava" in name:
        tags.append("Vision")
    if "instruct" in name or "chat" in name or "qwen" in name or "llama" in name or "gemma" in name:
        tags.append("General")
    if "creative" in name or "story" in name or "write" in name:
        tags.append("Creative")
    if not tags:
        tags.append("General")
    # keep order stable, remove duplicates
    return list(dict.fromkeys(tags))


def model_label(model_id):
    return f"{model_id} [{', '.join(model_tags(model_id))}]"


def resolve_model_id(selected, mapping):
    if selected in mapping:
        return mapping[selected]
    known_ids = list(mapping.values())
    for model_id in known_ids:
        if selected == model_id or selected.startswith(model_id + " ["):
            return model_id
    # Defensive fallback if the visible text was rewritten repeatedly.
    cleaned = re.sub(r"\s+\[[^\]]+\]\s*$", "", selected).strip()
    for model_id in known_ids:
        if cleaned == model_id or cleaned.startswith(model_id + " ["):
            return model_id
    return cleaned or selected


class HTMLTextExtractor(HTMLParser):
    def __init__(self):
        super().__init__()
        self.in_title = False
        self.skip_depth = 0
        self.title_parts = []
        self.text_parts = []

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag == "title":
            self.in_title = True
        if tag in {"script", "style", "noscript"}:
            self.skip_depth += 1

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag == "title":
            self.in_title = False
        if tag in {"script", "style", "noscript"} and self.skip_depth:
            self.skip_depth -= 1
        if tag in {"p", "div", "section", "article", "li", "br", "h1", "h2", "h3", "h4"}:
            self.text_parts.append("\n")

    def handle_data(self, data):
        if self.skip_depth:
            return
        text = html.unescape(data or "")
        if self.in_title:
            self.title_parts.append(text)
        self.text_parts.append(text)

    def title(self):
        return re.sub(r"\s+", " ", "".join(self.title_parts)).strip()

    def text(self):
        return re.sub(r"\s+", " ", "".join(self.text_parts)).strip()


def detect_urls(text):
    found = re.findall(r"https?://[^\s)>\"']+", text or "")
    return list(dict.fromkeys(found))[:URL_MAX_FETCHES]


def fetch_url_context_via_safari(url):
    script = f'''
set targetUrl to "{url.replace('"', '\\"')}"
tell application "Safari"
    activate
    set newDoc to make new document with properties {{URL:targetUrl}}
    repeat 60 times
        delay 0.5
        try
            set readyState to do JavaScript "document.readyState" in current tab of newDoc
            if readyState is "complete" then exit repeat
        end try
    end repeat
    set pageTitle to do JavaScript "document.title || ''" in current tab of newDoc
    set pageText to do JavaScript "document.body ? document.body.innerText : ''" in current tab of newDoc
    close newDoc
    return pageTitle & linefeed & pageText
end tell
'''
    proc = subprocess.run(
        ["osascript", "-e", script],
        capture_output=True,
        text=True,
        timeout=45,
    )
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "Safari browser fallback failed").strip()
        raise RuntimeError(detail)
    output = proc.stdout or ""
    title, _, text = output.partition("\n")
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        raise RuntimeError("Safari browser fallback returned no readable text")
    return {
        "url": url,
        "title": title.strip() or urllib.parse.urlparse(url).netloc,
        "content_type": "text/html (Safari fallback)",
        "text": text[:URL_MAX_CHARS],
    }


def fetch_url_context(url):
    parsed = urllib.parse.urlparse(url)
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/127.0.0.0 Safari/537.36"
        ),
        "Accept": (
            "text/html,application/xhtml+xml,application/xml;q=0.9,"
            "text/plain;q=0.8,*/*;q=0.7"
        ),
        "Accept-Language": "en-US,en;q=0.9",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
        "Referer": f"{parsed.scheme}://{parsed.netloc}/" if parsed.scheme and parsed.netloc else url,
    }
    last_error = None
    for candidate in [url, url.rstrip("/")]:
        if not candidate:
            continue
        req = urllib.request.Request(candidate, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                content_type = (resp.headers.get("Content-Type") or "").lower()
                raw = resp.read()
                charset = resp.headers.get_content_charset() or "utf-8"
            body = raw.decode(charset, errors="replace")
            break
        except Exception as exc:
            last_error = exc
    else:
        status = getattr(last_error, "code", None)
        if status in {403, 429}:
            return fetch_url_context_via_safari(url)
        raise last_error or RuntimeError("Unable to retrieve URL")
    if "html" in content_type or "<html" in body.lower():
        parser = HTMLTextExtractor()
        parser.feed(body)
        title = parser.title() or urllib.parse.urlparse(url).netloc
        text = parser.text()
    elif content_type.startswith("text/"):
        title = urllib.parse.urlparse(url).path.rsplit("/", 1)[-1] or url
        text = body
    else:
        raise RuntimeError(f"Unsupported content type for lightweight fetch: {content_type or 'unknown'}")
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        raise RuntimeError("No readable text found on page")
    return {
        "url": url,
        "title": title,
        "content_type": content_type or "unknown",
        "text": text[:URL_MAX_CHARS],
    }


def convert_file(path, mode):
    p = pathlib.Path(path).expanduser()
    should_convert = mode == "all" or (mode == "auto" and p.suffix.lower() in CONVERTIBLE)
    if should_convert:
        if p.suffix.lower() == ".doc":
            conv = subprocess.run(
                ["/usr/bin/textutil", "-convert", "txt", "-stdout", str(p)],
                capture_output=True,
                text=True,
            )
            if conv.returncode == 0 and conv.stdout.strip():
                return conv.stdout, f"{p.name} (converted via textutil)"
        markitdown = find_markitdown()
        conv = subprocess.run([markitdown, str(p)], capture_output=True, text=True)
        if conv.returncode != 0 or not conv.stdout.strip():
            detail = (conv.stderr or conv.stdout or "no output").strip()
            raise RuntimeError(f"markitdown could not convert {p.name}:\n{detail}")
        return conv.stdout, f"{p.name} (converted to markdown)"
    return p.read_text(errors="replace"), p.name


def find_markitdown():
    found = shutil.which("markitdown")
    if found:
        return found
    for candidate in ("/usr/local/bin/markitdown", "/opt/homebrew/bin/markitdown"):
        if pathlib.Path(candidate).exists():
            return candidate
    raise RuntimeError(
        "markitdown is not available to the GUI. Install it with "
        "pip install markitdown, or run mlxgui from a shell that has markitdown on PATH."
    )


def usage_counts(usage):
    if not usage:
        return 0, 0
    in_tokens = usage.get("prompt_tokens", usage.get("input_tokens", 0))
    out_tokens = usage.get("completion_tokens", usage.get("output_tokens", 0))
    return int(in_tokens or 0), int(out_tokens or 0)


PROMPT_REFINER_SYSTEM = (
    "You are a prompt decoder, not the task solver. Rewrite the user's request into one precise prompt for another LLM.\n"
    "Return only the rewritten prompt, with no preamble, analysis, or commentary.\n"
    "Preserve every literal filename, path, number, code, quoted value, list item, and requested output column.\n"
    "Do not invent missing inputs or facts. If the request refers to missing content, explicitly state that the input is missing.\n"
    "Use the supplied recent context only to resolve references such as 'these', 'each', 'above', or 'the previous result'; "
    "do not treat context as a new task unless the current request refers to it.\n"
    "Resolve intent into: objective, resources, exact operation, constraints, preservation/order rules, and output format.\n"
    "For matching or lookup tasks, require full-key matching and repeat any normalization rule exactly; never weaken it to suffix or similarity matching.\n"
    "For local file listings or size rankings, recommend find with stat and byte-based sorting; never rely on ls with positional awk fields because filenames may contain spaces.\n"
    "For file metadata such as creation dates, require stat on the exact files and never infer dates from names or prior text.\n"
    "Keep the result compact and directly actionable."
)


def compact_refinement_context(messages, max_chars=9000):
    parts = []
    for message in messages[1:]:
        role = message.get("role")
        content = (message.get("content") or "").strip()
        if role not in {"user", "assistant"} or not content:
            continue
        parts.append(f"{role.title()} message:\n{content[:4500]}")
    return "\n\n".join(parts[-4:])[-max_chars:]


def refine_prompt_once(url, key, model, raw_request, context=""):
    prompt = raw_request
    if context:
        prompt = f"Recent conversation context (use only for resolving references):\n{context}\n\nCurrent request to refine:\n{raw_request}"
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": PROMPT_REFINER_SYSTEM},
            {"role": "user", "content": prompt},
        ],
        "max_tokens": 900,
        "stream": False,
        "repetition_penalty": DEFAULT_REPETITION_PENALTY,
    }
    data = api(url, key, "/v1/chat/completions", payload)
    choices = data.get("choices") or []
    content = (choices[0].get("message") or {}).get("content", "") if choices else ""
    return content.strip(), data.get("usage") or {}


# gui_term_present and is_code_request are imported from mlxlib (shared with mlxcli).


def request_messages_for_turn(messages, user_text):
    if not is_code_request(user_text):
        return messages
    scoped = list(messages)
    insert_at = 1 if scoped and scoped[0].get("role") == "system" else 0
    scoped.insert(insert_at, {"role": "system", "content": CODE_REQUEST_SYSTEM})
    return scoped


def rag_candidate_files(folder):
    root = pathlib.Path(folder).expanduser()
    if not root.exists() or not root.is_dir():
        raise RuntimeError(f"RAG folder not found: {root}")
    files = []
    for path in sorted(root.rglob("*")):
        if len(files) >= RAG_MAX_FILES:
            break
        rel_parts = path.relative_to(root).parts
        if any(part.startswith(".") for part in rel_parts):
            continue
        if not path.is_file() or path.name.startswith("."):
            continue
        if path.suffix.lower() in CONVERTIBLE or path.suffix.lower() in {".md", ".txt", ".csv", ".json"}:
            files.append(path)
    return files


def rag_cache_dir(folder):
    return pathlib.Path(folder).expanduser() / RAG_CACHE_DIRNAME


def rag_cache_index_path(folder):
    return rag_cache_dir(folder) / RAG_CACHE_INDEX


def rag_load_cache_index(folder):
    path = rag_cache_index_path(folder)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def rag_save_cache_index(folder, data):
    cache_dir = rag_cache_dir(folder)
    cache_dir.mkdir(parents=True, exist_ok=True)
    rag_cache_index_path(folder).write_text(json.dumps(data, indent=2, sort_keys=True))


def rag_cache_name(path, folder):
    root = pathlib.Path(folder).expanduser()
    rel = path.relative_to(root)
    digest = hashlib.sha1(str(rel).encode("utf-8")).hexdigest()[:12]
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", rel.stem).strip("._") or "document"
    return f"{stem}-{digest}.md"


def extract_candidate_name(text, label):
    for line in text.splitlines():
        cleaned = line.strip().strip("*#").strip()
        if not cleaned:
            continue
        if len(cleaned) > 80:
            continue
        if any(ch.isdigit() for ch in cleaned):
            continue
        return cleaned
    return pathlib.Path(label).stem


def rag_chunks(text, label):
    compact = re.sub(r"\s+", " ", text).strip()
    if not compact:
        return []
    chunks = []
    start = 0
    while start < len(compact):
        end = min(start + RAG_CHUNK_CHARS, len(compact))
        chunks.append((label, compact[start:end]))
        start = end
    return chunks


def score_rag_chunk(query, chunk_text):
    terms = [term for term in re.findall(r"[A-Za-z0-9_]{3,}", query.lower()) if term]
    if not terms:
        return 0
    lowered = chunk_text.lower()
    return sum(lowered.count(term) for term in terms)


def is_compare_all_rag_request(text):
    lowered = text.lower()
    compare_terms = ("compare", "rank", "ranking", "strengths", "weaknesses")
    corpus_terms = ("resume", "resumes", "documents", "candidates", "folder")
    return any(term in lowered for term in compare_terms) and any(term in lowered for term in corpus_terms)


def rag_source_mtime(path):
    return int(path.stat().st_mtime_ns)


def rag_prepare_file(folder, path, index):
    root = pathlib.Path(folder).expanduser()
    rel = str(path.relative_to(root))
    cache_dir = rag_cache_dir(root)
    entry = index.get(rel, {})
    source_mtime = rag_source_mtime(path)
    cached_name = entry.get("cache_name") or rag_cache_name(path, root)
    cached_path = cache_dir / cached_name
    suffix = path.suffix.lower()
    needs_refresh = (
        entry.get("source_mtime_ns") != source_mtime
        or not cached_path.exists()
        or cached_path.stat().st_size == 0
    )
    converted = False

    if suffix in CONVERTIBLE:
        if needs_refresh:
            text, _label = convert_file(path, "all")
            cache_dir.mkdir(parents=True, exist_ok=True)
            cached_path.write_text(text)
            converted = True
        else:
            text = cached_path.read_text(errors="replace")
    else:
        text = path.read_text(errors="replace")
        if needs_refresh:
            cache_dir.mkdir(parents=True, exist_ok=True)
            cached_path.write_text(text)

    index[rel] = {
        "cache_name": cached_name,
        "label": f"{path.name} (cached markdown)",
        "source_mtime_ns": source_mtime,
        "cached_at": datetime.now().isoformat(timespec="seconds"),
        "source_suffix": suffix,
    }
    return {
        "relative_path": rel,
        "path": path,
        "text": text,
        "label": index[rel]["label"],
        "cache_path": cached_path,
        "converted": converted,
    }


def rag_context_for_query(folder, user_text):
    root = pathlib.Path(folder).expanduser()
    files = rag_candidate_files(folder)
    index = rag_load_cache_index(root)
    converted = 0
    indexed = 0
    failed = []
    compare_mode = is_compare_all_rag_request(user_text)
    scored_matches = []
    fallback_matches = []
    file_labels = []
    rebuilt = False

    for path in files:
        try:
            record = rag_prepare_file(root, path, index)
        except Exception as exc:
            failed.append(f"{path.name}: {exc}")
            continue
        if record["converted"]:
            converted += 1
            rebuilt = True
        indexed += 1
        text = record["text"]
        label = record["label"]
        file_labels.append(label)
        chunks = rag_chunks(text, label)
        if not chunks:
            failed.append(f"{path.name}: no usable text extracted")
            continue
        candidate_name = extract_candidate_name(text, path.name)
        fallback_label = f"{candidate_name} | {label}"
        fallback_matches.append((1, fallback_label, chunks[0][1][:RAG_COMPARE_CHARS]))
        if compare_mode:
            continue
        for chunk_label, chunk_text in chunks:
            score = score_rag_chunk(user_text, chunk_text)
            if score > 0:
                scored_matches.append((score, chunk_label, chunk_text))

    stale_keys = {str(path.relative_to(root)) for path in files}
    for rel in list(index):
        if rel not in stale_keys:
            cache_name = index.get(rel, {}).get("cache_name")
            if cache_name:
                try:
                    (rag_cache_dir(root) / cache_name).unlink()
                except FileNotFoundError:
                    pass
            del index[rel]
            rebuilt = True
    if rebuilt or not rag_cache_index_path(root).exists():
        rag_save_cache_index(root, index)

    if compare_mode:
        matches = fallback_matches[:RAG_COMPARE_FILE_LIMIT]
    else:
        scored_matches.sort(key=lambda item: item[0], reverse=True)
        matches = scored_matches[:RAG_MAX_CHUNKS] or fallback_matches[:RAG_MAX_CHUNKS]

    return {
        "folder": root,
        "candidate_count": len(files),
        "converted_count": converted,
        "indexed_count": indexed,
        "failed": failed,
        "file_labels": file_labels,
        "matches": matches,
        "compare_mode": compare_mode,
        "cache_dir": rag_cache_dir(root),
    }


def request_messages_with_context(messages, user_text, rag_folder):
    scoped = request_messages_for_turn(messages, user_text)
    statuses = []
    urls = detect_urls(user_text)

    folder = (rag_folder or "").strip()
    if folder:
        context = rag_context_for_query(folder, user_text)
        matches = context["matches"]
        if matches:
            content_lines = [
                f"Chat RAG folder: {context['folder']}",
                f"RAG files found: {context['candidate_count']}",
                f"RAG files converted this pass: {context['converted_count']}",
                f"RAG files indexed from cached markdown: {context['indexed_count']}",
                f"RAG markdown cache: {context['cache_dir']}",
                "Relevant reference excerpts from the chat RAG folder:",
            ]
            if context["file_labels"]:
                content_lines.append("Files represented in this RAG context:")
                content_lines.append(", ".join(context["file_labels"]))
            used_labels = []
            for _score, label, chunk_text in matches:
                used_labels.append(label)
                content_lines.append(f"\n[{label}]\n{chunk_text}")
            insert_at = 1 if scoped and scoped[0].get("role") == "system" else 0
            scoped.insert(insert_at, {"role": "system", "content": RAG_USE_SYSTEM})
            insert_at += 1
            content_lines.append(
                "\nTreat the represented files above as the available documents for this turn. "
                "Use the provided excerpts to compare the source files that are represented here. "
                "If the request asks for ranking, rank the represented files directly. "
                "When possible, list every represented candidate by name. "
                "If some files are represented only partially, still analyze and rank the represented set rather than asking "
                "the user to paste the resumes again."
            )
            scoped.insert(insert_at, {"role": "system", "content": "\n".join(content_lines)})
            failed_note = ""
            if context["failed"]:
                failed_names = ", ".join(item.split(":", 1)[0] for item in context["failed"][:6])
                failed_note = f"; {len(context['failed'])} file(s) failed conversion: {failed_names}"
            statuses.append(
                f"RAG found {context['candidate_count']} file(s), converted {context['converted_count']}, "
                f"indexed {context['indexed_count']}, "
                f"used {len(matches)} excerpt(s) from {', '.join(dict.fromkeys(used_labels))}{failed_note}"
            )
        else:
            statuses.append("No usable RAG excerpts were found in the selected folder.")

    if urls:
        fetched = []
        for url in urls:
            try:
                fetched.append(fetch_url_context(url))
            except Exception as exc:
                if not fetched:
                    return scoped, None, f"Unable to retrieve {url}: {exc}"
                statuses.append(f"URL fetch skipped {url}: {exc}")
        if fetched:
            content_lines = ["Fetched web page content for this turn:"]
            for item in fetched:
                content_lines.append(
                    f"\n[URL]\nTitle: {item['title']}\nURL: {item['url']}\nContent-Type: {item['content_type']}\nExcerpt:\n{item['text']}"
                )
            insert_at = 1 if scoped and scoped[0].get("role") == "system" else 0
            scoped.insert(insert_at, {"role": "system", "content": URL_USE_SYSTEM})
            scoped.insert(insert_at + 1, {"role": "system", "content": "\n".join(content_lines)})
            statuses.append(
                f"Fetched {len(fetched)} URL(s): {', '.join(item['title'] for item in fetched)}"
            )

    status_text = " | ".join(statuses) if statuses else None
    return scoped, status_text, None


def system_resource_snapshot():
    def sysctl(name):
        try:
            out = subprocess.run(["sysctl", "-n", name], capture_output=True, text=True, timeout=5)
            return int(out.stdout.strip())
        except Exception:
            return None

    snapshot = {
        "total": sysctl("hw.memsize"),
        "metal_cap": sysctl("iogpu.wired_limit_mb"),
        "free": None,
        "reclaimable": None,
        "top": [],
        "errors": [],
    }
    try:
        vm = subprocess.run(["vm_stat"], capture_output=True, text=True, timeout=5).stdout
        match = re.search(r"page size of (\d+)", vm)
        page = int(match.group(1)) if match else 16384

        def pages(label):
            found = re.search(label + r":\s+(\d+)", vm)
            return int(found.group(1)) * page if found else 0

        free = pages("Pages free")
        snapshot["free"] = free
        snapshot["reclaimable"] = free + pages("Pages inactive") + pages("Pages purgeable")
    except Exception as exc:
        snapshot["errors"].append(f"vm_stat unavailable: {exc}")
    try:
        ps = subprocess.run(["ps", "axo", "rss=,comm="], capture_output=True, text=True, timeout=5)
        rows = []
        for line in ps.stdout.splitlines():
            parts = line.strip().split(None, 1)
            if len(parts) == 2 and parts[0].isdigit():
                rows.append((int(parts[0]), parts[1]))
        rows.sort(reverse=True)
        for rss, command in rows:
            mb = rss // 1024
            if mb < 300 or len(snapshot["top"]) >= 10:
                break
            if ".app/" in command:
                segment = command.split("/Applications/")[-1]
                name = segment.split(".app/")[0] if ".app/" in segment else command.rsplit("/", 1)[-1]
            else:
                name = command.rsplit("/", 1)[-1]
            tag = " <- oMLX" if "omlx" in command.lower() else ""
            snapshot["top"].append((mb, name, tag))
    except Exception as exc:
        snapshot["errors"].append(f"ps unavailable: {exc}")
    return snapshot


def system_resource_lines():
    lines = []
    snapshot = system_resource_snapshot()
    total = snapshot["total"]
    cap = snapshot["metal_cap"]
    if total:
        lines.append(f"- Total RAM: {total / 2**30:.1f} GB")
    if cap:
        lines.append(f"- Metal cap: {cap / 1024:.1f} GB")
    if snapshot["free"] is not None:
        lines.append(f"- Free now: {snapshot['free'] / 2**30:.1f} GB")
    if snapshot["reclaimable"] is not None:
        lines.append(f"- Reclaimable: {snapshot['reclaimable'] / 2**30:.1f} GB")
    lines.extend(f"- {error}" for error in snapshot["errors"])
    lines.append("")
    lines.append("Top Memory Users")
    if snapshot["top"]:
        for mb, name, tag in snapshot["top"]:
            lines.append(f"- {mb:>6} MB  {name}{tag}")
    else:
        lines.append("- Nothing over 300 MB")
    return lines


def trim(messages):
    starts = [i for i, m in enumerate(messages) if m.get("role") == "user"]
    if len(starts) > MAX_HISTORY_TURNS:
        cut = starts[-MAX_HISTORY_TURNS]
        messages = [messages[0]] + messages[cut:]
    if sum(len(str(m.get("content") or "")) for m in messages) <= MAX_CONTEXT_CHARS:
        return messages
    system = messages[:1]
    kept = []
    total = sum(len(str(m.get("content") or "")) for m in system)
    for message in reversed(messages[1:]):
        size = len(str(message.get("content") or ""))
        if kept and total + size > MAX_CONTEXT_CHARS:
            break
        kept.append(message)
        total += size
    return system + list(reversed(kept))


# gui_should_auto_enable_agentic, gui_execution_contract, gui_tool_failed,
# gui_normalize_tool_name, and gui_infer_command_cwd are imported from mlxlib
# (shared with mlxcli).


def parse_gui_text_tool_calls(content):
    if not content or "call:" not in content:
        return []
    patterns = (
        ("run_command", r"call:(?:(?:[A-Za-z0-9_]+)[.:])*run_command\s*\{\s*command:\s*(\"(?:\\.|[^\"])*\")\s*\}"),
        ("read_file", r"call:(?:(?:[A-Za-z0-9_]+)[.:])*read_file\s*\{\s*path:\s*(\"(?:\\.|[^\"])*\")\s*\}"),
        ("write_file", r"call:(?:(?:[A-Za-z0-9_]+)[.:])*write_file\s*\{\s*path:\s*(\"(?:\\.|[^\"])*\")\s*,\s*(?:content|text):\s*(\"(?:\\.|[^\"])*\")\s*\}"),
        ("python_interpreter", r"call:python_interpreter\s*\{\s*code:\s*(\"(?:\\.|[^\"])*\")\s*\}"),
    )
    calls = []
    for name, pattern in patterns:
        for match in re.finditer(pattern, content, flags=re.DOTALL):
            try:
                first = json.loads(match.group(1))
                if name == "write_file":
                    second = re.search(r"(?:content|text):\s*(\"(?:\\.|[^\"])*\")", match.group(0), flags=re.DOTALL)
                    args = {"path": first, "content": json.loads(second.group(1))} if second else {}
                elif name == "run_command":
                    args = {"command": first}
                elif name == "read_file":
                    args = {"path": first}
                else:
                    args = {"code": first}
            except (json.JSONDecodeError, AttributeError):
                continue
            calls.append({"id": gui_unique_call_id("gui_text_call"), "type": "function",
                          "function": {"name": name, "arguments": json.dumps(args)}})
    return calls


def markdown_to_text(markdown):
    lines = []
    for line in markdown.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            stripped = stripped.lstrip("#").strip()
        elif stripped.startswith(("- ", "* ")):
            stripped = "- " + stripped[2:].strip()
        lines.append(stripped)
    return "\n".join(lines)


def paragraph_xml(text, style=None):
    p_pr = f'<w:pPr><w:pStyle w:val="{style}"/></w:pPr>' if style else ""
    runs = []
    for part in text.split("\n"):
        if runs:
            runs.append("<w:br/>")
        runs.append(f"<w:t>{escape(part)}</w:t>")
    return f"<w:p>{p_pr}<w:r>{''.join(runs)}</w:r></w:p>"


def markdown_to_docx(path, title, markdown):
    if formatted_markdown_to_docx is not None:
        return formatted_markdown_to_docx(path, title, markdown)
    body = [paragraph_xml(title, "Title")]
    body.append(paragraph_xml(f"Exported {datetime.now().strftime('%Y-%m-%d %H:%M')}"))
    for line in markdown.splitlines():
        stripped = line.strip()
        if not stripped:
            body.append(paragraph_xml(""))
        elif stripped.startswith("### "):
            body.append(paragraph_xml(stripped[4:], "Heading3"))
        elif stripped.startswith("## "):
            body.append(paragraph_xml(stripped[3:], "Heading2"))
        elif stripped.startswith("# "):
            body.append(paragraph_xml(stripped[2:], "Heading1"))
        elif stripped.startswith(("- ", "* ")):
            body.append(paragraph_xml("- " + stripped[2:]))
        else:
            body.append(paragraph_xml(stripped))

    document_xml = f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body>
    {''.join(body)}
    <w:sectPr>
      <w:pgSz w:w="12240" w:h="15840"/>
      <w:pgMar w:top="1440" w:right="1440" w:bottom="1440" w:left="1440" w:header="708" w:footer="708" w:gutter="0"/>
    </w:sectPr>
  </w:body>
</w:document>'''
    styles_xml = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:style w:type="paragraph" w:default="1" w:styleId="Normal">
    <w:name w:val="Normal"/>
    <w:rPr><w:rFonts w:ascii="Calibri" w:hAnsi="Calibri"/><w:sz w:val="22"/></w:rPr>
    <w:pPr><w:spacing w:after="120" w:line="300" w:lineRule="auto"/></w:pPr>
  </w:style>
  <w:style w:type="paragraph" w:styleId="Title">
    <w:name w:val="Title"/><w:basedOn w:val="Normal"/>
    <w:rPr><w:b/><w:color w:val="0B2545"/><w:sz w:val="48"/></w:rPr>
    <w:pPr><w:spacing w:after="200"/></w:pPr>
  </w:style>
  <w:style w:type="paragraph" w:styleId="Heading1">
    <w:name w:val="heading 1"/><w:basedOn w:val="Normal"/>
    <w:rPr><w:b/><w:color w:val="2E74B5"/><w:sz w:val="32"/></w:rPr>
    <w:pPr><w:spacing w:before="360" w:after="200"/></w:pPr>
  </w:style>
  <w:style w:type="paragraph" w:styleId="Heading2">
    <w:name w:val="heading 2"/><w:basedOn w:val="Normal"/>
    <w:rPr><w:b/><w:color w:val="2E74B5"/><w:sz w:val="26"/></w:rPr>
    <w:pPr><w:spacing w:before="280" w:after="140"/></w:pPr>
  </w:style>
  <w:style w:type="paragraph" w:styleId="Heading3">
    <w:name w:val="heading 3"/><w:basedOn w:val="Normal"/>
    <w:rPr><w:b/><w:color w:val="1F4D78"/><w:sz w:val="24"/></w:rPr>
    <w:pPr><w:spacing w:before="200" w:after="100"/></w:pPr>
  </w:style>
</w:styles>'''
    content_types = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
  <Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>
</Types>'''
    rels = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
</Relationships>'''
    doc_rels = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>
</Relationships>'''
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", content_types)
        zf.writestr("_rels/.rels", rels)
        zf.writestr("word/_rels/document.xml.rels", doc_rels)
        zf.writestr("word/document.xml", document_xml)
        zf.writestr("word/styles.xml", styles_xml)


class MlxGui(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("mlxgui")
        self.geometry("920x680")
        self.minsize(720, 500)

        self.backend = load_backend()
        self.url, self.key = load_backend_cfg(self.backend)
        self.system_prompt = load_system_prompt()
        self.gui_defaults = load_gui_defaults()
        self.messages = [{"role": "system", "content": self.system_prompt}]
        notes_path, notes_text = find_project_notes()
        if notes_text:
            self.messages.append({"role": "system", "content": f"Project notes from {notes_path}:\n\n{notes_text}"})
        self.totals = {"in": 0, "out": 0}
        self.last_turn_tokens = {"in": 0, "out": 0}
        self.events = queue.Queue()
        self.model_display_to_id = {}
        self.model_var = tk.StringVar()
        self.backend_var = tk.StringVar(value=backend_label(self.backend))
        self.convert_var = tk.StringVar(value=self.gui_defaults["convert_mode"])
        self.chat_rag_folder_var = tk.StringVar(value="")
        self.status_var = tk.StringVar(value="Starting")
        self.tokens_var = tk.StringVar(value="tokens: in 0 / out 0")
        self.resource_var = tk.StringVar(value="Context: OK")
        self.memory_var = tk.StringVar(value="Resources: ...")
        self.working_var = tk.StringVar(value="")
        self.busy = False
        self.refining = False
        self.last_user_text = ""
        self.current_stream_start = None
        self.current_stream_end = None
        self.working_started_at = None
        self.working_after = None
        self.resource_after = None
        self.context_widget = None
        self.cancel_requested = False
        self.pending_user_index = None

        self.build_ui()
        self.after(80, self.choose_backend_on_launch)
        self.update_memory_indicator()
        self.start_resource_refresh()
        self.after(60, self.drain_events)

    def build_ui(self):
        self.build_menu()
        top = ttk.Frame(self, padding=(10, 8))
        top.pack(fill="x")

        ttk.Label(top, text="Model").pack(side="left")
        self.model_box = ttk.Combobox(top, textvariable=self.model_var, state="readonly", width=38)
        self.model_box.pack(side="left", padx=(6, 12))

        ttk.Label(top, text="Backend").pack(side="left")
        self.backend_box = ttk.Combobox(
            top,
            textvariable=self.backend_var,
            state="readonly",
            values=[backend_label(name) for name in SUPPORTED_BACKENDS],
            width=25,
        )
        self.backend_box.pack(side="left", padx=(6, 12))
        self.backend_box.bind("<<ComboboxSelected>>", self.backend_selection_changed)

        ttk.Button(top, text="New Chat", command=self.new_chat).pack(side="left", padx=(0, 6))
        ttk.Button(top, text="Clear", command=self.clear_chat).pack(side="left", padx=(0, 6))
        ttk.Button(top, text="Import", command=self.import_files).pack(side="left")
        ttk.Button(top, text="Settings", command=self.open_settings).pack(side="left", padx=(6, 0))
        ttk.Label(top, textvariable=self.tokens_var).pack(side="right")
        ttk.Label(top, textvariable=self.resource_var).pack(side="right", padx=(0, 12))

        chat_frame = ttk.Frame(self)
        chat_frame.pack(fill="both", expand=True, padx=10)
        ttk.Label(chat_frame, text="Conversation").pack(anchor="w")
        self.chat = tk.Text(chat_frame, wrap="word", padx=10, pady=10, undo=True)
        chat_scroll = ttk.Scrollbar(chat_frame, orient="vertical", command=self.chat.yview)
        self.chat.configure(yscrollcommand=chat_scroll.set)
        self.chat.pack(side="left", fill="both", expand=True)
        chat_scroll.pack(side="right", fill="y")
        body_font = tkfont.Font(family="Georgia", size=16)
        label_font = tkfont.Font(family="Helvetica", size=13)
        small_font = tkfont.Font(family="Helvetica", size=11)
        heading_font = tkfont.Font(family="Helvetica", size=19, weight="bold")
        subheading_font = tkfont.Font(family="Helvetica", size=16, weight="bold")
        self.chat.configure(
            state="normal",
            background="#ffffff",
            borderwidth=0,
            font=body_font,
            insertbackground="#111111",
            relief="flat",
            selectbackground="#2f6fed",
            selectforeground="#ffffff",
        )
        self.chat.tag_configure(
            "user_bubble",
            justify="right",
            background="#f2f1ef",
            foreground="#202020",
            font=tkfont.Font(family="Helvetica", size=17),
            lmargin1=180,
            lmargin2=180,
            rmargin=24,
            spacing1=12,
            spacing3=18,
        )
        self.chat.tag_configure(
            "assistant_label",
            foreground="#7a7a73",
            font=label_font,
            spacing1=14,
            spacing3=8,
        )
        self.chat.tag_configure(
            "assistant_body",
            foreground="#0f0f0f",
            background="#f7f7f5",
            font=body_font,
            lmargin1=24,
            lmargin2=24,
            rmargin=40,
            spacing1=8,
            spacing3=10,
        )
        self.chat.tag_configure("meta", foreground="#667085", font=small_font, spacing1=8, spacing3=8)
        self.chat.tag_configure(
            "heading",
            foreground="#111111",
            background="#f7f7f5",
            font=heading_font,
            lmargin1=24,
            lmargin2=24,
            rmargin=40,
            spacing1=14,
            spacing3=8,
        )
        self.chat.tag_configure(
            "subheading",
            foreground="#252525",
            background="#f7f7f5",
            font=subheading_font,
            lmargin1=24,
            lmargin2=24,
            rmargin=40,
            spacing1=10,
            spacing3=6,
        )
        self.chat.tag_configure(
            "bullet",
            foreground="#0f0f0f",
            background="#f7f7f5",
            font=body_font,
            lmargin1=46,
            lmargin2=66,
            rmargin=40,
            spacing1=3,
            spacing3=5,
        )
        self.chat.tag_configure(
            "code",
            foreground="#111827",
            background="#e9edf3",
            font=tkfont.Font(family="Menlo", size=13, weight="bold"),
        )
        self.chat.tag_configure(
            "code_block",
            foreground="#064e3b",
            background="#eef6ef",
            font=tkfont.Font(family="Menlo", size=14, weight="bold"),
            lmargin1=38,
            lmargin2=38,
            rmargin=38,
            spacing1=8,
            spacing3=8,
        )
        self.raise_text_selection()
        self.chat.bind("<ButtonRelease-1>", lambda _event: self.raise_text_selection())
        self.bind_text_context_menu(self.chat)

        bottom = ttk.Frame(self, padding=10)
        bottom.pack(fill="x")
        self.input = tk.Text(bottom, height=3, wrap="word", undo=True)
        self.input.configure(
            background="#fbfbfa",
            borderwidth=1,
            padx=10,
            pady=8,
            relief="solid",
            selectbackground="#2f6fed",
            selectforeground="#ffffff",
        )
        self.input.pack(side="left", fill="x", expand=True)
        self.input.bind("<Return>", self.send_from_keyboard)
        self.input.bind("<Shift-Return>", lambda _event: None)
        self.input.bind("<<Paste>>", self.paste_into_input)
        self.bind_text_context_menu(self.input)
        ttk.Button(bottom, text="Paste", command=self.paste_into_input).pack(side="left", padx=(8, 0))
        ttk.Button(bottom, text="Add File", command=self.insert_file_references).pack(side="left", padx=(8, 0))
        self.refine_button = ttk.Button(bottom, text="Refine", command=self.refine_prompt)
        self.refine_button.pack(side="left", padx=(8, 0))
        self.send_button = ttk.Button(bottom, text="Send", command=self.send)
        self.send_button.pack(side="left", padx=(8, 0))

        status_bar = ttk.Frame(self, padding=(10, 0, 10, 8))
        status_bar.pack(fill="x")
        ttk.Label(status_bar, textvariable=self.working_var, width=10).pack(side="left")
        ttk.Label(status_bar, textvariable=self.status_var, anchor="w").pack(side="left", fill="x", expand=True)
        ttk.Label(status_bar, textvariable=self.memory_var, anchor="e").pack(side="right", padx=(12, 0))

    def build_menu(self):
        menu = tk.Menu(self)
        file_menu = tk.Menu(menu, tearoff=False)
        file_menu.add_command(label="Import Files...", command=self.import_files)
        file_menu.add_separator()
        file_menu.add_command(label="New Chat", command=self.new_chat)
        file_menu.add_command(label="Clear Chat", command=self.clear_chat)
        file_menu.add_separator()
        file_menu.add_command(label="Save Dialogue Context...", command=self.save_dialogue_context)
        file_menu.add_command(label="Load Dialogue Context...", command=self.load_dialogue_context)
        file_menu.add_separator()
        file_menu.add_command(label="Export Latest Reply...", command=self.export_reply)
        file_menu.add_command(label="Save Latest Reply as DOCX", command=self.save_latest_docx)
        file_menu.add_separator()
        file_menu.add_command(label="Quit", command=self.quit_app)
        menu.add_cascade(label="File", menu=file_menu)
        edit = tk.Menu(menu, tearoff=False)
        edit.add_command(label="Copy", accelerator="Cmd+C", command=self.menu_copy)
        edit.add_command(label="Paste", accelerator="Cmd+V", command=self.menu_paste)
        edit.add_command(label="Select All", accelerator="Cmd+A", command=self.menu_select_all)
        edit.add_command(label="Copy Latest Reply", command=self.copy_latest_reply)
        edit.add_separator()
        edit.add_command(label="Clear Chat", command=self.clear_chat)
        menu.add_cascade(label="Edit", menu=edit)
        tools_menu = tk.Menu(menu, tearoff=False)
        tools_menu.add_command(label="Refine Prompt...", command=self.refine_prompt)
        tools_menu.add_command(label="Add File Reference...", command=self.insert_file_references)
        menu.add_cascade(label="Tools", menu=tools_menu)
        view = tk.Menu(menu, tearoff=False)
        view.add_command(label="Resources...", command=self.open_resources)
        menu.add_cascade(label="View", menu=view)
        backend_menu = tk.Menu(menu, tearoff=False)
        for backend in SUPPORTED_BACKENDS:
            backend_menu.add_command(
                label=f"{backend_label(backend)} - {backend_description(backend)}",
                command=lambda name=backend: self.switch_backend(name),
            )
        menu.add_cascade(label="Backend", menu=backend_menu)
        defaults = tk.Menu(menu, tearoff=False)
        defaults.add_command(label="Settings...", command=self.open_settings)
        defaults.add_command(label="Restore Built-in Dialogue Defaults", command=self.restore_builtin_dialogue_defaults)
        menu.add_cascade(label="Defaults", menu=defaults)
        self.config(menu=menu)
        self.bind_all("<Command-c>", lambda _event: self.menu_copy())
        self.bind_all("<Command-C>", lambda _event: self.menu_copy())
        self.bind_all("<Control-c>", lambda _event: self.menu_copy())
        self.bind_all("<Control-C>", lambda _event: self.menu_copy())
        self.bind_all("<Command-a>", lambda _event: self.menu_select_all())
        self.bind_all("<Command-A>", lambda _event: self.menu_select_all())
        self.bind_all("<Control-a>", lambda _event: self.menu_select_all())
        self.bind_all("<Control-A>", lambda _event: self.menu_select_all())
        self.bind_all("<Escape>", self.handle_escape)
        self.text_menu = tk.Menu(self, tearoff=False)
        self.text_menu.add_command(label="Copy", command=self.context_copy)
        self.text_menu.add_command(label="Paste", command=self.context_paste)
        self.text_menu.add_command(label="Select All", command=self.context_select_all)

    def bind_text_context_menu(self, widget):
        widget.bind("<Button-2>", self.show_text_context_menu)
        widget.bind("<Button-3>", self.show_text_context_menu)
        widget.bind("<Control-Button-1>", self.show_text_context_menu)

    def show_text_context_menu(self, event):
        self.context_widget = event.widget
        event.widget.focus_set()
        try:
            self.text_menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.text_menu.grab_release()
        return "break"

    def active_text_widget(self):
        widget = self.context_widget or self.focused_text_widget()
        return widget if isinstance(widget, tk.Text) else None

    def context_copy(self):
        widget = self.active_text_widget()
        if widget is self.input:
            return self.copy_input_selection()
        return self.copy_chat_selection()

    def context_paste(self):
        widget = self.active_text_widget()
        if widget is self.input:
            return self.paste_into_input()
        self.input.focus_set()
        return self.paste_into_input()

    def context_select_all(self):
        widget = self.active_text_widget()
        if widget is self.input:
            return self.select_all_input()
        return self.select_all_chat()

    def focused_text_widget(self):
        widget = self.focus_get()
        return widget if isinstance(widget, tk.Text) else None

    def menu_copy(self):
        widget = self.focused_text_widget()
        if widget is self.chat:
            return self.copy_chat_selection()
        if widget is self.input:
            return self.copy_input_selection()
        return self.copy_chat_selection()

    def menu_paste(self):
        widget = self.focused_text_widget()
        if widget is self.input:
            return self.paste_into_input()
        self.input.focus_set()
        return self.paste_into_input()

    def menu_select_all(self):
        widget = self.focused_text_widget()
        if widget is self.input:
            return self.select_all_input()
        return self.select_all_chat()

    def status(self, text):
        self.events.put(("status", text))

    def raise_text_selection(self):
        try:
            self.chat.tag_raise("sel")
            self.input.tag_raise("sel")
        except Exception:
            pass
        return None

    def start_working(self, text="Thinking"):
        self.working_started_at = time.time()
        self.update_working_elapsed()
        self.status_var.set(text)

    def stop_working(self):
        if self.working_after is not None:
            self.after_cancel(self.working_after)
            self.working_after = None
        self.working_started_at = None
        self.working_var.set("")

    def update_working_elapsed(self):
        if not self.busy or self.working_started_at is None:
            self.working_var.set("")
            self.working_after = None
            return
        elapsed = int(time.time() - self.working_started_at)
        self.working_var.set(f"Working {elapsed}s")
        self.working_after = self.after(1000, self.update_working_elapsed)

    def quit_app(self):
        self.stop_working()
        self.stop_resource_refresh()
        self.destroy()
        return "break"

    def apply_system_prompt(self, text):
        self.system_prompt = text.strip() or DEFAULT_SYSTEM
        if self.messages and self.messages[0].get("role") == "system":
            self.messages[0]["content"] = self.system_prompt
        else:
            self.messages.insert(0, {"role": "system", "content": self.system_prompt})

    def open_dialogue_options(self):
        return self.open_settings()

    def refine_prompt(self, request_text=None):
        if self.busy or getattr(self, "refining", False):
            return
        raw_request = (request_text if request_text is not None else self.input.get("1.0", "end-1c")).strip()
        if not raw_request:
            raw_request = self.last_user_text.strip()
        if not raw_request:
            messagebox.showinfo(
                "Refine Prompt",
                "Enter a request in the prompt box first, or refine after sending a request.",
                parent=self,
            )
            return
        model = self.selected_model_id()
        if not model:
            messagebox.showinfo("No model", "Wait for models to load first.", parent=self)
            return
        self.refining = True
        self.refine_button.configure(state="disabled")
        self.status_var.set("Refining prompt")
        self.start_working("Refining")
        threading.Thread(
            target=self.refine_prompt_worker,
            args=(model, raw_request, compact_refinement_context(self.messages)),
            daemon=True,
        ).start()

    def refine_prompt_worker(self, model, raw_request, context):
        try:
            refined, usage = refine_prompt_once(self.url, self.key, model, raw_request, context)
            self.events.put(("refined", raw_request, refined, usage))
        except urllib.error.HTTPError as exc:
            try:
                detail = exc.read().decode("utf-8", errors="replace").strip()
            except Exception:
                detail = ""
            message = f"HTTP {exc.code}: {exc.reason}"
            if detail:
                message += f" - {detail[:400]}"
            self.events.put(("refine_error", message))
        except Exception as exc:
            self.events.put(("refine_error", str(exc)))

    def open_settings(self):
        win = tk.Toplevel(self)
        win.title("Settings")
        win.geometry("760x540")
        win.transient(self)

        frame = ttk.Frame(win, padding=12)
        frame.pack(fill="both", expand=True)
        notebook = ttk.Notebook(frame)
        notebook.pack(fill="both", expand=True)

        defaults_tab = ttk.Frame(notebook, padding=10)
        chat_tab = ttk.Frame(notebook, padding=10)
        notebook.add(defaults_tab, text="Defaults")
        notebook.add(chat_tab, text="This Chat")

        ttk.Label(defaults_tab, text="Saved defaults used when mlxgui starts or chat is cleared").pack(anchor="w")
        preset_row = ttk.Frame(defaults_tab)
        preset_row.pack(fill="x", pady=(8, 0))
        ttk.Label(preset_row, text="Preset").pack(side="left")
        preset_var = tk.StringVar(value="Concise, Efficient Agent Conversation")
        ttk.OptionMenu(
            preset_row,
            preset_var,
            preset_var.get(),
            *PRESET_SYSTEMS.keys(),
        ).pack(side="left", padx=(8, 8))

        default_convert_var = tk.StringVar(value=self.convert_var.get())
        ttk.Label(preset_row, text="Import conversion").pack(side="left", padx=(16, 0))
        ttk.OptionMenu(
            preset_row,
            default_convert_var,
            default_convert_var.get(),
            *CONVERT_MODES,
        ).pack(side="left", padx=(8, 0))

        text = tk.Text(defaults_tab, wrap="word", height=16, padx=8, pady=8)
        text.pack(fill="both", expand=True, pady=(8, 10))
        text.insert("1.0", self.system_prompt)

        buttons = ttk.Frame(defaults_tab)
        buttons.pack(fill="x")

        ttk.Label(chat_tab, text="Settings that apply only to the current dialogue").pack(anchor="w")
        rag_row = ttk.Frame(chat_tab)
        rag_row.pack(fill="x", pady=(12, 6))
        ttk.Label(rag_row, text="RAG folder").pack(side="left")
        rag_entry = ttk.Entry(rag_row, textvariable=self.chat_rag_folder_var)
        rag_entry.pack(side="left", fill="x", expand=True, padx=(8, 8))

        def browse_rag_folder():
            folder = filedialog.askdirectory(title="Select RAG folder", initialdir=str(DOWNLOADS))
            if folder:
                self.chat_rag_folder_var.set(folder)
                self.status_var.set(f"Set chat RAG folder to {folder}")

        def clear_rag_folder():
            self.chat_rag_folder_var.set("")
            self.status_var.set("Cleared chat RAG folder")

        ttk.Button(rag_row, text="Browse", command=browse_rag_folder).pack(side="left")
        ttk.Button(rag_row, text="Clear", command=clear_rag_folder).pack(side="left", padx=(6, 0))
        ttk.Label(
            chat_tab,
            text=(
                "The RAG folder is intentionally chat-specific. It is not saved as a global default, "
                "so unrelated chats do not automatically reference the same documents."
            ),
            wraplength=680,
        ).pack(anchor="w", pady=(4, 12))
        ttk.Label(
            chat_tab,
            text=(
                "Original files stay in their original format. mlxgui builds and reuses a hidden "
                "Markdown cache for retrieval in .mlxgui_rag_cache inside the selected RAG folder."
            ),
            wraplength=680,
        ).pack(anchor="w", pady=(0, 10))
        rag_actions = ttk.Frame(chat_tab)
        rag_actions.pack(fill="x", pady=(0, 12))
        ttk.Button(rag_actions, text="Rebuild RAG Cache", command=self.rebuild_rag_cache).pack(side="left")
        ttk.Button(rag_actions, text="Clear RAG Cache", command=self.clear_rag_cache).pack(side="left", padx=(8, 0))
        ttk.Button(chat_tab, text="Open Resources", command=self.open_resources).pack(anchor="w")

        def save_current():
            content = text.get("1.0", "end-1c").strip()
            if not content:
                messagebox.showinfo("Dialogue Options", "Instructions cannot be empty.")
                return
            try:
                save_system_prompt(content)
            except Exception as exc:
                messagebox.showerror("Save failed", str(exc))
                return
            mode = default_convert_var.get()
            if mode not in CONVERT_MODES:
                messagebox.showinfo("Settings", "Choose a valid import conversion mode.")
                return
            self.gui_defaults["convert_mode"] = mode
            try:
                save_gui_defaults(self.gui_defaults)
            except Exception as exc:
                messagebox.showerror("Save failed", str(exc))
                return
            self.apply_system_prompt(content)
            self.convert_var.set(mode)
            self.status_var.set("Saved default settings")
            win.destroy()

        def restore_text():
            text.delete("1.0", "end")
            text.insert("1.0", DEFAULT_SYSTEM)

        def apply_preset():
            text.delete("1.0", "end")
            text.insert("1.0", PRESET_SYSTEMS[preset_var.get()])

        ttk.Button(buttons, text="Save Default", command=save_current).pack(side="right")
        ttk.Button(buttons, text="Restore Built-in", command=restore_text).pack(side="right", padx=(0, 8))
        ttk.Button(buttons, text="Apply Preset", command=apply_preset).pack(side="right", padx=(0, 8))
        ttk.Button(buttons, text="Cancel", command=win.destroy).pack(side="right", padx=(0, 8))
        text.focus_set()

    def restore_builtin_dialogue_defaults(self):
        try:
            save_system_prompt(DEFAULT_SYSTEM)
        except Exception as exc:
            messagebox.showerror("Restore failed", str(exc))
            return
        self.apply_system_prompt(DEFAULT_SYSTEM)
        self.status_var.set("Restored built-in dialogue defaults")

    def rebuild_rag_cache(self):
        folder = (self.chat_rag_folder_var.get() or "").strip()
        if not folder:
            messagebox.showinfo("RAG cache", "Set a chat RAG folder first.")
            return
        try:
            cache_dir = rag_cache_dir(folder)
            if cache_dir.exists():
                shutil.rmtree(cache_dir)
            context = rag_context_for_query(folder, "rebuild rag cache")
        except Exception as exc:
            messagebox.showerror("RAG cache", str(exc))
            return
        self.status_var.set(
            f"Rebuilt RAG cache: {context['indexed_count']} file(s) indexed at {context['cache_dir']}"
        )

    def clear_rag_cache(self):
        folder = (self.chat_rag_folder_var.get() or "").strip()
        if not folder:
            messagebox.showinfo("RAG cache", "Set a chat RAG folder first.")
            return
        cache_dir = rag_cache_dir(folder)
        try:
            if cache_dir.exists():
                shutil.rmtree(cache_dir)
        except Exception as exc:
            messagebox.showerror("RAG cache", str(exc))
            return
        self.status_var.set(f"Cleared RAG cache at {cache_dir}")

    def open_resources(self):
        win = tk.Toplevel(self)
        win.title("Resources")
        win.geometry("620x520")
        win.transient(self)

        frame = ttk.Frame(win, padding=12)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="Local session and Mac resource snapshot").pack(anchor="w")
        text = tk.Text(frame, wrap="word", padx=8, pady=8, height=20)
        text.pack(fill="both", expand=True, pady=(8, 10))
        text.insert("1.0", self.resource_report())
        text.configure(state="disabled")

        buttons = ttk.Frame(frame)
        buttons.pack(fill="x")

        def refresh():
            self.update_memory_indicator()
            text.configure(state="normal")
            text.delete("1.0", "end")
            text.insert("1.0", self.resource_report())
            text.configure(state="disabled")

        ttk.Button(buttons, text="Clear Conversation", command=self.clear_chat).pack(side="left")
        ttk.Button(buttons, text="Refresh", command=refresh).pack(side="right")
        ttk.Button(buttons, text="Close", command=win.destroy).pack(side="right", padx=(0, 8))

    def resource_report(self):
        lines = [
            "Dialogue",
            f"- User turns in context: {self.user_turn_count()} of {MAX_HISTORY_TURNS}",
            f"- Last turn tokens: in {self.last_turn_tokens['in']:,} / out {self.last_turn_tokens['out']:,}",
            f"- Session tokens: in {self.totals['in']:,} / out {self.totals['out']:,}",
            f"- Imported file cap: {MAX_FILE_CHARS:,} characters per file",
            f"- RAG folder: {self.chat_rag_folder_var.get() or '(none set for this chat)'}",
            f"- RAG mode: keep original files, retrieve from cached markdown",
            "",
            "Guidance",
            self.resource_guidance(),
            "",
            "Recommendations",
            *self.resource_recommendations(),
            "",
            "Mac Memory",
        ]
        lines.extend(system_resource_lines())
        return "\n".join(lines)

    def user_turn_count(self):
        return sum(1 for message in self.messages if message.get("role") == "user")

    def resource_guidance(self):
        last_in = self.last_turn_tokens["in"]
        turns = self.user_turn_count()
        if last_in >= 12000:
            return "- Clear or start a new dialogue soon. Input context is already large."
        if last_in >= 8000:
            return "- Consider Clear after this task. Input context is approaching a heavy local-model turn."
        if turns >= MAX_HISTORY_TURNS:
            return "- Conversation is at the retained-turn limit; older turns are being trimmed automatically."
        if turns >= MAX_HISTORY_TURNS - 3:
            return "- Several turns are in context. Clear when the current topic is done."
        return "- Resource use looks reasonable for the current dialogue."

    def resource_recommendations(self):
        last_in = self.last_turn_tokens["in"]
        turns = self.user_turn_count()
        recommendations = []
        if last_in >= 12000:
            recommendations.append("- Clear now unless the next question needs this full dialogue.")
            recommendations.append("- Save or export anything important before clearing.")
        elif last_in >= 8000:
            recommendations.append("- Finish the current task, then clear before changing topics.")
            recommendations.append("- Import fewer or smaller files, or ask for a summary first.")
        elif turns >= MAX_HISTORY_TURNS - 3:
            recommendations.append("- Clear when this topic is done; older turns will soon be trimmed.")
        else:
            recommendations.append("- Continue normally.")
        recommendations.append("- Start a new dialogue for unrelated work.")
        recommendations.append("- Use concise dialogue defaults for code or document tasks.")
        return recommendations

    def resource_status_text(self):
        last_in = self.last_turn_tokens["in"]
        turns = self.user_turn_count()
        if last_in >= 12000:
            return "Context: Clear Soon"
        if last_in >= 8000 or turns >= MAX_HISTORY_TURNS - 3:
            return "Context: Growing"
        return "Context: OK"

    def update_resource_indicator(self):
        self.resource_var.set(self.resource_status_text())

    def update_memory_indicator(self):
        snapshot = system_resource_snapshot()
        total = snapshot["total"]
        reclaimable = snapshot["reclaimable"]
        if not total or reclaimable is None:
            self.memory_var.set("Resources: unavailable")
            return
        used = max(total - reclaimable, 0)
        used_pct = used / total * 100
        self.memory_var.set(
            f"Resources: memory {used_pct:.0f}% used / {reclaimable / 2**30:.1f} GB avail"
        )

    def start_resource_refresh(self):
        if self.resource_after is None:
            self.resource_after = self.after(RESOURCE_REFRESH_MS, self.refresh_resources_live)

    def stop_resource_refresh(self):
        if self.resource_after is not None:
            self.after_cancel(self.resource_after)
            self.resource_after = None

    def refresh_resources_live(self):
        self.resource_after = None
        self.update_memory_indicator()
        self.start_resource_refresh()

    def handle_escape(self, _event=None):
        if self.busy:
            self.cancel_current_response()
        else:
            self.status_var.set("Nothing running")
        return "break"

    def cancel_current_response(self):
        self.cancel_requested = True
        self.status_var.set("Stopping current response...")
        self.working_var.set("Stopping")
        self.send_button.configure(state="normal")

    def request_tool_approval(self, description):
        event = threading.Event()
        result = {"approved": False, "cancelled": False}
        self.events.put(("approval", description, event, result))
        while not event.wait(0.1):
            if self.cancel_requested:
                result["cancelled"] = True
                return False
        return result["approved"]

    def execute_tool(self, name, args):
        name = str(name or "").strip().lower().replace(".", ":").replace("/", ":").rsplit(":", 1)[-1]
        if name == "run_command":
            command = args.get("command", "")
            try:
                mkdir_parts = shlex.split(command)
            except ValueError:
                mkdir_parts = []
            if mkdir_parts[:2] == ["mkdir", "-p"] and len(mkdir_parts) > 2:
                root = DOWNLOADS.resolve()
                targets = [pathlib.Path(part).expanduser().resolve(strict=False) for part in mkdir_parts[2:]]
                if all(target.is_relative_to(root) for target in targets) and all(target.exists() for target in targets):
                    return "exit_code=0\n(no action; requested directories already exist)"
            if not self.request_tool_approval(f"Run command:\n{command}"):
                return "User declined."
            try:
                cwd = gui_infer_command_cwd(command)
                proc = subprocess.run(command, shell=True, capture_output=True, text=True, timeout=180, cwd=cwd)
                output = (proc.stdout + proc.stderr).strip()[:MAX_FILE_CHARS]
                location = f"\nworking_directory={cwd}" if cwd else ""
                return f"exit_code={proc.returncode}{location}\n{output or '(no output)'}"
            except subprocess.TimeoutExpired:
                return "Command timed out."
        if name == "python_interpreter":
            code = args.get("code", "")
            if any(term in code for term in ("open(", "write_text(", "makedirs(", "mkdir(", "os.remove(", "unlink(")):
                return "Error: use write_file for file creation and run_command for execution; python_interpreter is disabled for filesystem writes."
            if not self.request_tool_approval(f"Run Python code ({len(code)} chars)"):
                return "User declined."
            try:
                proc = subprocess.run(["python3", "-c", code], capture_output=True, text=True, timeout=180)
                output = (proc.stdout + proc.stderr).strip()[:MAX_FILE_CHARS]
                return f"exit_code={proc.returncode}\n{output or '(no output)'}"
            except subprocess.TimeoutExpired:
                return "Python execution timed out."
        if name == "read_file":
            path = pathlib.Path(args.get("path", "")).expanduser()
            if path.is_dir():
                return (f"Error: {path} is a directory, not a file. "
                        f"Use run_command with 'ls' or 'find' to see its contents.")
            try:
                text = path.read_text(errors="replace")
                return text[:MAX_FILE_CHARS] + ("\n[truncated]" if len(text) > MAX_FILE_CHARS else "")
            except Exception as exc:
                return f"Error: {exc}"
        if name == "write_file":
            raw_path = args.get("path", "")
            content = args.get("content", args.get("text", ""))
            target = resolve_output_path(raw_path)
            old = ""
            if target.exists():
                try:
                    old = target.read_text(errors="replace")
                except Exception:
                    old = ""
                if target.suffix == ".py" and "zcta" in old.lower() and re.search(r"\bzzcta\b", content, re.IGNORECASE):
                    return "Error: rejected suspicious overwrite; proposed Python content changes zcta to zzcta in an existing validated script. Inspect and preserve the existing file."
            # Existing-file overwrites are gated by the approval dialog below (which
            # shows a diff), not by guessing intent from the request's wording — that
            # keyword-based pre-check silently blocked legitimate requests before.
            preview = f"Write {len(content)} chars to:\n{target}"
            if old and old != content:
                diff_lines = list(difflib.unified_diff(
                    old.splitlines(), content.splitlines(),
                    fromfile="current", tofile="proposed", lineterm=""))[:40]
                if diff_lines:
                    preview += "\n\n" + "\n".join(diff_lines)
            if not self.request_tool_approval(preview):
                return "User declined."
            backup_path = backup_before_overwrite(target) if (old and old != content) else None
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content)
                note = f" (previous version backed up to {backup_path})" if backup_path else ""
                return f"Written: {target} ({target.stat().st_size} bytes).{note}"
            except Exception as exc:
                return f"Error: {exc}"
        return f"Unknown tool: {name}"

    def finish_canceled_response(self, partial_text):
        if self.current_stream_start and self.current_stream_end and partial_text:
            self.style_assistant_range(self.current_stream_start, self.current_stream_end)
        if self.pending_user_index is not None and self.pending_user_index < len(self.messages):
            pending = self.messages[self.pending_user_index]
            if pending.get("role") == "user" and pending.get("content") == self.last_user_text:
                del self.messages[self.pending_user_index]
        self.pending_user_index = None
        self.cancel_requested = False
        self.busy = False
        self.stop_working()
        self.send_button.configure(state="normal")
        self.current_stream_start = None
        self.current_stream_end = None
        self.append_tagged("\n[stopped - partial reply not kept in context]\n", "meta")
        self.update_resource_indicator()
        self.status_var.set("Stopped")

    def append(self, text):
        self.chat.insert("end", text)
        if not self.chat.tag_ranges("sel"):
            self.chat.see("end")

    def append_tagged(self, text, tag):
        self.chat.insert("end", text, tag)
        if not self.chat.tag_ranges("sel"):
            self.chat.see("end")

    def append_model_text(self, text):
        start = self.chat.index("end-1c")
        self.chat.insert("end", text, "assistant_body")
        end = self.chat.index("end-1c")
        self.current_stream_end = end
        if not self.chat.tag_ranges("sel"):
            self.chat.see("end")

    def style_assistant_range(self, start, end):
        self.apply_markdown_line_tags(start, end)
        self.apply_inline_code_tag(start, end)
        self.raise_text_selection()

    def apply_markdown_line_tags(self, start, end):
        text = self.chat.get(start, end)
        offset = 0
        in_fence = False
        for line in text.splitlines(True):
            stripped = line.strip()
            line_start = f"{start}+{offset}c"
            line_end = f"{start}+{offset + len(line)}c"
            if stripped.startswith("```"):
                self.chat.tag_add("code_block", line_start, line_end)
                in_fence = not in_fence
            elif in_fence:
                self.chat.tag_add("code_block", line_start, line_end)
            elif stripped.startswith("# "):
                self.chat.tag_add("heading", line_start, line_end)
            elif stripped.startswith(("## ", "### ")):
                self.chat.tag_add("subheading", line_start, line_end)
            elif stripped.startswith(("- ", "* ")) or (len(stripped) > 3 and stripped[0].isdigit() and stripped[1:3] in (". ", ") ")):
                self.chat.tag_add("bullet", line_start, line_end)
            offset += len(line)

    def apply_inline_code_tag(self, start, end):
        text = self.chat.get(start, end)
        offset = 0
        while True:
            left = text.find("`", offset)
            if left < 0:
                break
            right = text.find("`", left + 1)
            if right < 0:
                break
            self.chat.tag_add("code", f"{start}+{left}c", f"{start}+{right + 1}c")
            offset = right + 1

    def copy_text(self, text):
        self.clipboard_clear()
        self.clipboard_append(text)
        self.status_var.set(f"Copied {len(text)} chars")

    def select_all_input(self, _event=None):
        self.input.focus_set()
        self.input.tag_remove("sel", "1.0", "end")
        self.input.tag_add("sel", "1.0", "end-1c")
        self.input.mark_set("insert", "end-1c")
        self.raise_text_selection()
        self.status_var.set("Selected all input text")
        return "break"

    def copy_input_selection(self, _event=None):
        try:
            selected = self.input.get("sel.first", "sel.last")
        except tk.TclError:
            selected = ""
        if not selected:
            self.status_var.set("No input text selected")
            return "break"
        self.clipboard_clear()
        self.clipboard_append(selected)
        self.status_var.set("Copied selected input text")
        return "break"

    def select_all_chat(self, _event=None):
        self.chat.focus_set()
        self.chat.tag_remove("sel", "1.0", "end")
        self.chat.tag_add("sel", "1.0", "end-1c")
        self.chat.mark_set("insert", "end-1c")
        self.raise_text_selection()
        self.status_var.set("Selected all chat text")
        return "break"

    def copy_chat_selection(self, _event=None):
        try:
            selected = self.chat.get("sel.first", "sel.last")
        except tk.TclError:
            selected = ""
        if not selected:
            self.status_var.set("No chat text selected")
            return "break"
        self.clipboard_clear()
        self.clipboard_append(selected)
        self.status_var.set("Copied selected chat text")
        return "break"

    def latest_reply_text(self):
        for message in reversed(self.messages):
            if message.get("role") == "assistant" and message.get("content"):
                return message["content"]
        return ""

    def copy_latest_reply(self):
        content = self.latest_reply_text()
        if not content:
            self.status_var.set("No model reply to copy")
            return
        self.clipboard_clear()
        self.clipboard_append(content)
        self.status_var.set("Copied latest reply")
        return "break"

    def autosave_docx_requested(self):
        text = self.last_user_text.lower()
        wants_doc = any(term in text for term in ("word document", "docx", ".docx", "export as word", "save as word"))
        wants_save = any(term in text for term in ("save", "export", "create", "make", "generate"))
        return wants_doc and wants_save

    def default_docx_path(self):
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        text = self.last_user_text.lower()
        if "omlx" in text and ("mlxgui" in text or "mlxcli" in text):
            name = "omlx-mlxcli-mlxgui-setup"
        else:
            name = "mlxgui-reply"
        return DOWNLOADS / f"{name}-{stamp}.docx"

    def save_docx_content(self, content, target=None):
        target = pathlib.Path(target or self.default_docx_path()).expanduser()
        target.parent.mkdir(parents=True, exist_ok=True)
        markdown_to_docx(target, "mlxgui Export", content)
        self.status_var.set(f"Saved {target.name}")
        self.append(f"\n[saved Word document to {target}]\n")
        return target

    def save_latest_docx(self):
        content = self.latest_reply_text()
        if not content:
            messagebox.showinfo("No reply", "There is no model reply to save yet.")
            return
        try:
            self.save_docx_content(content)
        except Exception as exc:
            messagebox.showerror("Save failed", str(exc))

    def paste_into_input(self, _event=None):
        self.input.focus_set()
        text = ""
        try:
            text = self.clipboard_get()
        except tk.TclError:
            pass
        if not text and shutil.which("pbpaste"):
            try:
                text = subprocess.run(
                    ["pbpaste"], capture_output=True, text=True, timeout=2
                ).stdout
            except Exception:
                text = ""
        if not text:
            self.status_var.set("Clipboard is empty or unavailable")
            return "break"
        paths = self.clipboard_file_paths(text)
        if paths:
            self.import_paths(paths, source="pasted")
            return "break"
        try:
            self.input.delete("sel.first", "sel.last")
        except tk.TclError:
            pass
        self.input.insert("insert", text)
        self.input.see("insert")
        self.status_var.set(f"Pasted {len(text)} chars")
        return "break"

    def clipboard_file_paths(self, text):
        candidates = []
        if shutil.which("pbpaste"):
            try:
                file_text = subprocess.run(
                    ["pbpaste", "-Prefer", "file"], capture_output=True, text=True, timeout=2
                ).stdout
                candidates.extend(file_text.splitlines())
            except Exception:
                pass
        candidates.extend(text.splitlines())
        if text.strip().startswith("{") and text.strip().endswith("}"):
            candidates.extend(part for part in text.strip()[1:-1].split("} {") if part)
        paths = []
        for raw in candidates:
            item = raw.strip().strip('"').strip("'")
            if not self.looks_like_file_path(item):
                continue
            if item.startswith("file://"):
                item = urllib.parse.unquote(urllib.parse.urlparse(item).path)
            p = pathlib.Path(item).expanduser()
            try:
                is_file = p.exists() and p.is_file()
            except OSError:
                is_file = False
            if is_file:
                paths.append(str(p))
        deduped = []
        seen = set()
        for path in paths:
            if path not in seen:
                seen.add(path)
                deduped.append(path)
        return deduped

    def looks_like_file_path(self, item):
        if not item or len(item) > 1024 or "\n" in item:
            return False
        if item.startswith("file://"):
            return True
        expanded = item.replace("~", str(pathlib.Path.home()), 1)
        return expanded.startswith("/") or expanded.startswith("./") or expanded.startswith("../")

    def send_from_keyboard(self, event):
        if event.state & 0x0001:
            return None
        self.send()
        return "break"

    def start_server_and_models(self):
        if not ensure_server(self.backend, self.url, self.key, self.status):
            return
        try:
            models = list_models(self.url, self.key)
        except Exception as exc:
            self.status(f"Could not load models: {exc}")
            return
        self.events.put(("models", models))
        self.status("Ready")

    def choose_backend_on_launch(self):
        choices = "\n".join(
            f"{index}) {backend_label(name)} - {backend_description(name)}"
            for index, name in enumerate(SUPPORTED_BACKENDS, 1)
        )
        selected = simpledialog.askstring(
            "mlxgui Backend",
            f"Choose backend (Enter for {backend_label(self.backend)}):\n\n{choices}",
            initialvalue=str(SUPPORTED_BACKENDS.index(self.backend) + 1),
            parent=self,
        )
        if selected is not None and selected.strip():
            selected = selected.strip().lower()
            if selected in SUPPORTED_BACKENDS:
                self.backend = selected
            else:
                try:
                    self.backend = SUPPORTED_BACKENDS[int(selected) - 1]
                except (ValueError, IndexError):
                    self.status_var.set("Invalid backend; using the previous backend")
        self.backend_var.set(backend_label(self.backend))
        self.url, self.key = load_backend_cfg(self.backend)
        save_backend(self.backend)
        stop_other_backend(self.backend, self.status)
        threading.Thread(target=self.start_server_and_models, daemon=True).start()

    def backend_selection_changed(self, _event=None):
        selected_label = self.backend_var.get()
        selected = next(
            (name for name in SUPPORTED_BACKENDS if backend_label(name) == selected_label),
            self.backend,
        )
        self.switch_backend(selected)

    def switch_backend(self, backend):
        if backend == self.backend:
            return
        if self.busy:
            messagebox.showinfo("Backend", "Wait for the current response to finish before switching backends.")
            self.backend_var.set(backend_label(self.backend))
            return
        self.backend = backend
        self.backend_var.set(backend_label(backend))
        self.url, self.key = load_backend_cfg(backend)
        save_backend(backend)
        self.model_var.set("")
        self.model_box.configure(values=[])
        self.status_var.set(f"Switching to {backend_label(backend)}...")
        threading.Thread(target=self.restart_backend, daemon=True).start()

    def restart_backend(self):
        stop_other_backend(self.backend, self.status)
        self.start_server_and_models()

    def import_files(self):
        paths = filedialog.askopenfilenames(
            title="Import one or more files",
            initialdir=str(DOWNLOADS),
        )
        if not paths:
            return
        self.import_paths(paths)

    def insert_file_references(self):
        paths = filedialog.askopenfilenames(
            title="Select files to reference in your request",
            initialdir=str(DOWNLOADS),
        )
        if not paths:
            return
        references = "\n".join(f"- {pathlib.Path(path)}" for path in paths)
        existing = self.input.get("1.0", "end-1c").strip()
        prefix = f"{existing}\n\n" if existing else ""
        self.input.delete("1.0", "end")
        self.input.insert("1.0", f"{prefix}Files:\n{references}\n")
        self.input.focus_set()
        self.status_var.set(f"Added {len(paths)} file reference(s) to the request")

    def import_paths(self, paths, source="imported"):
        mode = self.convert_var.get()
        sections = []
        labels = []
        try:
            for path in paths:
                text, label = convert_file(path, mode)
                clipped = text[:MAX_FILE_CHARS]
                note = "" if len(text) <= MAX_FILE_CHARS else "\n[truncated]"
                sections.append(f"Contents of {label}:\n\n{clipped}{note}")
                labels.append(label)
        except Exception as exc:
            messagebox.showerror("Import failed", f"No files were imported.\n\n{exc}")
            return
        content = "\n\n---\n\n".join(sections)
        self.messages.append({"role": "user", "content": content})
        self.messages = trim(self.messages)
        self.append(f"\n[{source} {len(labels)} file(s) as {mode}: {', '.join(labels)}]\n")
        self.update_resource_indicator()
        self.status_var.set(f"Imported {len(labels)} file(s)")

    def export_reply(self):
        content = self.latest_reply_text()
        if not content:
            messagebox.showinfo("No reply", "There is no model reply to export yet.")
            return
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        path = filedialog.asksaveasfilename(
            title="Export latest model reply",
            initialdir=str(DOWNLOADS),
            initialfile=f"mlxgui-reply-{stamp}.docx",
            defaultextension=".docx",
            filetypes=[
                ("Word document", "*.docx"),
                ("Markdown", "*.md"),
                ("Plain text", "*.txt"),
            ],
        )
        if not path:
            return
        target = pathlib.Path(path).expanduser()
        try:
            if target.suffix.lower() == ".docx":
                markdown_to_docx(target, "mlxgui Export", content)
            elif target.suffix.lower() == ".md":
                target.write_text(content)
            elif target.suffix.lower() == ".txt":
                target.write_text(markdown_to_text(content))
            else:
                target.write_text(content)
        except Exception as exc:
            messagebox.showerror("Export failed", str(exc))
            return
        self.status_var.set(f"Exported {target.name}")
        self.append(f"\n[exported latest reply to {target}]\n")

    def dialogue_context_data(self):
        return {
            "version": 1,
            "saved_at": datetime.now().isoformat(timespec="seconds"),
            "system_prompt": self.system_prompt,
            "messages": self.messages,
            "totals": self.totals,
            "last_turn_tokens": self.last_turn_tokens,
            "model": self.model_var.get(),
            "convert_mode": self.convert_var.get(),
            "rag_folder": self.chat_rag_folder_var.get(),
        }

    def save_dialogue_context(self):
        SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        path = filedialog.asksaveasfilename(
            title="Save dialogue context",
            initialdir=str(SESSIONS_DIR),
            initialfile=f"mlxgui-context-{stamp}.json",
            defaultextension=".json",
            filetypes=[("Dialogue context", "*.json")],
        )
        if not path:
            return
        target = pathlib.Path(path).expanduser()
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps(self.dialogue_context_data(), indent=2))
        except Exception as exc:
            messagebox.showerror("Save failed", str(exc))
            return
        self.status_var.set(f"Saved dialogue context to {target.name}")

    def load_dialogue_context(self):
        SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
        path = filedialog.askopenfilename(
            title="Load dialogue context",
            initialdir=str(SESSIONS_DIR),
            filetypes=[("Dialogue context", "*.json"), ("JSON", "*.json")],
        )
        if not path:
            return
        try:
            data = json.loads(pathlib.Path(path).expanduser().read_text())
            messages = data.get("messages")
            if not isinstance(messages, list) or not messages:
                raise ValueError("No saved message context found.")
            self.system_prompt = data.get("system_prompt") or DEFAULT_SYSTEM
            self.messages = messages
            if self.messages[0].get("role") != "system":
                self.messages.insert(0, {"role": "system", "content": self.system_prompt})
            else:
                self.messages[0]["content"] = self.system_prompt
            totals = data.get("totals") or {}
            self.totals = {"in": int(totals.get("in") or 0), "out": int(totals.get("out") or 0)}
            last_turn = data.get("last_turn_tokens") or {}
            self.last_turn_tokens = {
                "in": int(last_turn.get("in") or 0),
                "out": int(last_turn.get("out") or 0),
            }
            mode = data.get("convert_mode")
            if mode in CONVERT_MODES:
                self.convert_var.set(mode)
            self.chat_rag_folder_var.set(data.get("rag_folder") or "")
            model = data.get("model")
            if model:
                self.model_var.set(model_label(model) if self.model_display_to_id else model)
        except Exception as exc:
            messagebox.showerror("Load failed", str(exc))
            return
        self.render_loaded_context()
        self.tokens_var.set(f"tokens: in {self.totals['in']:,} / out {self.totals['out']:,}")
        self.update_resource_indicator()
        self.update_memory_indicator()
        self.current_stream_start = None
        self.current_stream_end = None
        self.pending_user_index = None
        self.cancel_requested = False
        self.status_var.set(f"Loaded dialogue context from {pathlib.Path(path).name}")

    def render_loaded_context(self):
        self.chat.configure(state="normal")
        self.chat.delete("1.0", "end")
        for message in self.messages:
            role = message.get("role")
            content = message.get("content") or ""
            if role == "system" or not content:
                continue
            if role == "user":
                self.append_tagged(f"\n{content}\n", "user_bubble")
            elif role == "assistant":
                self.append_tagged("Assistant\n", "assistant_label")
                start = self.chat.index("end-1c")
                self.append_model_text(content)
                end = self.chat.index("end-1c")
                self.style_assistant_range(start, end)
            elif role == "tool":
                self.append_tagged(f"\n[tool]\n{content}\n", "meta")

    def reset_chat_state(self, keep_rag=True):
        if not keep_rag:
            self.chat_rag_folder_var.set("")
        self.messages = [{"role": "system", "content": self.system_prompt}]
        notes_path, notes_text = find_project_notes()
        if notes_text:
            self.messages.append({"role": "system", "content": f"Project notes from {notes_path}:\n\n{notes_text}"})
        self.totals = {"in": 0, "out": 0}
        self.last_turn_tokens = {"in": 0, "out": 0}
        self.last_user_text = ""
        self.refining = False
        self.pending_user_index = None
        self.cancel_requested = False
        self.tokens_var.set("tokens: in 0 / out 0")
        self.update_resource_indicator()
        self.update_memory_indicator()
        self.chat.configure(state="normal")
        self.chat.delete("1.0", "end")
        self.input.delete("1.0", "end")
        self.current_stream_start = None
        self.current_stream_end = None

    def new_chat(self):
        keep_rag = True
        if self.chat_rag_folder_var.get().strip():
            keep = messagebox.askyesnocancel(
                "New Chat",
                "Keep the current chat RAG folder for the new chat?\n\n"
                "Yes: start a new chat and keep the RAG folder.\n"
                "No: start a new chat and clear the RAG folder.\n"
                "Cancel: stay in the current chat.",
            )
            if keep is None:
                return
            keep_rag = keep
        self.reset_chat_state(keep_rag=keep_rag)
        if keep_rag and self.chat_rag_folder_var.get().strip():
            self.status_var.set("Started new chat and kept the current RAG folder")
        else:
            self.status_var.set("Started new chat")

    def clear_chat(self):
        self.reset_chat_state(keep_rag=True)
        self.status_var.set("Cleared current chat")

    def send(self):
        if self.busy or self.refining:
            return
        text = self.input.get("1.0", "end-1c").strip()
        if not text:
            return
        if text.startswith("?"):
            self.input.delete("1.0", "end")
            self.refine_prompt(text[1:].strip() or None)
            return
        model = self.selected_model_id()
        if not model:
            messagebox.showinfo("No model", "Wait for models to load first.")
            return
        self.input.delete("1.0", "end")
        self.last_user_text = text
        self.messages.append({"role": "user", "content": text})
        self.messages = trim(self.messages)
        self.pending_user_index = len(self.messages) - 1
        self.cancel_requested = False
        self.update_resource_indicator()
        self.append_tagged(f"\n{text}\n", "user_bubble")
        self.append_tagged("Assistant\n", "assistant_label")
        self.current_stream_start = self.chat.index("end-1c")
        self.current_stream_end = self.current_stream_start
        self.busy = True
        self.send_button.configure(state="disabled")
        self.start_working("Waiting for model response")
        threading.Thread(target=self.stream_reply, args=(model,), daemon=True).start()

    def stream_reply(self, model):
        request_messages, context_status, context_error = request_messages_with_context(
            self.messages,
            self.last_user_text,
            self.chat_rag_folder_var.get(),
        )
        if context_status:
            self.events.put(("status", context_status))
        if context_error:
            self.events.put(("append", f"\n[error] {context_error}\n"))
            self.events.put(("done", "", {}))
            return
        working_messages = list(request_messages)
        agentic = gui_should_auto_enable_agentic(self.last_user_text)
        execution_required = gui_requires_agentic_execution(self.last_user_text)
        contract = gui_execution_contract(self.last_user_text)
        tool_state = {"write": False, "run": False, "verify": False}
        seen_tool_calls = {}
        retried_with_tools = False
        repetition_streak = 0
        if agentic:
            working_messages.insert(1, {"role": "system", "content": (
                "Agentic local-resource task: use tools for all file reads, writes, commands, and verification. "
                "Do not simulate tool calls or claim completion without successful tool results and exact path evidence. "
                "Do not repeat identical tool calls after a successful result; reuse the returned evidence, and preserve stronger existing file validation. write_file always shows the user a preview and asks for approval before an existing file is changed, so call it directly rather than staging a copy elsewhere first. "
                "write_file creates parent directories, so do not issue a separate mkdir unless it is actually required."
            )})
        effective_agentic = agentic
        for _step in range(MAX_TOOL_STEPS):
            payload = {
                "model": model, "messages": working_messages,
                "max_tokens": MAX_RESPONSE_TOKENS, "stream": True,
                "stream_options": {"include_usage": True},
                "repetition_penalty": DEFAULT_REPETITION_PENALTY,
            }
            if effective_agentic:
                payload["tools"] = TOOLS
            parts, calls, usage = [], {}, {}
            stream_error = None
            repetition_detected = False
            chunks_since_check = 0
            try:
                with urllib.request.urlopen(request(self.url, self.key, "/v1/chat/completions", payload), timeout=900) as resp:
                    for rawline in resp:
                        if self.cancel_requested:
                            self.events.put(("canceled", "".join(parts)))
                            return
                        line = rawline.decode("utf-8", errors="replace").strip()
                        if not line.startswith("data:"):
                            continue
                        data = line[5:].strip()
                        if data == "[DONE]":
                            break
                        try:
                            chunk = json.loads(data)
                        except json.JSONDecodeError:
                            continue
                        if chunk.get("error"):
                            stream_error = chunk["error"]
                            break
                        if chunk.get("usage"):
                            usage = chunk["usage"]
                        choices = chunk.get("choices") or []
                        if not choices:
                            continue
                        delta = choices[0].get("delta") or {}
                        piece = delta.get("content")
                        if piece:
                            parts.append(piece)
                            if not effective_agentic:
                                self.events.put(("append", piece))
                            chunks_since_check += 1
                            # Check periodically rather than on every token — cheap
                            # at this cadence, and stopping within ~15 chunks of a
                            # stuck model is what matters, not the exact first repeat.
                            if chunks_since_check >= 15:
                                chunks_since_check = 0
                                if gui_detect_repetition_loop("".join(parts)):
                                    repetition_detected = True
                                    break
                        for tool_call in delta.get("tool_calls") or []:
                            index = tool_call.get("index", 0)
                            slot = calls.setdefault(index, {"id": None, "name": "", "arguments": ""})
                            slot["id"] = tool_call.get("id") or slot["id"]
                            function = tool_call.get("function") or {}
                            slot["name"] += function.get("name") or ""
                            slot["arguments"] += function.get("arguments") or ""
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", errors="replace").strip()[:400]
                self.events.put(("append", f"\n[error] HTTP {exc.code}: {detail}\n"))
                self.events.put(("done", "", usage))
                return
            except Exception as exc:
                if self.cancel_requested:
                    self.events.put(("canceled", "".join(parts)))
                else:
                    self.events.put(("append", f"\n[error] {exc}\n"))
                    self.events.put(("done", "", usage))
                return
            if repetition_detected:
                repetition_streak += 1
                self.events.put(("status", "Model generation fell into a repetition loop; stopped early"))
                if repetition_streak >= 2:
                    # It fell into the identical failure mode twice in a row for
                    # this request — further retries are very unlikely to help.
                    self.events.put(("append", "\n[stopped: the model repeated the same generation-loop failure twice in a row; try rephrasing the request]\n"))
                    self.events.put(("done", "", usage))
                    return
            else:
                repetition_streak = 0
            if stream_error:
                # The model attempted a tool call the server couldn't resolve, most often
                # because conversation history primed it to keep calling tools even on a
                # turn where none were declared. Give it a real chance to use one before
                # treating the truncated response as if it were a normal, complete answer.
                if not effective_agentic and not retried_with_tools:
                    retried_with_tools = True
                    effective_agentic = True
                    self.events.put(("status", "Server rejected an unexpected tool call; retrying with tools enabled"))
                    continue
                self.events.put(("append", f"\n[server error mid-generation: {stream_error}; response above may be incomplete]\n"))
            tool_calls = [{"id": slot["id"] or f"gui_call_{index}", "type": "function",
                           "function": {"name": slot["name"], "arguments": slot["arguments"]}}
                          for index, slot in sorted(calls.items())]
            content = "".join(parts)
            if not tool_calls and effective_agentic:
                tool_calls = parse_gui_text_tool_calls(content)
                if tool_calls:
                    self.events.put(("status", "Compatibility text tool call parsed"))
            if not tool_calls and effective_agentic:
                tool_calls = gui_parse_bare_json_tool_call(content)
                if tool_calls:
                    self.events.put(("status", "Compatibility bare-JSON tool call parsed"))
            if not tool_calls and effective_agentic:
                tool_calls = gui_parse_xml_tag_tool_call(content)
                if tool_calls:
                    self.events.put(("status", "Compatibility XML-tag tool call parsed"))
            if not tool_calls and effective_agentic:
                tool_calls = gui_parse_python_call_tool_call(content)
                if tool_calls:
                    self.events.put(("status", "Compatibility Python-call-style tool call parsed"))
            if not tool_calls:
                if execution_required and _step == 0:
                    # The model answered with prose and never attempted a tool
                    # call at all - force at least one real attempt before
                    # falling back to the narrower per-category contract check
                    # below, which can legitimately require nothing yet still
                    # need enforcement (e.g. a bare "open <path>" request).
                    working_messages.append({"role": "assistant", "content": content})
                    working_messages.append({"role": "user", "content": (
                        "Execution required for this request. You did not call a tool. "
                        "Do not report completion. Use run_command, read_file, or write_file now to perform the "
                        "requested actions, then verify the exact output paths before responding."
                    )})
                    self.events.put(("status", "Model returned prose without performing the requested file operation; requesting tool execution"))
                    continue
                unmet = [name for name, required in contract.items() if required and not tool_state.get(name)]
                if unmet and _step < MAX_TOOL_STEPS - 1:
                    working_messages.append({"role": "assistant", "content": content})
                    working_messages.append({"role": "user", "content": (
                        "Do not claim completion. Missing tool evidence for: " + ", ".join(unmet) +
                        ". Use the available tools and verify exact paths before responding."
                    )})
                    self.events.put(("status", f"Requesting missing tool actions: {', '.join(unmet)}"))
                    continue
                if unmet:
                    self.events.put(("append", f"\n[unverified: missing tool evidence for {', '.join(unmet)}]\n"))
                    self.events.put(("done", "", usage))
                    return
                if effective_agentic and content:
                    self.events.put(("append", content))
                self.events.put(("done", content, usage))
                return
            clean_content = "" if "call:" in content else content
            assistant_tool_message = {"role": "assistant", "content": clean_content or None, "tool_calls": tool_calls}
            working_messages.append(assistant_tool_message)
            self.events.put(("tool_history", assistant_tool_message))
            repeated_failure = False
            for call in tool_calls:
                call["function"]["name"] = gui_normalize_tool_name(call["function"]["name"])
                try:
                    args = json.loads(call["function"].get("arguments") or "{}")
                except json.JSONDecodeError:
                    args = {}
                self.events.put(("status", f"Running tool: {call['function']['name']}"))
                call_key = (call["function"]["name"], json.dumps(args, sort_keys=True, ensure_ascii=False))
                if call_key in seen_tool_calls:
                    result = seen_tool_calls[call_key]
                    self.events.put(("status", "Duplicate tool call suppressed; reusing prior result"))
                    if gui_tool_failed(result):
                        repeated_failure = True
                else:
                    result = self.execute_tool(call["function"]["name"], args)
                    seen_tool_calls[call_key] = result
                if self.cancel_requested:
                    self.events.put(("canceled", content))
                    return
                if not gui_tool_failed(result):
                    if call["function"]["name"] == "write_file":
                        tool_state["write"] = True
                    if call["function"]["name"] in {"run_command", "python_interpreter"}:
                        tool_state["run"] = True
                    if call["function"]["name"] == "read_file" or (
                        call["function"]["name"] in {"run_command", "python_interpreter"}
                        and "stat " in args.get("command", "")
                    ):
                        tool_state["verify"] = True
                working_messages.append({"role": "tool", "tool_call_id": call["id"], "content": result})
                self.events.put(("tool_history", working_messages[-1]))
            if repeated_failure:
                self.events.put(("append", "\n[stopped: the model repeated an already-failed tool call instead of adapting]\n"))
                self.events.put(("done", "", usage))
                return
        self.events.put(("append", "\n[stopped: too many tool steps]\n"))
        self.events.put(("done", "", {}))

    def selected_model_id(self):
        selected = self.model_var.get()
        return resolve_model_id(selected, self.model_display_to_id)

    def drain_events(self):
        try:
            while True:
                event = self.events.get_nowait()
                kind = event[0]
                if kind == "approval":
                    _kind, description, approval_event, approval_result = event
                    if self.cancel_requested or approval_result.get("cancelled"):
                        approval_result["approved"] = False
                    else:
                        approval_result["approved"] = messagebox.askyesno("Approve local tool action", description, parent=self)
                    approval_event.set()
                elif kind == "tool_history":
                    self.messages.append(event[1])
                elif kind == "append":
                    self.append_model_text(event[1])
                elif kind == "refined":
                    _raw_request, refined, usage = event[1], event[2], event[3]
                    self.refining = False
                    self.refine_button.configure(state="normal")
                    self.stop_working()
                    in_tokens, out_tokens = usage_counts(usage)
                    self.totals["in"] += in_tokens
                    self.totals["out"] += out_tokens
                    if refined:
                        self.input.delete("1.0", "end")
                        self.input.insert("1.0", refined)
                        self.input.focus_set()
                        self.status_var.set(
                            f"Prompt refined; review and press Send (in {in_tokens:,} / out {out_tokens:,})"
                        )
                    else:
                        self.status_var.set("Refiner returned no text; original prompt retained")
                elif kind == "refine_error":
                    self.refining = False
                    self.refine_button.configure(state="normal")
                    self.stop_working()
                    self.status_var.set("Prompt refinement failed")
                    self.append(f"\n[refiner error] {event[1]}\n")
                elif kind == "status":
                    self.status_var.set(event[1])
                elif kind == "models":
                    models = event[1]
                    labels = [model_label(model) for model in models]
                    self.model_display_to_id = dict(zip(labels, models))
                    self.model_box.configure(values=labels)
                    if models:
                        self.model_var.set(labels[0])
                elif kind == "done":
                    content, usage = event[1], event[2]
                    self.pending_user_index = None
                    self.cancel_requested = False
                    if content:
                        if self.current_stream_start and self.current_stream_end:
                            self.style_assistant_range(self.current_stream_start, self.current_stream_end)
                        self.messages.append({"role": "assistant", "content": content})
                        if self.autosave_docx_requested():
                            try:
                                self.save_docx_content(content)
                            except Exception as exc:
                                self.append(f"\n[could not save Word document: {exc}]\n")
                    in_tokens, out_tokens = usage_counts(usage)
                    self.last_turn_tokens = {"in": in_tokens, "out": out_tokens}
                    self.totals["in"] += in_tokens
                    self.totals["out"] += out_tokens
                    self.tokens_var.set(
                        f"tokens: in {self.totals['in']:,} / out {self.totals['out']:,}"
                    )
                    self.update_resource_indicator()
                    self.update_memory_indicator()
                    self.status_var.set(f"Ready - last turn: in {in_tokens:,} / out {out_tokens:,}")
                    self.busy = False
                    self.stop_working()
                    self.send_button.configure(state="normal")
                    self.current_stream_start = None
                    self.current_stream_end = None
                elif kind == "canceled":
                    self.finish_canceled_response(event[1])
        except queue.Empty:
            pass
        self.after(60, self.drain_events)


if __name__ == "__main__":
    MlxGui().mainloop()
