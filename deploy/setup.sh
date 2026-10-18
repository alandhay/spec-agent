#!/bin/bash
# Install or update specdev on the instance (Ubuntu, run as root by deploy.py).
# Safe to run again: each step is skipped or repeated harmlessly.
# Expects the new code in /opt/specdev/app.new and the settings in /etc/specdev.env.
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive

if ! command -v fuse-sdl >/dev/null || ! command -v Xvfb >/dev/null || ! command -v unzip >/dev/null; then
    apt-get update -q
    apt-get install -y -q xvfb fuse-emulator-sdl spectrum-roms python3-venv build-essential git unzip curl
fi
test -f /usr/share/spectrum-roms/48.rom || { echo "the Spectrum ROMs are missing from /usr/share/spectrum-roms"; exit 1; }

# The assembler, built without its Lua scripting (which could run anything).
if ! command -v sjasmplus >/dev/null; then
    rm -rf /tmp/sjasmplus
    git clone -q --depth 1 https://github.com/z00m128/sjasmplus /tmp/sjasmplus
    make -C /tmp/sjasmplus -j2 USE_LUA=0 >/dev/null
    install -m 755 /tmp/sjasmplus/sjasmplus /usr/local/bin/sjasmplus
    rm -rf /tmp/sjasmplus
fi

command -v aws >/dev/null || snap install aws-cli --classic >/dev/null

id specdev >/dev/null 2>&1 || useradd --system --create-home --home-dir /var/lib/specdev --shell /usr/sbin/nologin specdev
install -d -o specdev -g specdev -m 750 /var/lib/specdev/data

# The code belongs to root: the service can read it but not change it.
rm -rf /opt/specdev/app.old
[ -d /opt/specdev/app ] && mv /opt/specdev/app /opt/specdev/app.old
mv /opt/specdev/app.new /opt/specdev/app
chmod -R u=rwX,go=rX /opt/specdev/app
[ -d /opt/specdev/venv ] || python3 -m venv /opt/specdev/venv
/opt/specdev/venv/bin/pip install -q --upgrade pip
/opt/specdev/venv/bin/pip install -q -e "/opt/specdev/app[hosted]"

# systemd reads the settings as root and hands them to the service; nothing else needs the file,
# and the confined processes, which may read /etc, must not see the keys in it.
chown root:root /etc/specdev.env
chmod 600 /etc/specdev.env

# Each person's agent, assembler and emulator run confined to their own folder (specdev/sandbox.py).
# The service refuses to start them without that, so check it works on this kernel.
runuser -u specdev -- /opt/specdev/venv/bin/python -I /opt/specdev/app/specdev/sandbox.py --check

cat >/etc/systemd/system/specdev.service <<'UNIT'
[Unit]
Description=specdev - ZX Spectrum coding agent
After=network-online.target
Wants=network-online.target

[Service]
User=specdev
WorkingDirectory=/opt/specdev/app
EnvironmentFile=/etc/specdev.env
Environment=HOME=/var/lib/specdev PYTHONUNBUFFERED=1
ExecStart=/opt/specdev/venv/bin/python -m specdev --host 0.0.0.0
Restart=always
RestartSec=3
NoNewPrivileges=yes
ProtectSystem=strict
ReadWritePaths=/var/lib/specdev
PrivateTmp=yes

[Install]
WantedBy=multi-user.target
UNIT

# Every five minutes: once nothing has used the service for SPECDEV_STOP_MINUTES, copy everyone's
# programs to the bucket and switch the instance off. The page's /wake starts it again.
cat >/usr/local/bin/specdev-idle-stop <<'STOP'
#!/bin/bash
. /etc/specdev.env
[ "$(cut -d. -f1 /proc/uptime)" -gt 600 ] || exit 0
[ -n "$(find "$SPECDEV_ACTIVITY" -mmin "+$SPECDEV_STOP_MINUTES" 2>/dev/null)" ] || exit 0
/snap/bin/aws s3 sync /var/lib/specdev/data "s3://$SPECDEV_BUCKET/data" --only-show-errors
logger -t specdev "unused for $SPECDEV_STOP_MINUTES minutes: switching off"
systemctl poweroff
STOP
chmod 755 /usr/local/bin/specdev-idle-stop
rm -f /etc/cron.d/specdev-backup
cat >/etc/cron.d/specdev-idle-stop <<'CRON'
*/5 * * * * root /usr/local/bin/specdev-idle-stop
CRON

systemctl daemon-reload
systemctl enable -q specdev
systemctl restart specdev
sleep 3
systemctl is-active specdev
curl -fsS http://127.0.0.1:8766/api/config
echo
