# Use Hal away from home

This page shows how to reach the desktop's Hal from outside your home network with SSH over
Tailscale. Hal does not expose the gateway to the internet and does not use Tailscale Serve or
Funnel (Decision D17); only SSH crosses the network, with the same restricted keys as at home.

## Steps

1. Install Tailscale on the desktop and the laptop and sign in to the same tailnet. Tailscale is
   not installed on the desktop today; this is a documented alternative, not a default.

2. Restrict the tailnet ACL so only your laptop reaches port 22 on the desktop.

3. Pair with the desktop's tailnet name. The `from=` pattern then names the tailnet address, so
   pass it explicitly.

   ```sh
   hal remote setup me@desk.example-tailnet.ts.net --from 100.64.0.0/10   # your tailnet range
   ```

4. Start the tunnel and check it.

   ```sh
   hal remote up
   hal remote status   # gateway ok means the models answer
   ```

5. Keep the desktop awake. A sleeping desktop shows as `backoff` and the tunnel returns by itself
   when it wakes.

| Symptom | Fix |
| --- | --- |
| `authorize` refused from the new address | The key's `from=` does not match. On the desktop run `hal remote authorize --client NAME --from PATTERN` |
| Login works but the key is refused | Do not use Tailscale SSH for the mcp key: it ignores `authorized_keys` options. Use the normal sshd |
| `status` stays in `backoff` | Check `tailscale status` on both machines |
