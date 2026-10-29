# Tool manifest reference (`hal-module.toml`)

Every Hal tool module ships a `hal-module.toml` beside its code. The hub reads it without
importing the module: it names the module, says how to run its tools, and declares each tool's
tier, flags and parameters. This page is the schema; how the hub finds manifests, resolves
interpreters and runs tools is in [04-hub](../design/04-hub.md) ("Discovery and registry",
"Runner"), and the worker side of the wire is in [worker-protocol.md](worker-protocol.md).
`hal-hub new NAME` writes a valid v2 pack to start from, and `hal-hub doctor` checks a
manifest against its code.

A manifest is a TOML file named `hal-module.toml`. Unknown keys are errors in v2 (they catch
typos) and warnings in v1.

## Top level

| Key | Type | Required | Meaning |
| --- | --- | --- | --- |
| `schema` | integer | yes for v2 | `2`. Absent means v1. Any other value is an error |
| `[module]` | table | yes | The module |
| `[[tools]]` | array of tables | no | The tools; a module with only units may have none |
| `[[install]]` | array of tables | no | Install steps, run only after the user confirms each one |
| `[[units]]` | array of tables | no | systemd user units shipped with the module |
| `[[desktop]]` | array of tables | no | Desktop launchers shipped with the module |

## `[module]`

