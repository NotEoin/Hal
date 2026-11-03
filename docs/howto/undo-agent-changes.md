# Undo agent changes

The agent works on an isolated copy by default, so undoing is usually deleting the copy. Only a
`--direct` session touches your project. Background: [the sandbox guide](../user/sandbox.md).

## Steps

1. Inside `hal code`, go back to before a message: click the message (or Ctrl+P, then `/rewind`)
   and choose Rewind files to before this. The files of the copy return to the checkpoint taken
   just before that message ran; the agent still remembers the later turns, so `/clear` starts a
   fresh conversation if you want it to forget them. The rest of this page works from the shell.

   ```sh
   /rewind                            # in hal code: lists the checkpoints (a picker in the full screen)
   ```

2. List the sessions that still hold changes.

   ```sh
   hal code ls                        # one line per isolated session
   ```

3. Look at what the agent did.

   ```sh
   hal code diff SID                  # the patch against the snapshot, with risk flags
   ```

4. Throw the changes away, or keep part of them.

   ```sh
   hal code reject SID                # removes the copy; your project was never changed
   hal code export SID out.patch      # a patch you can apply by hand with git apply
   ```

5. After a `--direct` session, find the checkpoints in your repository. A checkpoint is a git
   commit kept under `refs/hal/checkpoints/<session>/<n>`; checkpoint 0 is the state at start.
   [Unverified: a `hal` command that restores one is not built yet; check by running the first
   command below.]

   ```sh
   git for-each-ref refs/hal/checkpoints    # lists the checkpoints of every session
   git diff refs/hal/checkpoints/SID/0      # what changed since the session began
   ```

## When it fails

| Symptom | Fix |
| --- | --- |
| A message offers no Rewind | It ran in `hal chat`, or before checkpoints existed in this session; use `/rewind` to list them |
| `hal code ls` shows nothing | The session was accepted or rejected, or it ran with `--direct`. |
| `accept` refuses with a `high` flag | Read the flag, then `--allow-flags` if you trust the change, or `reject`. |
| No checkpoint refs | The session ran with checkpoints off, or the folder has no git; non-git folders keep a shadow repository under the state dir. |
