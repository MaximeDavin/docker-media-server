#!/usr/bin/env bash
# Nightly unattended update: snapshot, pull repo, pull images, recreate.
# Run by tools/systemd/media-update.timer; safe to run by hand.
#
#   1. restic snapshot of ~/.config and .env (containers paused for it)
#   2. git pull --ff-only    - picks up Renovate's merged version bumps
#   3. docker compose pull   - picks up new digests for latest/nightly/v3...
#   4. docker compose up -d  - recreates only what changed
#   5. prune dangling images, ntfy the list of recreated containers
#
# A failed snapshot or git pull aborts before anything is updated: no update
# without a way back, and no deploying on top of a diverged checkout.
#
# Restore one service: docker compose stop <svc>, then as root
#   restic -r $RESTIC_REPOSITORY restore latest --include ~/.config/<svc> --target /
# Root because the tree holds files owned by container uids.
set -euo pipefail

REPO=/home/max/docker-media-server
export RESTIC_REPOSITORY=/mnt/storage/backups/restic
export RESTIC_PASSWORD_FILE=$HOME/.config/restic/password

# Pihole stays running: pausing it takes LAN DNS down for the whole snapshot.
NO_PAUSE=(pihole)

cd "$REPO"
exec 9>"${XDG_RUNTIME_DIR:-/tmp}/media-update.lock"
flock -n 9 || { echo "another update is running"; exit 0; }

# NTFY_TOKEN and DOMAIN, without sourcing the whole .env into this shell.
envval() { sed -n "s/^$1=//p" .env | tail -1 | tr -d '"'\'; }
NTFY_URL="https://ntfy.$(envval DOMAIN)"
NTFY_TOKEN=$(envval NTFY_TOKEN)

# Retries cover traefik/ntfy themselves having just been recreated.
notify() { # topic priority title message
  curl -fsS --retry 6 --retry-delay 10 --retry-all-errors -o /dev/null \
    -H "Authorization: Bearer $NTFY_TOKEN" -H "Title: $3" \
    -H "Priority: $2" -H "Tags: whale" -d "$4" "$NTFY_URL/$1" \
    || echo "ntfy publish failed"
}

step=start
paused=()
unpause() {
  if ((${#paused[@]})); then docker unpause "${paused[@]}" >/dev/null; fi
  paused=()
}
on_exit() {
  rc=$?
  unpause || true
  ((rc)) && notify homelab-alerts 4 "Media update failed" \
    "Step '$step' failed (exit $rc). journalctl -u media-update for details."
  exit $rc
}
trap on_exit EXIT

# ------------------------------------------------------------------ snapshot
step=snapshot
for c in $(docker compose ps --status running --format '{{.Name}}'); do
  [[ " ${NO_PAUSE[*]} " == *" $c "* ]] || paused+=("$c")
done
# Paused, not stopped: no writes land mid-copy (no torn sqlite files), and
# nothing restarts - unpause is instant.
docker pause "${paused[@]}" >/dev/null
restic backup --one-file-system --exclude-caches --tag nightly \
  --exclude "$HOME/.config/**/cache" \
  --exclude "$HOME/.config/**/transcodes" \
  --exclude "$HOME/.config/**/logs" \
  --exclude "$HOME/.config/**/log" \
  --exclude "$HOME/.config/Code" \
  "$HOME/.config" "$REPO/.env"
unpause
restic forget --prune --tag nightly --keep-daily 7 --keep-weekly 4

# ---------------------------------------------------------------- git pull
step="git pull"
git pull --ff-only

# ------------------------------------------------------------ pull + recreate
step="compose pull"
before=$(docker compose ps -a --format '{{.Name}} {{.ID}}' | sort)
# One unreachable registry should not block every other update.
docker compose pull --quiet --ignore-pull-failures

step="compose up"
docker compose up -d --quiet-pull
after=$(docker compose ps -a --format '{{.Name}} {{.ID}}' | sort)

step=prune
docker image prune -f >/dev/null

# Recreated containers get a new ID; that covers new images and changed config.
changed=$(comm -13 <(echo "$before") <(echo "$after") | cut -d' ' -f1)
if [[ -n $changed ]]; then
  echo "recreated: $(echo $changed)"
  notify homelab-updates 2 "Media server updated" "Recreated: $(echo $changed | sed 's/ /, /g')"
else
  echo "nothing to update"
fi