| Key | Type | Default | Meaning |
| --- | --- | --- | --- |
| `name` | string | required | `^[a-z][a-z0-9]{0,31}$`. Tool name prefix and, by convention, the module's config table name |
| `pack` | string | the module name | The pack this module belongs to; the `pack` in the policy's approval key and in `[packs.<pack>]` policy rules |
| `summary` | string | required | One line, at most 200 characters |
| `version` | string | distribution version, else `"0"` | Dotted numbers; compared by `requires` of other modules; the policy's `pack_version` |
| `python` | string | none | Import path of the module holding the tool functions, such as `halcal.tools`. Exactly one of `python` and `exec` when `[[tools]]` is not empty |
| `exec` | array of strings | none | Command for a non-Python worker; placeholders allowed (see [Placeholders](#placeholders)). hal-voice registers this way |
| `protocol` | string | `"jsonl"` (v2), `"oneshot"` (v1) | `exec` only: `jsonl` speaks the [worker protocol](worker-protocol.md), `oneshot` the v1 per-call protocol |
| `interpreter` | array of strings | see [04-hub, Interpreter](../design/04-hub.md#interpreter) | `python` only, path-scanned modules only: the Python to run the worker with, such as `["{root}/.venv/bin/python"]`. Must be Python 3.11 or later |
| `inprocess` | boolean | `false` | Asks to run inside the hub process. Honoured only for the built-in module and modules listed in `[hub.runner].inprocess_modules` |
| `requires` | array of strings | `[]` | Other modules this one needs: `"notify"`, `"memory>=0.1"`. Operators `>=`, `>`, `<=`, `<`, `==`, `!=`, comma-joined |
| `calls` | array of strings | `[]` | Tool names this module's code may call through the nested-call channel |
| `requires_hal` | string | none | Hub versions the module works with, such as `">=0.1,<0.2"` |
| `env` | array of strings | `[]` | Names of extra environment variables passed to the worker, such as `"OLLAMA_HOST"`. Never secrets: those go in `[[module.secrets]]` |
| `domains` | array of strings | `[]` | Hosts the module's network tools contact; shown at install, used by the policy as the tools' default `domains` |
| `[[module.secrets]]` | array of tables | none | Secrets the module's tools need; keys `name`, `config_key`, `default_path` as defined in [01-security, Secrets](../design/01-security.md#secrets) |
| `startup_timeout` | number | `[hub.runner].startup_timeout` (20) | Seconds the worker may take to send `ready` |
| `max_workers` | integer | `[hub.runner].max_workers_per_module` (2) | Concurrent workers for this module, 1 to 8 |
| `keywords` | string or array of strings | `""` | Search words added to every tool of the module |

## `[[tools]]`

| Key | Type | Default | Meaning |
| --- | --- | --- | --- |
| `name` | string | required | `^<module>_[a-z0-9]+(_[a-z0-9]+)*$`, at most 64 characters, no `__` |
| `function` | string | `name` | Function in the `python` module, or the function name sent to an `exec` worker |
| `title` | string | none | Human-readable name for client display |
| `description` | string | required | What the tool does, for the model; at most 2000 characters |
| `tier` | string | required in v2 | `read`, `write`, `act`, `send` or `system` (meanings: [01-security](../design/01-security.md)) |
| `network` | boolean | `false` | The tool reaches the network (egress); `false` lets the runner drop the network  |
| `untrusted_output` | boolean | `false` | The result can contain attacker-controlled text (mail, web, feeds, files, screen, clipboard) |
| `reviewable` | boolean | `false` | A reviewer model may approve an `ask` call to this tool ([01-security](../design/01-security.md)) |
| `reversible` | boolean | `false` | An `act` tool whose effect another call undoes and that touches no user data; the shipped `default` profile allows it through `act_reversible` |
| `runs_code` | boolean | `false` | The tool runs caller-supplied commands or code on the host; the policy makes it `deny` unless the user sets `ask` by name |
| `routine` | inline table | none | `{id = "<param>", steps = "<param>"}`: approving this tool approves the step list in `steps` for the routine named by `id`. Only the routines tools set it |
| `timeout` | number | `60` | Seconds, from 1 to `[hub.runner].max_timeout` (7200); a larger value is an error in v2 and is clamped with a warning in v1 |
| `always_offer` | boolean | `false` | Offer this tool to a chat model on every turn, before search hits |
| `keywords` | string or array of strings | `""` | Extra search words, such as everyday names for the tool's job |
| `max_output` | integer | `[hub.runner].max_result_chars` (20000) | Characters of result returned to the caller before cutting |
| `[tools.params.<name>]` | table | none | One table per parameter; `<name>` matches `^[a-z][a-z0-9_]{0,63}$` |

There is no per-tool `default` verdict key: a pack can never relax policy for itself.
Verdicts other than the tier default come from the policy profiles and the user's
`policy.toml`.

## `[tools.params.<name>]`

| Key | Type | Default | Meaning |
| --- | --- | --- | --- |
| `type` | string | `"string"` | `string`, `integer`, `number` or `boolean` |
| `description` | string | `""` | For the model |
| `required` | boolean | `false` | Must be present |
| `enum` | array | none | Allowed values, of the parameter's type; enforced by the policy's `decide` |
| `default` | value of `type` | none | Documentation only: shown in the schema. The hub does not fill it in; the function's own default applies, and `doctor` checks they agree |
| `list` | boolean | `false` | `string` only: the value is a comma-separated list; the function receives `list[str]` or `list[int]` |
| `item_type` | string | `"string"` | With `list = true`: `string` or `integer` |
| `minimum`, `maximum` | number | none | Bounds for `integer` and `number`, inclusive; checked by the hub |
| `max_length` | integer | `65536` | Maximum characters of a `string` (the policy's default); all arguments together are also capped at 1 MiB of canonical JSON |
| `aliases` | array of strings | `[]` | Other names accepted for this parameter, such as `["when"]` for `when_text`; applied for every front end |
| `format` | string | `"text"` | `text`, `path`, `url`, `host`, `email`, `phone`, `command` or `json`. The policy checks `path`, `url` and `host`; `email` and `phone` are display hints; `command` marks an argv-like string; `json` is a string holding JSON, checked by the hub (see [04-hub, Coercion](../design/04-hub.md#coercion)) |
| `access` | string | `"read"` | `read` or `write`; only with `format = "path"`, selects the policy's `read_roots` or `write_roots` |
| `content` | boolean | `false` | A message body (mail body, SMS text, note text): summarised in the audit log unless `[audit].content = true` in `policy.toml`, and never routed to a surface without `shows_all_args` |

## `[[install]]`

| Key | Type | Default | Meaning |
| --- | --- | --- | --- |
| `id` | string | required | `^[a-z0-9-]{1,40}$`, unique in the module |
| `summary` | string | required | What the step does and roughly how much it downloads, shown before asking |
| `run` | array of strings | required | argv, no shell; placeholders allowed |
| `timeout` | number | `600` | Seconds, at most 7200 |
| `network` | boolean | `false` | The step downloads (shown to the user) |
| `cwd` | string | `"root"` | `root` (the manifest's directory) or `state` (the module's state directory) |

A step that needs root is not allowed; the module's README tells the user the command instead.

## `[[units]]` and `[[desktop]]`

| Table | Key | Type | Default | Meaning |
| --- | --- | --- | --- | --- |
| `[[units]]` | `file` | string | required | Path relative to the manifest directory, ending in `.service`, `.timer`, `.target` or `.path` |
| `[[units]]` | `gpu` | boolean | `false` | The unit uses the GPU; the renderer (06-core-cli) adds `X-HalGPU=yes` and the off-flag condition. This flag, not the template, carries `X-HalGPU` |
| `[[units]]` | `enable` | boolean | `true` | Enable when the unit has an `[Install]` section and is not a template |
| `[[desktop]]` | `file` | string | required | Path to a `.desktop` file relative to the manifest directory |

## Placeholders

In `exec`, `interpreter`, `install.run`: `{root}` (the manifest's directory), `{python}` (the
interpreter chosen for the module, see [04-hub, Interpreter](../design/04-hub.md#interpreter)), `{state}` (`state_dir()/modules/<name>`),
`{data}` (`data_dir()/modules/<name>`). Substitution is plain text inside each argv element.
Unit files keep the renderer's `@ROOT@`-style placeholders (06-core-cli).

## Example

```toml
schema = 2

[module]
name = "calendar"
pack = "organiser"
summary = "Events, reminders, ICS import and export, CalDAV sync"
python = "halcal.tools"
requires = ["notify"]
calls = ["notify_send"]
keywords = ["diary", "agenda"]

[[module.secrets]]
name = "caldav_password"
config_key = "calendar.caldav_password_file"
default_path = "~/.config/hal/caldav-password"

[[tools]]
name = "calendar_add"
function = "add"
description = "Add an event. Times are plain English, such as 'thursday 10am'."
tier = "write"
reviewable = true
reversible = true
keywords = "appointment meeting remind schedule book"
[tools.params.title]
description = "what the event is"
required = true
max_length = 200
[tools.params.when_text]
description = "when, in plain words"
required = true
aliases = ["when"]
[tools.params.reminders]
description = "minutes before the event to remind, comma-separated"
list = true
item_type = "integer"

[[tools]]
name = "calendar_import"
function = "import_ics"
description = "Import events from an .ics file."
tier = "act"
untrusted_output = true
[tools.params.path]
description = "the .ics file"
required = true
format = "path"
access = "read"
max_length = 4096

[[units]]
file = "systemd/hal-calendar-reminders.service"
```

## v1 compatibility

| v1 | Loaded as |
| --- | --- |
| no `schema` | v1: unknown keys warn |
| `confirm = true` or no `confirm` | `tier = "act"`, `reviewable = false`. Every shipped pack has v2 manifests, so this affects only third-party v1 packs |
| no `pack` | `pack` = module name |
| `policy.toml` verdict by tool name | applies as for any tool ([01-security](../design/01-security.md) syntax) |
| `type = "array"` | `string` with `list = true`, `item_type = "string"` |
| `type = "object"` | `string` with `format = "json"`; for v1 modules only, the worker receives the parsed object |
| `exec` | `protocol = "oneshot"`, `cwd` = module root |
| `install` (argv in `[module]`) | one `[[install]]` step with `id = "setup"`, `timeout = 3600`, `network = true` |
| no `[[units]]` | `systemd/*` and `desktop/*.desktop` by convention |
| `python` module found on disk | worker gets `PYTHONPATH=<root>` |
| param names starting with `_` | not present in any of the 33 manifests [verified: parsed all with `tomllib`]; rejected in v1 too |
