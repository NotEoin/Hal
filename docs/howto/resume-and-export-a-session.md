# Resume and export a session

This page shows how to find an earlier `hal-agent` session, continue it and export it. Unverified
against a real model: the steps are covered by tests with fake servers only.

## Steps

1. List the sessions started in the current directory, newest first.

   ```sh
   hal-agent sessions list   # id, start time (UTC), model, directory; add --all for every directory
   ```

2. Read a short overview of one session before you continue it.

   ```sh
   hal-agent sessions show 3f9a1c0b7e2d   # start time, directory, model, message count, how each turn ended
   ```

3. Continue it by id, or continue the latest session of this directory.

   ```sh
   hal-agent --resume 3f9a1c0b7e2d   # the agent replays the stored history, summaries included
   hal-agent --continue              # the newest session whose directory is this one
   ```

4. Export it as Markdown to share, or as records for tools. Secrets are masked on write, so neither contains them.

   ```sh
   hal-agent sessions export 3f9a1c0b7e2d --out session.md          # Markdown, ASCII only
   hal-agent sessions export 3f9a1c0b7e2d --format jsonl --out s.jsonl   # one record per line
   hal-agent sessions export 3f9a1c0b7e2d --reasoning               # also the model's reasoning
   ```

5. Run without a transcript when the task is private or a throwaway.

   ```sh
   hal-agent --no-session -p "explain this error"   # nothing is written to the session store
   ```

## When it does not work

| Symptom | Fix |
| --- | --- |
| `sessions list` prints nothing | The sessions were started in another directory: add `--all`, or pass `--cwd DIR` |
| `invalid session id` | An id is 12 lowercase hex characters; copy it from `sessions list` |
| `--resume` says the session is unknown | Pass `--sessions-dir` if you used one when the session was written |
| Resumed session forgets old details | Old turns were compacted into a summary; export the session to read the full records |
