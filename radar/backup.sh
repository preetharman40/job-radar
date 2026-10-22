#!/usr/bin/env bash
# Daily snapshot of the files that cannot be regenerated.
#
# applications.md holds your statuses, contacts and hand-written notes on each
# role. It is gitignored - deliberately, it has hiring-manager details in it -
# which means git is NOT protecting it. A bad edit or a dead disk loses it.
# seen.sqlite3 holds dismissals and liveness state that would take a full
# re-seed to rebuild.
#
# Keeps 30 daily copies, skips writing when nothing changed.
set -euo pipefail
cd "$(dirname "$0")/.."
DEST="backups"
STAMP="$(date +%F)"
mkdir -p "$DEST"

for f in applications.md radar/seen.sqlite3 radar/targets.json; do
  [ -f "$f" ] || continue
  name="$(basename "$f")"
  latest="$(ls -1t "$DEST"/"$name".*.bak 2>/dev/null | head -1 || true)"
  # Don't churn identical copies.
  if [ -n "$latest" ] && cmp -s "$f" "$latest"; then continue; fi
  cp -p "$f" "$DEST/$name.$STAMP.bak"
done

# Keep 30 of each. seen.sqlite3 is the big one (11MB and rising - it grew
# 4.7 -> 11MB the day 96 boards were added), so 30 copies is roughly 330MB.
# That was worth trimming to 7 when the root filesystem was 97% full; after the
# disk was grown from 98G to 246G it is noise against 145G free, and a longer
# tail buys more room to recover from a bad day.
keep() { ls -1t "$DEST"/"$1".*.bak 2>/dev/null | tail -n +"$2" | xargs -r rm -f; }
keep applications.md 31
keep targets.json     31
keep seen.sqlite3     31

printf '%s  %s files, %s\n' "$(date '+%F %H:%M')" \
  "$(ls -1 "$DEST" | wc -l)" "$(du -sh "$DEST" | cut -f1)"
