# Run on Windows with WSL2

This page shows how to run Hal on a Windows 11 PC through WSL2 and which parts of Hal behave
differently there. Native Windows is not supported: Hal needs Linux processes, Unix sockets,
docker and systemd user units. WSL2 runs a real Linux kernel, so it is the supported route.

Support is new and has not been run on a Windows PC yet. The table after the steps marks every
feature as verified or unverified, and `hal doctor` prints WSL2 lines that check the parts it can.
To pair a Windows laptop with a Hal on another machine instead, see
[use-hal-from-a-windows-laptop.md](use-hal-from-a-windows-laptop.md).

## Steps

1. Enable WSL and install Ubuntu. In an administrator PowerShell on Windows:

   ```powershell
   wsl --install -d Ubuntu-24.04   # installs WSL2 and the distro; reboot when asked
   wsl --status                    # Default Version: 2
   ```

2. Enable systemd inside the distro. Hal's background services are systemd user units. Inside Ubuntu:

   ```bash
   printf '[boot]\nsystemd=true\n' | sudo tee -a /etc/wsl.conf   # appends; edit by hand if [boot] exists
   ```

   Restart the distro from Windows, then check from Ubuntu:

   ```powershell
   wsl --shutdown                  # run in PowerShell; stops every WSL distro
   ```

   ```bash
   systemctl --user is-system-running   # running or degraded, not "offline"
   ```

3. Install the NVIDIA driver on Windows, not in Ubuntu. The Windows driver exposes CUDA inside
   WSL through `/usr/lib/wsl/lib`. Skip this step if you have no GPU: Hal runs on the CPU.

   ```bash
   ls /usr/lib/wsl/lib/libcuda.so.1   # the driver's CUDA library, provided by Windows
   nvidia-smi                         # lists your GPU; if not, update the Windows driver
   ```

4. Install Docker Engine inside Ubuntu, or turn on Docker Desktop's WSL integration for this
   distro. The agent box needs a docker daemon.

   ```bash
   docker info --format '{{.ServerVersion}}'   # prints a version if the daemon answers
   ```

5. Install Hal inside Ubuntu, in the Linux file system (`~`), not under `/mnt/c`. Follow
   [SETUP.md](../../SETUP.md), then run the doctor.

   ```bash
   hal doctor   # the platform, wsl systemd and wsl gpu lines apply only on WSL2
   ```

6. Run your model server. Ollama inside Ubuntu works the same as on Linux. If you run Ollama or
   LM Studio on the Windows side instead, Hal accepts only local URLs by default, so reach it
   through WSL's localhost forwarding and check the address first:

   ```bash
   curl -s http://127.0.0.1:11434/api/tags | head -c 200   # JSON means Hal can use it
   hal models add-source ollama http://127.0.0.1:11434     # then: hal tune --auto --write
   ```

7. Decide on the box runtime. The default `standard` lockdown uses gVisor (`runsc`) when docker
   has it and falls back to `runc` with a warning when it does not. `strict` refuses to start
   without `runsc`. gVisor under WSL2 is unverified; try [install-gvisor.md](install-gvisor.md)
   and check:

   ```bash
   docker info --format '{{json .Runtimes}}'   # lists runsc if it is registered
   hal doctor --only backend --only runtime    # warns when the box runs on runc
   ```

## What works under WSL2

"Unverified" means the code is expected to work or to fall back, but nobody has run it on WSL2.
Run the check in the last column and report the result.

| Feature | Status | How to check |
| --- | --- | --- |
| WSL2 detection (`/proc/sys/kernel/osrelease` names Microsoft) | Verified with a fake `osrelease` in the tests | `hal tune --auto`: the hardware lines end with `WSL2` |
| `hal doctor` WSL2 lines | Verified with fakes in `test_doctor.py`; unverified on a real distro | `hal doctor --only platform --only wsl` |
| Hardware figures for tuning | Unverified: VRAM and RAM are what Windows lends the distro | `hal tune --auto --json`, then compare with `nvidia-smi` and `free -m` |
| systemd user units (`hal on`, `hal setup --enable`) | Unverified; needs `systemd=true` | `systemctl --user is-system-running` |
| Docker box with `runc` | Unverified | `hal box image build`, then a short `hal code` session |
| Docker box with `runsc` (gVisor) | Unverified; may not run on the WSL2 kernel | `hal doctor --only backend --only runtime` |
| `bwrap` backend | Unverified; needs user namespaces and only the `relaxed` lockdown allows it | set `[sandbox].lockdown = "relaxed"` in `config.toml`, then `hal doctor` |
| GPU in the model server | Unverified; needs the Windows driver | `nvidia-smi` inside Ubuntu, then `hal tune --auto` |
| Hermetic test suite | Unverified on WSL2 | `uv run pytest -q` |

## Symptoms and fixes

| Symptom | Fix |
| --- | --- |
| `hal doctor` shows `wsl systemd` as warn | Do step 2, then `wsl --shutdown` in PowerShell. |
| `hal doctor` shows `wsl gpu` as warn | Install or update the NVIDIA driver on Windows. Do not install a Linux driver in the distro. |
| `nvidia-smi` runs but lists no GPU | Run `wsl --update` in PowerShell, check the distro is WSL2 (`wsl -l -v`), then update the Windows driver. |
| `hal doctor` says docker is not on the PATH | Do step 4. With Docker Desktop, enable the integration for this distro in its settings. |
| `hal doctor` warns that the box runs on runc | `runsc` is not registered. Install it ([install-gvisor.md](install-gvisor.md)) or accept `runc` at `standard` lockdown. |
| `hal code` refuses to start at `strict` lockdown | `strict` needs `runsc`. Register it, or set `[sandbox].lockdown = "standard"` in `config.toml` (`hal lockdown` only explains the level). |
| `systemctl --user` says `Failed to connect to bus` | Log in again, or run `loginctl enable-linger "$USER"` and `wsl --shutdown`. |
| Hal is slow to read project files | The project is under `/mnt/c`. Move it into the Linux file system. |
| The model server on Windows is not reachable | Use `http://127.0.0.1:PORT` first. A non-local address needs `allow_remote = true` and https (see [../user/models.md](../user/models.md)). |
