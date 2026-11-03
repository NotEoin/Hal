# Approve a request from another terminal

A boxed job, a channel or a harness cannot ask you inline. Its request waits on a control socket
until you answer from a terminal of your own. This page shows how. The policy side is in
[approve-tool-calls.md](approve-tool-calls.md).

## Steps

1. Open a second terminal on the same machine and as the same user. The socket lives under your
   runtime directory and is private to you.

2. List what is waiting.

   ```sh
   hal approvals          # one block per pending request: id, tool, arguments, age
   hal approvals --json   # the same as JSON, for a script that only reads
   ```

3. Read the arguments. They are shown as Hal will run them, with control characters escaped.

4. Answer by id.

   ```sh
   hal approve ID         # asks "Allow this exact call? [y/N]"; only y allows it, once
   hal deny ID            # refuses it
   ```

5. Leave a request alone to refuse it. Requests that nobody answers time out to deny.

   When `hal code` or `hal chat` shows the same request, its approval prompt closes as soon as you
   answer here, with the note `approval answered elsewhere`; nothing you type there afterwards
   answers it.

## Symptoms and fixes

| Symptom | Fix |
| --- | --- |
| `hal approvals` lists nothing | The job may have finished or timed out; start it again and list at once |
| `hal approve` exits 2 | It needs a terminal on stdin and stdout and refuses when `HAL_HARNESS` is set; run it in your own shell, not from a harness |
| `no such request` | The id is wrong or the request already ended; list again |
| A request keeps timing out | Answer sooner, or pick the console, which queues requests on a page: `hal console --open` |
