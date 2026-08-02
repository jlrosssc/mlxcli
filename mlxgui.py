#!/usr/bin/env python3
"""Lightweight Tkinter chat GUI for oMLX with Markdown file import."""
import json
import os
import pathlib
import queue
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
MAX_FILE_CHARS = 8000
MAX_HISTORY_TURNS = 12
CONVERTIBLE = {".docx", ".pdf", ".pptx", ".xlsx", ".doc"}
CONVERT_MODES = ("auto", "all", "off")
DOWNLOADS = pathlib.Path.home() / "Downloads"
DEFAULT_SYSTEM = (
    "You are a concise assistant running locally on the user's Mac. "
    "Save created files in ~/Downloads unless the user gives another path. "
    "Keep answers brief and practical."
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
        self.messages = [{"role": "system", "content": DEFAULT_SYSTEM}]
        self.totals = {"in": 0, "out": 0}
        self.events = queue.Queue()
        self.model_var = tk.StringVar()
        self.convert_var = tk.StringVar(value="auto")
        self.status_var = tk.StringVar(value="Starting")
        self.tokens_var = tk.StringVar(value="tokens: in 0 / out 0")
        self.busy = False
        self.last_user_text = ""
        self.current_stream_start = None
        self.current_stream_end = None

        self.build_ui()
        threading.Thread(target=self.start_server_and_models, daemon=True).start()
        self.after(60, self.drain_events)

    def build_ui(self):
        self.build_menu()
        top = ttk.Frame(self, padding=(10, 8))
        top.pack(fill="x")

        ttk.Label(top, text="Model").pack(side="left")
        self.model_box = ttk.Combobox(top, textvariable=self.model_var, state="readonly", width=38)
        self.model_box.pack(side="left", padx=(6, 12))

        ttk.Label(top, text="Imports").pack(side="left")
        ttk.OptionMenu(top, self.convert_var, self.convert_var.get(), *CONVERT_MODES).pack(
            side="left", padx=(6, 12)
        )
        ttk.Button(top, text="Clear", command=self.clear_chat).pack(side="left", padx=(0, 6))
        ttk.Button(top, text="Import", command=self.import_files).pack(side="left")
        ttk.Button(top, text="Export", command=self.export_reply).pack(side="left", padx=(6, 0))
        ttk.Button(top, text="DOCX", command=self.save_latest_docx).pack(side="left", padx=(6, 0))
        ttk.Button(top, text="All", command=self.select_all_chat).pack(side="left", padx=(6, 0))
        ttk.Button(top, text="Copy", command=self.copy_chat_selection).pack(side="left", padx=(6, 0))
        ttk.Button(top, text="Copy Reply", command=self.copy_latest_reply).pack(side="left", padx=(6, 0))
        ttk.Label(top, textvariable=self.tokens_var).pack(side="right")

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
        ttk.Button(bottom, text="Paste", command=self.paste_into_input).pack(side="left", padx=(8, 0))
        self.send_button = ttk.Button(bottom, text="Send", command=self.send)
        self.send_button.pack(side="left", padx=(8, 0))

        status = ttk.Label(self, textvariable=self.status_var, anchor="w", padding=(10, 0, 10, 8))
        status.pack(fill="x")

    def build_menu(self):
        menu = tk.Menu(self)
        edit = tk.Menu(menu, tearoff=False)
        edit.add_command(label="Copy", accelerator="Cmd+C", command=self.menu_copy)
        edit.add_command(label="Paste", accelerator="Cmd+V", command=self.menu_paste)
        edit.add_command(label="Select All", accelerator="Cmd+A", command=self.menu_select_all)
        edit.add_separator()
        edit.add_command(label="Clear Chat", command=self.clear_chat)
        menu.add_cascade(label="Edit", menu=edit)
        self.config(menu=menu)
        self.bind_all("<Command-c>", lambda _event: self.menu_copy())
        self.bind_all("<Command-C>", lambda _event: self.menu_copy())
        self.bind_all("<Control-c>", lambda _event: self.menu_copy())
        self.bind_all("<Control-C>", lambda _event: self.menu_copy())
        self.bind_all("<Command-v>", lambda _event: self.menu_paste())
        self.bind_all("<Command-V>", lambda _event: self.menu_paste())
        self.bind_all("<Control-v>", lambda _event: self.menu_paste())
        self.bind_all("<Control-V>", lambda _event: self.menu_paste())
        self.bind_all("<Command-a>", lambda _event: self.menu_select_all())
        self.bind_all("<Command-A>", lambda _event: self.menu_select_all())
        self.bind_all("<Control-a>", lambda _event: self.menu_select_all())
        self.bind_all("<Control-A>", lambda _event: self.menu_select_all())

    def focused_text_widget(self):
        widget = self.focus_get()
        return widget if isinstance(widget, tk.Text) else None

    def menu_copy(self):
        widget = self.focused_text_widget()
        if widget is self.chat:
            return self.copy_chat_selection()
        if widget is self.input:
            try:
                selected = self.input.get("sel.first", "sel.last")
            except tk.TclError:
                selected = ""
            if selected:
                self.clipboard_clear()
                self.clipboard_append(selected)
                self.status_var.set("Copied selected input text")
            return "break"
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
            self.input.tag_add("sel", "1.0", "end-1c")
            self.input.mark_set("insert", "end-1c")
            self.status_var.set("Selected all input text")
            return "break"
        return self.select_all_chat()

    def status(self, text):
        self.events.put(("status", text))

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

    def select_all_chat(self, _event=None):
        self.chat.focus_set()
        self.chat.tag_remove("sel", "1.0", "end")
        self.chat.tag_add("sel", "1.0", "end-1c")
        self.chat.mark_set("insert", "end-1c")
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
            if not item:
                continue
            if item.startswith("file://"):
                item = urllib.parse.unquote(urllib.parse.urlparse(item).path)
            p = pathlib.Path(item).expanduser()
            if p.exists() and p.is_file():
                paths.append(str(p))
        deduped = []
        seen = set()
        for path in paths:
            if path not in seen:
                seen.add(path)
                deduped.append(path)
        return deduped

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

    def clear_chat(self):
        self.messages = [{"role": "system", "content": DEFAULT_SYSTEM}]
        self.totals = {"in": 0, "out": 0}
        self.tokens_var.set("tokens: in 0 / out 0")
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
        self.append_tagged(f"\n{text}\n", "user_bubble")
        self.append_tagged("Assistant\n", "assistant_label")
        self.current_stream_start = self.chat.index("end-1c")
        self.current_stream_end = self.current_stream_start
        self.busy = True
        self.send_button.configure(state="disabled")
        threading.Thread(target=self.stream_reply, args=(model,), daemon=True).start()

    def stream_reply(self, model):
        payload = {
            "model": model,
            "messages": self.messages,
            "max_tokens": 2000,
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
                        parts.append(piece)
                        self.events.put(("append", piece))
        except Exception as exc:
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
                    self.totals["in"] += in_tokens
                    self.totals["out"] += out_tokens
                    self.tokens_var.set(
                        f"tokens: in {self.totals['in']:,} / out {self.totals['out']:,}"
                    )
                    self.status_var.set(f"Ready - last turn: in {in_tokens:,} / out {out_tokens:,}")
                    self.busy = False
                    self.send_button.configure(state="normal")
                    self.current_stream_start = None
                    self.current_stream_end = None
        except queue.Empty:
            pass
        self.after(60, self.drain_events)


if __name__ == "__main__":
    MlxGui().mainloop()
