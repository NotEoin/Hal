# Approve tool calls

Hal asks before a tool call that the policy does not allow on its own. This page shows how to
see why, answer a request, and make a rule last. The rules behind it are in
[the security guide](../user/security.md).

## Steps

1. See what the policy says about one call, without running it.

   ```sh
   hal policy explain send_mail --args '{"to": "a@example.org"}'   # prints allow, ask or deny, the rule and the trace
   hal policy explain send_mail --args '{}' --exit-code            # exit 0 allow, 20 ask, 21 deny
   ```

2. Answer a request that is waiting for you. A terminal session asks inline; a channel or a
   background job waits on a control socket.

   ```sh
   hal approvals          # lists every pending request, with the tool and its arguments
   hal approve ID         # allow that request once
   hal deny ID            # refuse it
   ```

   The request shows the arguments as Hal will run them, with control characters escaped.
   Only you can answer: a tool call, a model or a harness cannot (`HAL_HARNESS` refuses).

3. Make a rule last. `allow` shows the change as a diff and writes it only after you type `y`
   on a terminal.

   ```sh
   hal policy allow send_mail --args '{"to": "a@example.org"}'   # one exact argument set
   hal policy allow web_fetch --domain docs.example.org           # one host for a fetch tool
   ```

   A `system` tool can never become `allow`, and a `runs_code` tool only stays `ask`.

4. Edit the whole file when a rule needs more. The edit happens on a copy and the live file is
   replaced only if the copy loads; the old file stays as `policy.toml.bak`.

   ```sh
   hal policy edit        # opens $VISUAL or $EDITOR
   hal policy check       # validates the live file against the installed tools
   ```

5. Review what happened.

   ```sh
   hal audit tail -n 20   # the last records, newest last
   hal audit verify       # exits 1 when the log has a damaged line
   ```

## Symptoms and fixes

| Symptom | Fix |
| --- | --- |
| `hal policy: ... mode 0664 lets others edit it` | `chmod 600` the policy file; Hal refuses a file others can write |
| `hal policy: ... schema_version: must be 1` | The file needs `schema_version = 1` as its first key; `hal policy init` writes one |
| `hal policy allow needs a terminal to confirm` | Run it in a terminal; a pipe or script cannot extend the policy |
| `hal policy edit` prints a load error and changes nothing | Edit again and fix the line it names; the live file was not touched |
| `hal approvals` lists nothing but a call waits | The session that asked has ended; run the call again |
| `no tool named X` from `explain` | `hal-hub tools` lists the names; install the pack that has it |
| A call is denied with `no-approver` | Nobody can answer in that session; start it from a terminal or answer with `hal approve` |
