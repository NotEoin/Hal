# Run an agent CLI in the box

`hal box` runs claude, qwen, opencode, codex, crush or goose with no network, on an isolated
copy of your project. Background: [the sandbox guide](../user/sandbox.md).

## Steps

1. Build the base image and the image of the CLI you want. This is the only step that uses the
   network (apt, pip, npm or the goose release, all pinned).

   ```sh
   hal box image build                # the base image, about 870 MB
   hal box image build crush          # one CLI image on top of it
   hal box harnesses              # which CLIs are built, with their pinned versions
   ```

2. Run the CLI in the project folder. Everything after `--` reaches the CLI unchanged.

   ```sh
   hal box -- crush --version         # a quick check that the box starts
   hal box --dir ~/src/app -- crush   # the interactive CLI on a copy of ~/src/app
   ```

3. Keep the CLI's own prompts with `--ask`; without it the CLI auto-approves work inside the box.
   Calls that leave the box still ask on your host (`hal approvals`).

   ```sh
   hal box --ask -- qwen              # the CLI asks before each edit or command
   ```

4. Take the changes out, or throw them away. The exit line names the session.

   ```sh
   hal code accept SID                # shows the diff and risk flags, then applies it
   hal code reject SID                # deletes the copy
   ```

## When it fails

| Symptom | Fix |
| --- | --- |
| `no image for crush; run hal box image build crush` | Build the base image first, then the CLI image (step 1). |
| `hal box runs on docker only` | Drop `--backend`, or use `docker`. |
| `image ... not found; run hal box image build` | The base image was removed; build it again. |
| `cannot start crush: No such file` inside the box | The CLI image is older than the base; rebuild it with `--no-cache`. |
| `--direct` refused | Set `[sandbox].lockdown = "relaxed"` on the host; a tool call cannot raise it. |
