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
/model          switch models
/models         list server models
/mem            show local memory status and top memory users
/tokens         show running session token totals
/save [file]    save the transcript as markdown
/clear          wipe conversation history
/help           show command help
/exit           quit
```

Supported `/paste` conversions: `.docx`, `.pdf`, `.pptx`, `.xlsx`, and `.doc`
when `markitdown` is installed.

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
