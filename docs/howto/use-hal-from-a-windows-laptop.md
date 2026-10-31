# Use Hal from a Windows laptop

This page shows how to pair a Windows laptop with the desktop that runs Hal and keep the SSH
tunnel up. Windows is client-only (Decision D2): the sandbox needs Linux, so `hal code` there
means WSL2 or an SSH session on the desktop. The tunnel serves tools that call the desktop's
models from Windows. Design: [07-remote](../design/07-remote.md).

Parts marked unverified were not run on a Windows machine; check them against the manual
checks M3 and M4 in [the developer notes](../dev/remote.md).

## Steps

1. Check that the OpenSSH client is installed (a built-in optional feature of Windows 10 1809
   and later). If `ssh.exe` is missing, add it under Settings, Apps, Optional features.

   ```powershell
   ssh -V   # prints the OpenSSH version
   ```

2. Install Hal's remote client from the release tag with `uv`.

   ```powershell
   uv tool install --from "git+<repository URL>@<release tag>#subdirectory=packages/hal-remote" hal-remote   # unverified
   ```

3. Pair. Your own key must already log in to the desktop; otherwise add `--manual`.

   ```powershell
   hal-remote setup me@desk.lan   # makes keys with ssh-keygen.exe, never in %USERPROFILE%\.ssh
   ```

4. Start the tunnel. It runs without a console window.

   ```powershell
   hal-remote up              # starts hal-remote-tunnelw.exe detached
   hal-remote status          # exits 0 only when the gateway answers
   ```

5. Start at logon. This creates the task `Hal\remote-desktop`, or a `.cmd` in the Startup folder
   when the task cannot be created.

   ```powershell
   hal-remote up --install-task   # unverified: whether schtasks needs an administrator (M4)
   ```

6. Stop it.

   ```powershell
   hal-remote down --disable   # stops the process tree and removes the task and Startup file
   ```

| Symptom | Fix |
| --- | --- |
| `no OpenSSH client found` | Install the OpenSSH Client optional feature, or set `[remote].ssh` |
| `Bad permissions` on a key | The key's ACL is too open. Run `setup --rotate` so `ssh-keygen.exe` makes it again |
| `status` says `not running` after logon | Open Task Scheduler, check `Hal\remote-desktop`, or look for the Startup `.cmd` |
| `up` says already running | `hal-remote down`, then `up` |
| Tunnel stops with a port error | Another program uses local port 8741. Stop it, then `up` |
