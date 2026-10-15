"""MCP tool servers for mlxcli: `--artcraft` (PdfCraft, ArtCraft app suite) and `--rea` (REA reverse engineering).

A tiny stdlib-only MCP stdio client. Each server is launched on first use, kept alive for the run, and a curated
allow-list of its tools becomes mlxcli plugin-style entries (schema + run + approval category) so the local model
sees a few dozen schemas, not hundreds. Only servers named on the command line are loaded.

Env: PDFCRAFT_CLI (default ~/bin/pdfcraft-cli), PDFCRAFT_ROOT (default ~/pdfcraft-trial),
     REA_JS (default ~/LocalAI/rea/node_modules/rea-agents/scripts/rea.mjs), REA_TOOLS (comma list overriding the REA allow-list).
"""
import atexit, json, os, pathlib, select, subprocess

HOME = pathlib.Path.home()
PDF_CLI = pathlib.Path(os.environ.get("PDFCRAFT_CLI", HOME / "bin" / "pdfcraft-cli"))
ROOT = pathlib.Path(os.environ.get("PDFCRAFT_ROOT", HOME / "pdfcraft-trial"))   # kept for mlxcli's banner
REA_JS = pathlib.Path(os.environ.get("REA_JS", HOME / "LocalAI" / "rea" / "node_modules" / "rea-agents" / "scripts" / "rea.mjs"))
TIMEOUT = 300
MAX_RESULT_CHARS = 14000

# REA ships 207 tools. Default set = static, read-only, small-schema ones (~6K tokens in all). Left out on purpose:
# trace_application_feature / trace_javascript_semantics (~8K tokens of schema between them), inspect_binary_layout,
# inspect_with_objdump, inspect_sqlite_database -- enable any of them with REA_TOOLS=a,b,c. The native and Android
# tools need an external engine (Ghidra/Hopper, jadx) and report that when it is missing.
REA_ALLOW = (
    "open_binary", "binary_session", "close_binary",
    "inspect_artifact", "inspect_macho", "inspect_plist", "inspect_signature", "list_architectures",
    "analyze_javascript_application", "inspect_electron_fuses",
    "binary_overview", "search_strings", "procedure_pseudo_code", "analyze_function",
    "inspect_android_package", "search_android_classes", "inspect_android_class", "inspect_android_method",
)
PDF_ALLOW = ("doc_open", "doc_info", "doc_save", "doc_close", "text_extract", "text_find", "doc_combine",
             "doc_split", "edit_undo", "tool_search", "tool_call")

SERVERS = {
    "pdfcraft": {
        "cmd": lambda: [str(PDF_CLI), "mcp", "--root", str(ROOT), "--compact"],
        "cwd": lambda: ROOT, "prefix": "pdfcraft_", "category": "pdfcraft", "allow": PDF_ALLOW,
        "check": lambda: PDF_CLI.exists() or f"{PDF_CLI} not found (github.com/storytold/pdfcraft/releases)",
    },
    "rea": {
        "cmd": lambda: ["node", str(REA_JS), "mcp"],
        "cwd": lambda: REA_JS.parent.parent.parent.parent if REA_JS.exists() else HOME,
        "prefix": "rea_", "category": "rea",
        "allow": tuple(t.strip() for t in os.environ["REA_TOOLS"].split(",")) if os.environ.get("REA_TOOLS") else REA_ALLOW,
        "check": lambda: REA_JS.exists() or f"{REA_JS} not found (cd ~/LocalAI/rea && npm install rea-agents --ignore-scripts)",
    },
}
CATEGORIES = tuple(s["category"] for s in SERVERS.values())


