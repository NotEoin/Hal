# Use Hal from a Linux laptop

This page shows how to pair a Linux laptop with the desktop that runs Hal, keep an SSH tunnel to
the desktop's model gateway, and use the desktop's models from the laptop. The tunnel carries
model calls only; the sandbox runs on the laptop. Design: [07-remote](../design/07-remote.md).

You need OpenSSH on both machines, Hal installed on both, and `hal-remote` at
`~/.local/bin/hal-remote` on the desktop.

## Steps

1. On the desktop, check sshd and the Hal lines. Fix every `critical` row before you go on.

   ```sh
   hal remote server-check   # read-only; add --json for scripts
   ```

2. On the laptop, pair. This works when your own SSH key already logs in to the desktop. It makes
   two new keys, asks the desktop to authorize them, pins the desktop's host key and stores a
   gateway token that is delivered once.

   ```sh
   hal remote setup me@desk.lan   # profile "desktop"; add --client NAME to name the laptop
   ```

3. If password login is off and the laptop has no key on the desktop, use the manual flow. Run
   the printed command on the desktop, compare the fingerprint, then press Enter on the laptop.

   ```sh
   hal remote setup me@desk.lan --manual --from 192.168.1.0/24   # prints one command for the desktop
   ```

4. Start the tunnel and check it. `status` exits 0 only when the gateway answers.

   ```sh
   hal remote up              # systemd user unit when usable, else a detached process
   hal remote status          # for example: desktop  up since 18:01Z (systemd)  gateway ok
   ```

5. Start at login if you want that.

   ```sh
   hal remote up --enable     # needs a working systemd user manager
   ```

6. Use Hal as usual. `setup` added `[gateway]` client-mode keys to your `config.toml`, so
   `hal code` sends model calls through the tunnel with the client token. It also added the
   desktop's tools as the MCP server `hal-desktop`, which asks before each call.

7. Stop it, or pair again with new keys.

   ```sh
   hal remote down            # add --disable to stop the login start
   hal remote setup me@desk.lan --rotate   # new keys; revoke the old client on the desktop first
   ```

| Symptom | Fix |
| --- | --- |
| `authorize on ... failed` | Your own SSH key does not work in batch mode. Load it into the agent, or use `--manual` |
| `host key mismatch` | The desktop changed or someone is in the way. Compare the fingerprint on the desktop, then use `--rotate` |
| `no token was delivered` | The token is delivered once. On the desktop run `hal remote authorize --client NAME --rotate-token`, then `setup --rotate` |
| `status` says `failed` with a fix line | Run the command on the fix line; permanent errors are not retried |
| `status` shows `backoff` | The desktop is asleep or off the network. The supervisor retries by itself |
| Gateway answers 401 | The token file is stale. Rotate the token as above |
| `profile exists` | Pass `--rotate` to pair again |
