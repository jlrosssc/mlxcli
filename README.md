# mlxcli

A minimal local agent CLI for oMLX.

`mlxcli` talks to the local oMLX OpenAI-compatible server, streams responses,
keeps bounded chat history, supports approval-gated tools, previews file-write
diffs, and can paste converted documents into context with `markitdown`.

## Install

```bash
cp mlxcli ~/bin/mlxcli
chmod +x ~/bin/mlxcli
```

Optional lightweight GUI:

```bash
cp mlxgui.py ~/bin/mlxgui
chmod +x ~/bin/mlxgui
```

`mlxcli` reads the oMLX API key from `~/.omlx/settings.json` by default. You can
override the server with `OMLX_URL` and the key with `OMLX_API_KEY`.

## Usage

```bash
mlxcli
mlxcli <model>
mlxcli <model> -p "custom system prompt"
```

If the oMLX server is not running, `mlxcli` opens the oMLX app and waits for the
server to come up. It does not launch the companion dashboard viewer.

## Commands

```text
/paste <file>   inject a file into context
/convert MODE   set paste conversion: auto, all, or off
/model          switch models
/models         list server models
/resources      show dialogue pressure, token totals, and Mac memory status
/mem            show local memory status and top memory users
/tokens         show running session token totals
/save [file]    save the transcript as markdown
/export [file]  save the latest model reply as .docx, .md, or .txt
/clear          wipe conversation history
/help           show command help
/exit           quit
```

## Saving Created Documents

Both `mlxcli` and `mlxgui.py` use `~/Downloads` as the default file location for
created or exported files.

In the CLI:

```text
/export
/export family-guide.docx
/export notes.md
/save
```

`/export` saves the latest model reply. With no filename, it creates a Word
document named like `~/Downloads/mlxcli-reply-20260801-211500.docx`. Relative
filenames such as `family-guide.docx` are also saved in `~/Downloads`.

`/save` saves the full transcript as Markdown in `~/Downloads` unless you give a
full path.

In the GUI, use **Export Reply**. The save dialog starts in `~/Downloads` and can
write `.docx`, `.md`, or `.txt`.

## Markdown Import

`/paste` can normalize documents to Markdown before they enter the model
context. The conversion mode is controlled with:

```text
/convert auto
/convert all
/convert off
```

`auto` is the default. It converts `.docx`, `.pdf`, `.pptx`, `.xlsx`, and `.doc`
through `markitdown`, then reads ordinary text files directly. `all` sends every
imported file through `markitdown` first. `off` reads files as plain text only.

Install the optional converter:

```bash
pip install markitdown
```

## Token Display

After every model reply, `mlxcli` prints a one-line token summary:

```text
[tokens: in 1,240 / out 315]
```

The input count covers the request sent for that turn, including system prompt,
conversation history, user message, and any tool-step follow-up requests. The
output count covers generated model tokens. Use `/tokens` to show running totals
for the current CLI session.

These per-turn numbers are meant as an early warning gauge. If input tokens
start drifting into large counts as a session ages, use `/clear` before context
pressure becomes a failure.

Use `/resources` for a combined view of retained dialogue turns, last-turn
tokens, session totals, import caps, practical recommendations, Mac memory,
Metal cap, and top memory users. In the GUI, use the **Resources** button for
the same kind of snapshot. The GUI toolbar also shows a small context pressure
indicator: `Context: OK`, `Context: Growing`, or `Context: Clear Soon`.

## Dialogue Defaults

`mlxcli` and `mlxgui` share a lightweight default instruction file at
`~/.omlx/mlx_system_prompt.txt`. The built-in defaults keep replies concise,
avoid hidden reasoning, keep code answers focused on final code, and remind the
model to respect local memory limits.

In `mlxgui`, open **Defaults > Dialogue Options...** or use the **Defaults**
button to edit and save those instructions. The dialog includes presets for
concise agent conversation, code-focused replies, and document drafting.

In `mlxcli`:

```bash
/system
/system save Keep answers short and show code only unless I ask for explanation.
/system preset concise
/system preset code
/system preset docs
/system default
```

## Lightweight GUI

`mlxgui.py` is a small Tkinter interface for the same local oMLX chat flow. It
does not embed a browser and does not use WebKit. It provides model selection,
streamed replies, token totals, clear, multi-file import, and latest-reply
export to `.docx`, `.md`, or `.txt`. Every selected file is converted according
to the active `auto`, `all`, or `off` Markdown mode before the batch is added to
context. It also includes Dialogue Options for editing the saved default
instructions used by both the GUI and CLI, plus a Resources snapshot for
deciding when to clear or end a dialogue.

Run it:

```bash
python3 mlxgui.py
```

The GUI is intentionally separate from the CLI. It gives you file picker import
and a transcript window without changing the terminal workflow.

## Companion: omlxpanel

`omlxpanel.py` opens the oMLX admin dashboard in a lightweight native WebKit
window. It is separate from `mlxcli`: the CLI handles the conversation view,
while the panel remains the place for server-wide telemetry such as cache hit
rates, memory tiers, and throughput across clients.

Install the viewer dependency:

```bash
pip install pywebview
```

Run it:

```bash
python3 omlxpanel.py
```

The panel stores its WebKit session data under `~/.omlxpanel-data`.
