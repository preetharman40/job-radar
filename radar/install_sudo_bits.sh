#!/usr/bin/env bash
# Run this once, as root:   sudo bash radar/install_sudo_bits.sh
#
# Everything else in this tool installed itself. These two changes need root,
# so they live here where you can read them before granting anything.
#
#   1. cron.service gets Restart=always with no start-limit, so systemd brings
#      cron back from any stop - not just a crash, which is all
#      Restart=on-failure covers today.
#   2. ONE command becomes passwordless for the radar user, so watchdog.sh can
#      restart cron itself instead of only reporting that it could not.
#
# Pass --no-sudoers to do step 1 only.
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo "run with sudo: sudo bash $0" >&2; exit 1; }
RADAR_USER="${SUDO_USER:-harman}"
do_sudoers=1
[ "${1:-}" = "--no-sudoers" ] && do_sudoers=0

echo "== 1. cron.service: Restart=always =="
mkdir -p /etc/systemd/system/cron.service.d
cat > /etc/systemd/system/cron.service.d/restart.conf <<'EOF'
# Installed by radar/install_sudo_bits.sh
[Unit]
# Without this, systemd's default start-limit gives up after a few rapid
# failures and leaves cron dead - which is precisely when it must come back.
StartLimitIntervalSec=0

[Service]
# on-failure (the default here) covers a crash but not a clean exit, a
# `systemctl stop`, or a cron that exits 0 after refusing to parse something.
Restart=always
RestartSec=5
EOF
systemctl daemon-reload
printf "   Restart=%s  StartLimitIntervalSec=%s\n" \
  "$(systemctl show cron.service -p Restart --value)" \
  "$(systemctl show cron.service -p StartLimitIntervalSec --value)"

if [ "$do_sudoers" -eq 1 ]; then
  echo "== 2. one passwordless command for $RADAR_USER =="
  # Written to a temp file and validated BEFORE being put in place: a malformed
  # file in /etc/sudoers.d can break sudo for every user on the machine, and
  # visudo -c on the real path after the fact is too late.
  tmp=$(mktemp)
  printf '%s ALL=(root) NOPASSWD: /usr/bin/systemctl restart cron.service\n' \
    "$RADAR_USER" > "$tmp"
  chmod 0440 "$tmp"
  if visudo -c -f "$tmp" >/dev/null 2>&1; then
    install -m 0440 -o root -g root "$tmp" /etc/sudoers.d/radar-watchdog
    rm -f "$tmp"
    visudo -c >/dev/null && echo "   /etc/sudoers.d/radar-watchdog installed, sudoers parses OK"
    su -s /bin/bash -c 'sudo -n /usr/bin/systemctl restart cron.service >/dev/null 2>&1' \
      "$RADAR_USER" && echo "   verified: $RADAR_USER can restart cron without a password" \
      || echo "   WARNING: the rule is installed but the test restart failed"
  else
    rm -f "$tmp"
    echo "   REFUSED: generated sudoers line did not validate. Nothing was changed." >&2
    exit 1
  fi
else
  echo "== 2. skipped (--no-sudoers) =="
fi
echo
echo "Done. Verify from your own shell:"
echo "  systemctl show cron.service -p Restart"
echo "  cd ~/devops/jobs/radar && ./watchdog.sh"
