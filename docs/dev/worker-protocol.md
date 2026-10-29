# Worker protocol

How Hal's hub talks to a tool worker. The hub starts one worker process per module (more
under load), sends it one call at a time over stdin and reads results from stdout. Python
modules use the hub's own worker script (`halhub/_worker/hal_worker.py`); a module with
`exec` and `protocol = "jsonl"` brings its own program, which must speak this protocol
(`hal-hub new NAME --exec` writes a stdlib example). v1 `exec` modules use the
[one-shot protocol](#one-shot-protocol) instead. The manifest keys are in
[manifest.md](manifest.md); timeouts, the process sandbox, secrets and nested calls are in
[04-hub](../design/04-hub.md), section "Runner".

## Version 1

Framing: UTF-8 JSON objects, one per line, `\n`-terminated, `ensure_ascii=True`, at most
8 MiB per line (`MAX_MESSAGE_BYTES`: bounds hub memory per call). Every message has `"v": 1`
and `"op"`. A worker handles one `call` at a time.

| Direction | `op` | Fields |
| --- | --- | --- |
| worker -> hub | `ready` | `pid`, `python`, `module`, `functions` (name -> signature), `missing` (requested names not found) |
| hub -> worker | `call` | `id`, `function`, `args`, `secrets` (object, may be empty), `timeout_s` (informational; the hub enforces it) |
| worker -> hub | `result` | `id`, `ok`, and `result` or `error` (`type`, `message`) |
| worker -> hub | `tool_call` | `id`, `parent` (the running call's id), `tool`, `args` |
| hub -> worker | `tool_result` | `id`, `ok`, and `result` or `error` (`type`, `message`) |
| hub -> worker | `ping` | `id` |
| worker -> hub | `pong` | `id` |
| hub -> worker | `shutdown` | none; the worker exits 0 |

Examples:

```json
{"v":1,"op":"ready","pid":4321,"python":"3.13.11","module":"halcal.tools","functions":{"add":{"params":[{"name":"title","required":true},{"name":"when_text","required":true},{"name":"reminders","required":false,"default":"None"}],"var_keyword":false,"async":false}},"missing":[]}
{"v":1,"op":"call","id":"c-000001","function":"add","args":{"title":"Dentist","when_text":"thursday 10am"},"secrets":{},"timeout_s":60}
{"v":1,"op":"tool_call","id":"n-000001","parent":"c-000001","tool":"notify_send","args":{"title":"Added Dentist"}}
{"v":1,"op":"tool_result","id":"n-000001","ok":true,"result":"sent"}
{"v":1,"op":"result","id":"c-000001","ok":true,"result":{"id":"ev42","title":"Dentist","start":"2026-10-29T10:00:00Z"}}
{"v":1,"op":"result","id":"c-000002","ok":false,"error":{"type":"ValueError","message":"no event matches 'dentist'"}}
```

Worker rules:

- Before calling, bind the arguments with `inspect.signature(fn).bind(**args)`; a failure
  returns `error.type = "ArgumentsMismatch"` (the manifest and the code disagree).
- A coroutine function runs under `asyncio.run`.
- `result` is serialised with `default=str` (dates, paths) and `allow_nan=False`; a value that
  still fails returns `error.type = "UnserialisableResult"`.
- An exception returns its class name and `str(exc)` cut to 2000 characters. The traceback
  goes to stderr only.
- Unknown `op`: reply `{"v":1,"op":"result","id":<id or null>,"ok":false,"error":{"type":"ProtocolError",...}}`.

Hub mapping of `error.type` to the caller's `error_type`: `ArgumentsMismatch` ->
`manifest_mismatch`; `UnserialisableResult` -> `tool_error`; `ProtocolError` ->
`protocol_error`; anything else -> `tool_error` with message `"<type>: <message>"` (just
`<message>` when the type is `ToolError`).

The worker's stdout carries only protocol lines. The reference worker keeps the original
stdout for the protocol and points both `sys.stdout` and file descriptor 1 at stderr, so a
tool's `print` or `os.write(1, ...)` lands in the hub's log. Stderr is logged, never parsed. A
call to a function the worker does not have answers `ProtocolError`.

## One-shot protocol

Run `[*exec, function]` with `cwd` = module root, JSON arguments on stdin, through
`halkit.proc.run` with the tool's timeout (process group kill). Exit 0: stdout parsed as JSON,
or returned as text when it is not JSON. Non-zero exit: `tool_error` with the last 800
characters of stderr (or stdout), as v1 did.
