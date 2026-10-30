# Install gVisor for the box

`runsc` (gVisor) gives the box its own kernel surface. The `standard` lockdown level uses it when
it is installed, and `strict` requires it. Hal needs `--host-uds=open` so the box can reach the
two host sockets.

## Steps

1. Install `runsc` from your distribution or from the gVisor release page, and check it runs.

   ```sh
   runsc --version                    # prints the release
   ```

2. Register it with Docker, with the flag. Merge this into `/etc/docker/daemon.json`.

   ```json
   {
     "runtimes": {
       "runsc": { "path": "/usr/bin/runsc", "runtimeArgs": ["--host-uds=open"] }
     }
   }
   ```

3. Restart Docker and check the runtime is listed.

   ```sh
   sudo systemctl restart docker      # a restart stops running containers
   docker info --format '{{json .Runtimes}}'   # must list runsc
   ```

4. Prove a box starts under it (this is the check Hal itself runs).

   ```sh
   docker run --rm --runtime runsc --network none debian:trixie-slim true   # exit 0
   ```

## When it fails

| Symptom | Fix |
| --- | --- |
| `runsc` missing from `docker info` | The daemon was not restarted, or `daemon.json` is invalid JSON. |
| The box cannot connect to `/run/hal/model.sock` | `--host-uds=open` is missing from `runtimeArgs`. |
| `strict` refuses to start | Install runsc; `strict` never falls back to runc. |
| Slow first start | gVisor adds start-up time; compare with a plain `docker run` under runc. |
