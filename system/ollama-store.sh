#!/usr/bin/env bash
# Optional one-time root setup: upgrade Ollama, keep its model store on another drive,
# and run it as the desktop user so it can read that drive. Mounts the drive at boot
# (an fstab entry by UUID), since /run/media is only populated after a desktop login.
#
#   sudo ./system/ollama-store.sh /run/media/$USER/BIGDRIVE/hal-models/ollama
set -euo pipefail

[ "$(id -u)" -eq 0 ] || { echo "Run with sudo." >&2; exit 1; }
STORE="${1:?usage: sudo $0 <model store folder on the drive>}"
USER_NAME="${SUDO_USER:?run through sudo from your own account}"
OLD_STORE="/usr/share/ollama/.ollama/models"

mkdir -p "$STORE"
MOUNT="$(findmnt -no TARGET --target "$STORE")"
DRIVE_UUID="$(findmnt -no UUID --target "$STORE")"
FSTYPE="$(findmnt -no FSTYPE --target "$STORE")"

echo "== Upgrading Ollama"
curl -fsSL https://ollama.com/install.sh | sh
/usr/local/bin/ollama --version

if [ "$MOUNT" != "/" ] && [ -n "$DRIVE_UUID" ]; then
  echo "== Mounting $MOUNT at boot"
  if ! grep -q "$DRIVE_UUID" /etc/fstab; then
    cp /etc/fstab "/etc/fstab.bak.$(date +%Y%m%d-%H%M%S)"
    printf 'UUID=%s %s %s defaults,nofail,x-systemd.device-timeout=10s 0 2\n' \
      "$DRIVE_UUID" "$MOUNT" "$FSTYPE" >> /etc/fstab
  fi
  systemctl daemon-reload
fi

echo "== Moving the model store"
systemctl stop ollama || true
if [ -d "$OLD_STORE" ]; then
  rsync -a "$OLD_STORE/" "$STORE/"
fi
chown -R "$USER_NAME:$USER_NAME" "$STORE"

echo "== Configuring the service"
mkdir -p /etc/systemd/system/ollama.service.d
cat > /etc/systemd/system/ollama.service.d/override.conf <<CONF
[Unit]
RequiresMountsFor=$MOUNT

[Service]
User=$USER_NAME
Group=$USER_NAME
Environment="OLLAMA_MODELS=$STORE"
Environment="OLLAMA_FLASH_ATTENTION=1"
Environment="OLLAMA_KV_CACHE_TYPE=q8_0"
Environment="OLLAMA_KEEP_ALIVE=5m"
CONF
systemctl daemon-reload
systemctl enable --now ollama
sleep 3
curl -fsS http://localhost:11434/api/version && echo
sudo -u "$USER_NAME" ollama list

echo
echo "Done. Once 'ollama list' shows your models, the old store can be removed:"
echo "  sudo rm -rf $OLD_STORE"