class _Client:
    def __init__(self, key):
        self.key, self.cfg, self.proc, self.next_id = key, SERVERS[key], None, 0

    def _send(self, obj):
        self.proc.stdin.write(json.dumps(obj) + "\n")
        self.proc.stdin.flush()

    def _recv(self, want_id):
        while True:
            ready, _, _ = select.select([self.proc.stdout], [], [], TIMEOUT)
            if not ready:
                raise RuntimeError(f"{self.key} MCP server gave no reply within {TIMEOUT}s")
            line = self.proc.stdout.readline()
            if not line:
                raise RuntimeError(f"{self.key} MCP server exited")
            try:
                msg = json.loads(line)
            except ValueError:
                continue   # stray non-protocol line on stdout
            if msg.get("id") == want_id:
                return msg

    def request(self, method, params=None):
        self.next_id += 1
        self._send({"jsonrpc": "2.0", "id": self.next_id, "method": method, "params": params or {}})
        msg = self._recv(self.next_id)
        if "error" in msg:
            raise RuntimeError(msg["error"].get("message", str(msg["error"])))
        return msg["result"]

    def start(self):
        if self.proc and self.proc.poll() is None:
            return
        ok = self.cfg["check"]()
        if ok is not True:
            raise RuntimeError(ok)
        cwd = self.cfg["cwd"]()
        pathlib.Path(cwd).mkdir(parents=True, exist_ok=True)
        self.proc = subprocess.Popen(self.cfg["cmd"](), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.DEVNULL, text=True, cwd=str(cwd))
        atexit.register(self.stop)
        self.request("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                    "clientInfo": {"name": "mlxcli", "version": "1"}})
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()

    def call(self, name, args):
        self.start()
        try:
            res = self.request("tools/call", {"name": name, "arguments": args or {}})
        except RuntimeError as exc:   # protocol-level error (e.g. unknown argument): hand it to the model to correct
            return f"Error: {exc}"
        parts = []
        for block in res.get("content", []):
            parts.append(block["text"] if block.get("type") == "text" else f"[{block.get('type', 'binary')} content omitted]")
        text = "\n".join(parts) or "(no output)"
        if len(text) > MAX_RESULT_CHARS:
            text = (text[:MAX_RESULT_CHARS] + f"\n... [truncated {len(text) - MAX_RESULT_CHARS} chars; "
                    "ask for a narrower slice (page, filter, one function) instead of everything]")
        return f"Error: {text}" if res.get("isError") else text


_clients = {}


def _client(key):
    if key not in _clients:
        _clients[key] = _Client(key)
    return _clients[key]


def register(registry, keys):
    """Add the chosen servers' allow-listed tools to mlxlib's plugin registry. Returns the names added."""
    added = []
    for key in keys:
        cl, cfg = _client(key), SERVERS[key]
        cl.start()
        for tool in cl.request("tools/list")["tools"]:
            if tool["name"] not in cfg["allow"]:
                continue
            desc = tool.get("description", "")
            if key == "pdfcraft" and tool["name"] == "doc_open":
                desc += f" Files must be under {ROOT} (relative paths resolve there)."
            full = cfg["prefix"] + tool["name"]
            ro = bool(tool.get("annotations", {}).get("readOnlyHint"))
            registry[full] = {
                "schema": {"type": "function", "function": {"name": full, "description": desc,
                                                            "parameters": tool["inputSchema"]}},
                "run": lambda args, _c=cl, _n=tool["name"]: _c.call(_n, args),
                "category": cfg["category"], "requires_approval": not ro,
            }
            added.append(full)
    return added


def stop():
    for cl in _clients.values():
        cl.stop()


def task_note(keys):
    notes = []
    if "pdfcraft" in keys:
        notes.append(f"[PdfCraft tools: use the pdfcraft_* tools for PDF work. Files live under {ROOT}. "
                     "ALWAYS call pdfcraft_doc_open first: the doc id it returns is required by every other tool; "
                     "page numbers are 1-based. Never overwrite an original: write results to a new file name. "
                     "Use pdfcraft_tool_search to find other operations and pdfcraft_tool_call to run them.]")
    if "rea" in keys:
        notes.append("[REA reverse-engineering tools: use the rea_* tools, which are read-only static analysis "
                     "(nothing is executed). Start with rea_open_binary on the absolute path of the target (app bundle, "
                     "APK, ASAR/JS folder, binary), then the inspect_/analyze_ tools. Report only what the tool output "
                     "shows, quote the evidence, and say plainly what could not be determined or what engine is missing. "
                     "Only analyze software the user owns or is permitted to analyze.]")
    return "\n".join(notes)
