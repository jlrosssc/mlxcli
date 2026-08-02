#!/usr/bin/env python3
"""Lightweight Tkinter chat GUI for oMLX with Markdown file import."""
import json
import os
import pathlib
import queue
import re
import shutil
import subprocess
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
import urllib.parse
import urllib.request
import zipfile
from datetime import datetime
from xml.sax.saxutils import escape
from tkinter import filedialog, messagebox, ttk
from docx_export import markdown_to_docx as formatted_markdown_to_docx


SETTINGS = pathlib.Path.home() / ".omlx" / "settings.json"
SYSTEM_PROMPT_PATH = pathlib.Path.home() / ".omlx" / "mlx_system_prompt.txt"
GUI_DEFAULTS_PATH = pathlib.Path.home() / ".omlx" / "mlxgui_defaults.json"
SESSIONS_DIR = pathlib.Path.home() / ".omlx" / "sessions"
OMLX_BIN = pathlib.Path.home() / ".omlx" / "bin" / "omlx"
MAX_FILE_CHARS = 8000
MAX_HISTORY_TURNS = 12
MAX_RESPONSE_TOKENS = 2500
RESOURCE_REFRESH_MS = 5000
RAG_MAX_FILES = 64
RAG_MAX_CHUNKS = 4
RAG_CHUNK_CHARS = 1800
CONVERTIBLE = {".docx", ".pdf", ".pptx", ".xlsx", ".doc"}
CONVERT_MODES = ("auto", "all", "off")
DOWNLOADS = pathlib.Path.home() / "Downloads"
DEFAULT_SYSTEM = (
    "You are a concise assistant running locally on the user's Mac.\n"
    "Do not reveal hidden reasoning, internal planning, or chain-of-thought.\n"
    "For code requests, give a short practical note and the final code block only unless the user asks for explanation.\n"
    "Save created files in ~/Downloads unless the user gives another path.\n"
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
        url + path,
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


def ensure_server(url, key, status):
    if server_up(url, key):
        return True
    status("Launching oMLX...")
    subprocess.run(["open", "-a", "oMLX"], capture_output=True, text=True)
    if OMLX_BIN.exists():
        status("Starting oMLX server...")
        subprocess.run([str(OMLX_BIN), "start", "--timeout", "60"], capture_output=True, text=True)
    for _ in range(30):
        time.sleep(2)
        if server_up(url, key):
            status("oMLX is up")
            return True
    status("oMLX server not reachable")
    return False


def list_models(url, key):
    data = api(url, key, "/v1/models")
    return [m["id"] for m in data.get("data", [])]


def convert_file(path, mode):
    p = pathlib.Path(path).expanduser()
    should_convert = mode == "all" or (mode == "auto" and p.suffix.lower() in CONVERTIBLE)
    if should_convert:
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


def is_code_request(text):
    lowered = text.lower()
    code_terms = (
        "script", "code", "program", "python", "bash", "shell", "function",
        "create a", "write a", "generate a",
    )
    return any(term in lowered for term in code_terms)


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
        if not path.is_file() or path.name.startswith("."):
            continue
        if path.suffix.lower() in CONVERTIBLE or path.suffix.lower() in {".md", ".txt", ".csv", ".json"}:
            files.append(path)
    return files


def rag_text_for_file(path):
    suffix = path.suffix.lower()
    mode = "all" if suffix in CONVERTIBLE else "off"
    text, label = convert_file(path, mode)
    return text, label


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


def rag_matches_for_query(folder, query):
    matches = []
    for path in rag_candidate_files(folder):
        try:
            text, label = rag_text_for_file(path)
        except Exception:
            continue
        for chunk_label, chunk_text in rag_chunks(text, label):
            score = score_rag_chunk(query, chunk_text)
            if score > 0:
                matches.append((score, chunk_label, chunk_text))
    matches.sort(key=lambda item: item[0], reverse=True)
    return matches[:RAG_MAX_CHUNKS]


def request_messages_with_rag(messages, user_text, rag_folder):
    scoped = request_messages_for_turn(messages, user_text)
    folder = (rag_folder or "").strip()
    if not folder:
        return scoped, None
    matches = rag_matches_for_query(folder, user_text)
    if not matches:
        return scoped, "No matching RAG excerpts were found for this prompt."
    content_lines = ["Relevant reference excerpts from the chat RAG folder:"]
    used_labels = []
    for _score, label, chunk_text in matches:
        used_labels.append(label)
        content_lines.append(f"\n[{label}]\n{chunk_text}")
    insert_at = 1 if scoped and scoped[0].get("role") == "system" else 0
    scoped.insert(insert_at, {"role": "system", "content": "\n".join(content_lines)})
    return scoped, f"RAG used {len(matches)} excerpt(s) from {', '.join(dict.fromkeys(used_labels))}"


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
        return [messages[0]] + messages[cut:]
    return messages


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

        self.url, self.key = load_cfg()
        self.system_prompt = load_system_prompt()
        self.gui_defaults = load_gui_defaults()
        self.messages = [{"role": "system", "content": self.system_prompt}]
        self.totals = {"in": 0, "out": 0}
        self.last_turn_tokens = {"in": 0, "out": 0}
        self.events = queue.Queue()
        self.model_var = tk.StringVar()
        self.convert_var = tk.StringVar(value=self.gui_defaults["convert_mode"])
        self.chat_rag_folder_var = tk.StringVar(value="")
        self.status_var = tk.StringVar(value="Starting")
        self.tokens_var = tk.StringVar(value="tokens: in 0 / out 0")
        self.resource_var = tk.StringVar(value="Context: OK")
        self.memory_var = tk.StringVar(value="Resources: ...")
        self.working_var = tk.StringVar(value="")
        self.busy = False
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
        self.update_memory_indicator()
        self.start_resource_refresh()
        threading.Thread(target=self.start_server_and_models, daemon=True).start()
        self.after(60, self.drain_events)

    def build_ui(self):
        self.build_menu()
        top = ttk.Frame(self, padding=(10, 8))
        top.pack(fill="x")

        ttk.Label(top, text="Model").pack(side="left")
        self.model_box = ttk.Combobox(top, textvariable=self.model_var, state="readonly", width=38)
        self.model_box.pack(side="left", padx=(6, 12))

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
        self.input.bind("<Command-v>", self.paste_into_input)
        self.input.bind("<Command-V>", self.paste_into_input)
        self.input.bind("<Control-v>", self.paste_into_input)
        self.input.bind("<Control-V>", self.paste_into_input)
        self.bind_text_context_menu(self.input)
        ttk.Button(bottom, text="Paste", command=self.paste_into_input).pack(side="left", padx=(8, 0))
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
        view = tk.Menu(menu, tearoff=False)
        view.add_command(label="Resources...", command=self.open_resources)
        menu.add_cascade(label="View", menu=view)
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
        if not ensure_server(self.url, self.key, self.status):
            return
        try:
            models = list_models(self.url, self.key)
        except Exception as exc:
            self.status(f"Could not load models: {exc}")
            return
        self.events.put(("models", models))
        self.status("Ready")

    def import_files(self):
        paths = filedialog.askopenfilenames(
            title="Import one or more files",
            initialdir=str(DOWNLOADS),
        )
        if not paths:
            return
        self.import_paths(paths)

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
                self.model_var.set(model)
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

    def clear_chat(self):
        self.messages = [{"role": "system", "content": self.system_prompt}]
        self.totals = {"in": 0, "out": 0}
        self.last_turn_tokens = {"in": 0, "out": 0}
        self.chat_rag_folder_var.set("")
        self.tokens_var.set("tokens: in 0 / out 0")
        self.update_resource_indicator()
        self.chat.configure(state="normal")
        self.chat.delete("1.0", "end")
        self.status_var.set("Cleared")

    def send(self):
        if self.busy:
            return
        text = self.input.get("1.0", "end-1c").strip()
        if not text:
            return
        model = self.model_var.get()
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
        request_messages, rag_status = request_messages_with_rag(
            self.messages,
            self.last_user_text,
            self.chat_rag_folder_var.get(),
        )
        if rag_status:
            self.events.put(("status", rag_status))
        payload = {
            "model": model,
            "messages": request_messages,
            "max_tokens": MAX_RESPONSE_TOKENS,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        parts = []
        usage = {}
        try:
            with urllib.request.urlopen(
                request(self.url, self.key, "/v1/chat/completions", payload), timeout=900
            ) as resp:
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
                    if chunk.get("usage"):
                        usage = chunk["usage"]
                    choices = chunk.get("choices") or []
                    if not choices:
                        continue
                    piece = (choices[0].get("delta") or {}).get("content")
                    if piece:
                        if self.cancel_requested:
                            self.events.put(("canceled", "".join(parts)))
                            return
                        parts.append(piece)
                        self.events.put(("append", piece))
        except Exception as exc:
            if self.cancel_requested:
                self.events.put(("canceled", "".join(parts)))
            else:
                self.events.put(("append", f"\n[error] {exc}\n"))
                self.events.put(("done", "", {}))
            return
        self.events.put(("done", "".join(parts), usage))

    def drain_events(self):
        try:
            while True:
                event = self.events.get_nowait()
                kind = event[0]
                if kind == "append":
                    self.append_model_text(event[1])
                elif kind == "status":
                    self.status_var.set(event[1])
                elif kind == "models":
                    models = event[1]
                    self.model_box.configure(values=models)
                    if models:
                        self.model_var.set(models[0])
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
