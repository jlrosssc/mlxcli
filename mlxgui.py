#!/usr/bin/env python3
"""Lightweight Tkinter chat GUI for oMLX with Markdown file import."""
import json
import os
import pathlib
import queue
import subprocess
import threading
import time
import tkinter as tk
import urllib.request
from tkinter import filedialog, messagebox, scrolledtext, ttk


SETTINGS = pathlib.Path.home() / ".omlx" / "settings.json"
MAX_FILE_CHARS = 8000
MAX_HISTORY_TURNS = 12
CONVERTIBLE = {".docx", ".pdf", ".pptx", ".xlsx", ".doc"}
CONVERT_MODES = ("auto", "all", "off")
DEFAULT_SYSTEM = (
    "You are a concise assistant running locally on the user's Mac. "
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
        conv = subprocess.run(["markitdown", str(p)], capture_output=True, text=True)
        if conv.returncode != 0 or not conv.stdout.strip():
            raise RuntimeError(f"markitdown could not convert {p.name}")
        return conv.stdout, f"{p.name} (converted to markdown)"
    return p.read_text(errors="replace"), p.name


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

        self.build_ui()
        threading.Thread(target=self.start_server_and_models, daemon=True).start()
        self.after(60, self.drain_events)

    def build_ui(self):
        top = ttk.Frame(self, padding=(10, 8))
        top.pack(fill="x")

        ttk.Label(top, text="Model").pack(side="left")
        self.model_box = ttk.Combobox(top, textvariable=self.model_var, state="readonly", width=38)
        self.model_box.pack(side="left", padx=(6, 12))

        ttk.Label(top, text="Imports").pack(side="left")
        ttk.OptionMenu(top, self.convert_var, self.convert_var.get(), *CONVERT_MODES).pack(
            side="left", padx=(6, 12)
        )
        ttk.Label(top, text="auto converts Office/PDF; text reads directly").pack(
            side="left", padx=(0, 12)
        )

        ttk.Button(top, text="Files", command=self.import_files).pack(side="left")
        ttk.Button(top, text="Clear", command=self.clear_chat).pack(side="left", padx=(6, 0))
        ttk.Label(top, textvariable=self.tokens_var).pack(side="right")

        self.chat = scrolledtext.ScrolledText(self, wrap="word", padx=10, pady=10)
        self.chat.pack(fill="both", expand=True, padx=10)
        self.chat.configure(state="disabled")

        bottom = ttk.Frame(self, padding=10)
        bottom.pack(fill="x")
        self.input = ttk.Entry(bottom)
        self.input.pack(side="left", fill="x", expand=True)
        self.input.bind("<Return>", lambda _event: self.send())
        self.send_button = ttk.Button(bottom, text="Send", command=self.send)
        self.send_button.pack(side="left", padx=(8, 0))

        status = ttk.Label(self, textvariable=self.status_var, anchor="w", padding=(10, 0, 10, 8))
        status.pack(fill="x")

    def status(self, text):
        self.events.put(("status", text))

    def append(self, text):
        self.chat.configure(state="normal")
        self.chat.insert("end", text)
        self.chat.see("end")
        self.chat.configure(state="disabled")

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
        paths = filedialog.askopenfilenames(title="Import files")
        if not paths:
            return
        mode = self.convert_var.get()
        sections = []
        try:
            for path in paths:
                text, label = convert_file(path, mode)
                clipped = text[:MAX_FILE_CHARS]
                note = "" if len(text) <= MAX_FILE_CHARS else "\n[truncated]"
                sections.append(f"Contents of {label}:\n\n{clipped}{note}")
        except Exception as exc:
            messagebox.showerror("Import failed", str(exc))
            return
        content = "\n\n---\n\n".join(sections)
        self.messages.append({"role": "user", "content": content})
        self.messages = trim(self.messages)
        self.append(f"\n[imported {len(paths)} file(s) as {mode}]\n")

    def clear_chat(self):
        self.messages = [{"role": "system", "content": DEFAULT_SYSTEM}]
        self.totals = {"in": 0, "out": 0}
        self.tokens_var.set("tokens: in 0 / out 0")
        self.chat.configure(state="normal")
        self.chat.delete("1.0", "end")
        self.chat.configure(state="disabled")
        self.status_var.set("Cleared")

    def send(self):
        if self.busy:
            return
        text = self.input.get().strip()
        if not text:
            return
        model = self.model_var.get()
        if not model:
            messagebox.showinfo("No model", "Wait for models to load first.")
            return
        self.input.delete(0, "end")
        self.messages.append({"role": "user", "content": text})
        self.messages = trim(self.messages)
        self.append(f"\nYou: {text}\n\nModel: ")
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
                    self.append(event[1])
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
                        self.messages.append({"role": "assistant", "content": content})
                    in_tokens, out_tokens = usage_counts(usage)
                    self.totals["in"] += in_tokens
                    self.totals["out"] += out_tokens
                    self.tokens_var.set(
                        f"tokens: in {self.totals['in']:,} / out {self.totals['out']:,}"
                    )
                    self.append(f"\n[tokens: in {in_tokens:,} / out {out_tokens:,}]\n")
                    self.busy = False
                    self.send_button.configure(state="normal")
                    self.status_var.set("Ready")
        except queue.Empty:
            pass
        self.after(60, self.drain_events)


if __name__ == "__main__":
    MlxGui().mainloop()
