#!/usr/bin/env bash
# One-time host setup for the Hal secure harness and the remote add-on (run with sudo).
#
# Installs the NVIDIA Container Toolkit and gVisor `runsc` only when missing, registers
# `runsc` with `--host-uds=open` (runsc refuses the bind-mounted broker and model sockets
# without it), and with --ssh writes a key-only sshd drop-in. Design notes:
#   - Docker is restarted only when no container runs, so running sandboxes survive.
#   - Nothing is piped into a shell: keys are piped into gpg, files are written by tee.
#   - userns-remap is not enabled: it would hide existing images.
#
#   sudo scripts/host-setup.sh --dry-run             print the steps, change nothing
#   sudo scripts/host-setup.sh                       toolkit and gVisor
#   sudo scripts/host-setup.sh --ssh                 also the key-only SSH server
#   sudo scripts/host-setup.sh --ssh --ssh-forwarding-limits
set -euo pipefail

DRY=0
SSH=0
LIMITS=0
FETCH_TIMEOUT_S=60 # a stalled download must not hang the script
DAEMON_JSON=/etc/docker/daemon.json
SSHD_DROPIN=/etc/ssh/sshd_config.d/10-hal-hardening.conf
KEYRINGS=/usr/share/keyrings

for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY=1 ;;
    --ssh) SSH=1 ;;
    --ssh-forwarding-limits) LIMITS=1 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done
if [ "$LIMITS" = 1 ] && [ "$SSH" = 0 ]; then
  echo "--ssh-forwarding-limits needs --ssh" >&2
  exit 2
fi

# Print a step, and run it unless this is a dry run.
run() { echo "+ $*"; [ "$DRY" = 1 ] || "$@"; }

if [ "$DRY" = 0 ] && [ "$(id -u)" != 0 ]; then
  echo "run with sudo (or use --dry-run)" >&2
  exit 1
fi
USER_NAME="${SUDO_USER:-${USER:-root}}"

echo "== NVIDIA Container Toolkit"
if command -v nvidia-ctk >/dev/null 2>&1; then
  echo "already installed"
else
  echo "+ fetch the NVIDIA key and apt list (signed-by keyring)"
  if [ "$DRY" = 0 ]; then
    curl -fsSL --max-time "$FETCH_TIMEOUT_S" https://nvidia.github.io/libnvidia-container/gpgkey \
      | gpg --batch --yes --dearmor -o "$KEYRINGS/nvidia-container-toolkit-keyring.gpg"
    curl -fsSL --max-time "$FETCH_TIMEOUT_S" \
      https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list \
      | sed "s#deb https://#deb [signed-by=$KEYRINGS/nvidia-container-toolkit-keyring.gpg] https://#g" \
      | tee /etc/apt/sources.list.d/nvidia-container-toolkit.list >/dev/null
  fi
  NEED_APT=1
fi

echo "== gVisor"
if command -v runsc >/dev/null 2>&1; then
  echo "already installed"
else
  echo "+ fetch the gVisor key and apt list (signed-by keyring)"
  if [ "$DRY" = 0 ]; then
    curl -fsSL --max-time "$FETCH_TIMEOUT_S" https://gvisor.dev/archive.key \
      | gpg --batch --yes --dearmor -o "$KEYRINGS/gvisor-archive-keyring.gpg"
    echo "deb [arch=$(dpkg --print-architecture) signed-by=$KEYRINGS/gvisor-archive-keyring.gpg] https://storage.googleapis.com/gvisor/releases release main" \
      | tee /etc/apt/sources.list.d/gvisor.list >/dev/null
  fi
  NEED_APT=1
fi
if [ "${NEED_APT:-0}" = 1 ]; then
  run apt-get update
  run apt-get install -y nvidia-container-toolkit runsc
fi

echo "== Docker runtimes"
echo "+ register runsc with runtimeArgs [\"--host-uds=open\"] in $DAEMON_JSON (backup kept)"
if [ "$DRY" = 0 ]; then
  [ -f "$DAEMON_JSON" ] && cp -n "$DAEMON_JSON" "$DAEMON_JSON.before-hal"
  python3 - "$DAEMON_JSON" <<'PY'
import json
import os
import sys

path = sys.argv[1]
data = json.load(open(path)) if os.path.exists(path) else {}
runtimes = data.setdefault("runtimes", {})
runsc = runtimes.setdefault("runsc", {"path": "/usr/bin/runsc"})
args = runsc.setdefault("runtimeArgs", [])
if "--host-uds=open" not in args:
    args.append("--host-uds=open")
with open(path, "w") as fh:
    json.dump(data, fh, indent=2)
    fh.write("\n")
PY
fi
run nvidia-ctk runtime configure --runtime=docker

if [ "$DRY" = 1 ]; then
  echo "+ restart Docker only when no container runs"
elif [ -n "$(docker ps -q)" ]; then
  echo "Containers are running; not restarting Docker. Restart it later with:"
  echo "  sudo systemctl restart docker"
else
  run systemctl restart docker
fi

if [ "$SSH" = 1 ]; then
  echo "== OpenSSH server (keys only; nobody can log in until a key is authorised)"
  run apt-get install -y openssh-server
  DROPIN="PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin no
AllowUsers $USER_NAME
X11Forwarding no
PermitUserEnvironment no"
  if [ "$LIMITS" = 1 ]; then
    DROPIN="$DROPIN
AllowTcpForwarding local
AllowStreamLocalForwarding no"
  fi
  echo "+ write $SSHD_DROPIN:"
  echo "$DROPIN" | sed 's/^/    /'
  if [ "$DRY" = 0 ]; then
    echo "$DROPIN" | tee "$SSHD_DROPIN" >/dev/null
  fi
  run sshd -t
  run systemctl reload ssh
fi

echo "== Check"
if [ "$DRY" = 1 ]; then
  echo "+ docker info --format '{{json .Runtimes}}'"
else
  docker info --format '{{json .Runtimes}}'
fi
