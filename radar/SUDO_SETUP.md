# Two commands that need your password

Everything else installed itself. These two need root, so they are written out
rather than run. Both are about one thing: making sure `cron` comes back if it
ever stops, because every lane except the watchdog is a cron job.

## 1. Make systemd restart cron unconditionally

`cron.service` currently has `Restart=on-failure`, which covers a crash but not
a clean exit, a `systemctl stop`, or a cron that exits 0 after failing to parse
something. `Restart=always` with no start-limit covers all of it.

```sh
sudo mkdir -p /etc/systemd/system/cron.service.d
sudo tee /etc/systemd/system/cron.service.d/restart.conf >/dev/null <<'EOF'
[Unit]
# Never give up restarting. The default burst limit would leave cron dead
# after a few quick failures, which is exactly when it must come back.
StartLimitIntervalSec=0

[Service]
Restart=always
RestartSec=5
EOF
sudo systemctl daemon-reload
systemctl show cron.service -p Restart -p StartLimitIntervalSec
```

Expect `Restart=always` and `StartLimitIntervalSec=0`.

## 2. Let the watchdog restart cron itself

`watchdog.sh` runs from a systemd **user** timer, so it keeps working when cron
does not — but it cannot restart a system service without privileges. Today it
detects the problem, alerts your phone, and reports that it could not act.

This grants exactly one command, with no password, and nothing else:

```sh
sudo tee /etc/sudoers.d/radar-watchdog >/dev/null <<'EOF'
harman ALL=(root) NOPASSWD: /usr/bin/systemctl restart cron.service
EOF
sudo chmod 0440 /etc/sudoers.d/radar-watchdog
sudo visudo -c          # must print "parsed OK"
```

Then confirm the watchdog can use it:

```sh
sudo -n /usr/bin/systemctl restart cron.service && echo "watchdog can restart cron"
```

### Is that safe?

It permits one exact command — restarting cron — and no arguments can be
varied, because sudoers matches the full command line. It cannot stop cron,
edit a unit, or run anything else. The risk is that something able to run as
your user could restart cron in a loop; the gain is that a dead scheduler
recovers without you being at the keyboard.

If you would rather not grant it, skip this one. Step 1 already covers the
common case, and the watchdog will still alert you.

## What covers what

| failure | caught by | fixed by |
|---|---|---|
| a lane errors out | `cronwrap.sh` → phone | retry, or you |
| a lane never runs | `watchdog.sh` → phone | step 1, or step 2 |
| cron process dies | systemd | step 1 (today: only on failure) |
| cron stopped cleanly | `watchdog.sh` → phone | step 2 |
| crontab wiped | `watchdog.sh` → phone | you |
| whole machine down | nothing here can | an external ping service |
| web app dies | systemd | `Restart=always`, already installed |
