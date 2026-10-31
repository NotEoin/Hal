# Use Hal code in JetBrains IDEs

This page shows how to add Hal as a custom agent in the JetBrains AI Assistant, which speaks ACP.
Unverified against a real IDE: the protocol side is tested with a scripted client. Check by
running step 3.

## Steps

1. Build the agent image once.

   ```sh
   hal code --build   # creates hal-agent:local from this checkout
   ```

2. Write the agent configuration. JetBrains reads `~/.jetbrains/acp.json`; Hal writes only the
   path you name and keeps a `.bak` when you pass `--force`.

   ```sh
   hal ide setup jetbrains                              # print it first
   hal ide setup jetbrains --write ~/.jetbrains/acp.json   # refuses to overwrite without --force
   ```

3. Restart the IDE, open the AI Assistant, pick the custom agent "Hal" and ask for a small edit.
   The change appears as a diff in the chat.

4. Apply the result to the real repository when you are happy.

   ```sh
   hal code accept SID   # the session id is printed when the session ends
   ```

## When it does not work

| Symptom | Fix |
| --- | --- |
| "Hal" is not listed as an agent | Check the file path and that `hal` is on the PATH of the IDE (start the IDE from a shell) |
| The agent starts and exits at once | Run `hal acp` in a terminal and read the error; exit 3 is a lockdown refusal |
| No diff appears, only text | Update the IDE; older AI Assistant builds ignore ACP diff content |
| Edits do not reach the repository | By design: run `hal code accept SID` |
