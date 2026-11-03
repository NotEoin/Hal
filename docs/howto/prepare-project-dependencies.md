# Prepare project dependencies

The box has no network, so a project's dependencies must be installed first. `hal box deps`
does that in a prep box whose only way out is a proxy limited to package registries. It needs
lockdown `relaxed` or lower and the docker backend.

## Steps

1. Check that the project has a lockfile (uv, npm or cargo). A project without one is not offered.

   ```sh
   ls uv.lock package-lock.json Cargo.lock   # at least one must exist
   ```

2. Run the prep. It asks once per lockfile; no terminal means no approval.

   ```sh
   hal box deps --dir .               # asks: Install dependencies from uv.lock (uv) in the prep box? [y/N]
   ```

3. Start the agent. The next `hal code` or `hal box` sees the installed dependencies; the prep
   is offered again only when a lockfile changes.

   ```sh
   hal code .                         # the box has the dependencies, still no network
   ```

## When it fails

| Symptom | Fix |
| --- | --- |
| `dependency prep needs lockdown relaxed or lower` | Set `[sandbox].lockdown = "relaxed"` on the host. |
| Nothing is installed and no error | There was no terminal, so the approval was denied; run it in a terminal. |
| A registry host is denied | The proxy allows only the package registries; the denied host is printed. |
| npm or cargo fails | Only uv is verified end to end; npm and cargo are unverified. |
