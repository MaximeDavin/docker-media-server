#!/usr/bin/env bash
# Collapse the split media/download trees into one /data root so the *arrs can
# hardlink instead of copy. Dry-run by default; pass --apply to execute.
#
# Assumes BOTH qBittorrent instances have been emptied of torrents+files first.
set -euo pipefail

APPLY=0; [[ ${1:-} == --apply ]] && APPLY=1
BRANCHES=(/mnt/disk2 /mnt/disk3)
COMPOSE=/home/max/docker-media-server
STAMP=$(date +%Y%m%d-%H%M%S)
BK="$HOME/.config/.migration-backup-$STAMP"
SONARR="$HOME/.config/sonarr/sonarr.db"
RADARR="$HOME/.config2/radarr/radarr.db"
WHIS="$HOME/.config/whisparr/whisparr3.db"

say()  { printf '%s\n' "$*"; }
run()  { if ((APPLY)); then eval "$@"; else printf '   [dry] %s\n' "$*"; fi; }

((APPLY)) || say ">>> DRY RUN - nothing will change. Re-run with --apply <<<"
say

# ---------------------------------------------------------------- preconditions
say "== 0. preconditions"
fail=0
for d in /mnt/disk2/downloads /mnt/disk3/downloads; do
  [[ -d $d ]] || continue
  n=$(find "$d" -type f 2>/dev/null | wc -l)
  sz=$(du -sh "$d" 2>/dev/null | cut -f1)
  printf '   %-28s %s files, %s\n' "$d" "$n" "$sz"
  (( n > 0 )) && fail=1
done
if (( fail )); then
  say
  say "   !! downloads still contain files. Empty both qBittorrent instances"
  say "      (remove torrents AND their files) before running with --apply."
  ((APPLY)) && exit 1
fi
for db in "$SONARR" "$RADARR" "$WHIS"; do
  [[ -f $db ]] || { say "   !! missing $db"; exit 1; }
done
say "   databases present"

# ---------------------------------------------------------------------- backup
say "== 1. backup"
run "mkdir -p '$BK'"
for db in "$SONARR" "$RADARR" "$WHIS"; do
  run "sqlite3 '$db' \".backup '$BK/$(basename "$(dirname "$db")").$(basename "$db")'\""
done
for c in qbittorrent qbittorrentwhisparr; do
  run "cp -a '$HOME/.config/$c/config/qBittorrent.conf' '$BK/$c.conf'"
done
say "   -> $BK"

# ------------------------------------------------------------ stop containers
say "== 2. stop affected containers"
SVCS="sonarr radarr bazarr whisparr qbittorrent qbittorrentwhisparr jellyfin movies calibre"
run "cd '$COMPOSE' && docker compose stop $SVCS"

# ------------------------------------------------------------------ move trees
say "== 3. move trees (same-filesystem rename, per branch)"
for b in "${BRANCHES[@]}"; do
  run "mkdir -p '$b/data/torrents' '$b/data/media' '$b/private/torrents' '$b/private/media'"
  # `private` is a SEPARATE root: the whisparr chain only. Keeping vids out of
  # `data` is what stops sonarr/radarr/bazarr/qbittorrent - all of which bind
  # the whole of `data` - from seeing it. Mount scoping is the only control
  # here; unix perms cannot help, since every container runs as uid 1000.
  for pair in "downloads/qbittorrent:data/torrents/qbittorrent" \
              "downloads/qbittorrentwhisparr:private/torrents/qbittorrentwhisparr" \
              "movies:data/media/movies" \
              "series:data/media/series" \
              "vids:private/media/vids"; do
    src="$b/${pair%%:*}"; dst="$b/${pair#*:}"
    [[ -d $src ]] || continue
    [[ -e $dst ]] && { say "   skip (exists): $dst"; continue; }
    run "mv '$src' '$dst'"
    ((APPLY)) || printf '   [dry] %s -> %s\n' "$src" "$dst"
  done
  run "rmdir '$b/downloads' 2>/dev/null || true"
done

# --------------------------------------------------------------- rewrite paths
say "== 4. rewrite stored paths in the *arr databases"
sq() { local db=$1; shift; if ((APPLY)); then sqlite3 "$db" "$*"; else printf '   [dry] %s: %s\n' "$(basename "$db")" "$*"; fi; }

sq "$SONARR" "update RootFolders set Path='/data/media/series/' where Path='/tv/';"
sq "$SONARR" "update Series set Path='/data/media/series/'||substr(Path,5) where Path like '/tv/%';"

sq "$RADARR" "update RootFolders set Path='/data/media/movies/' where Path='/movies/';"
sq "$RADARR" "update Movies set Path='/data/media/movies/'||substr(Path,9) where Path like '/movies/%';"
sq "$RADARR" "update Collections set RootFolderPath='/data/media/movies' where RootFolderPath='/movies';"
sq "$RADARR" "delete from RemotePathMappings where RemotePath=LocalPath;"

sq "$WHIS"   "update RootFolders set Path='/data/media/vids/' where Path='/data/';"
sq "$WHIS"   "update Movies set Path='/data/media/vids/scenes/'||substr(Path,14) where Path like '/data/scenes/%';"
sq "$WHIS"   "update Performers set RootFolderPath='/data/media/vids' where RootFolderPath='/data';"
sq "$WHIS"   "update Studios set RootFolderPath='/data/media/vids' where RootFolderPath='/data';"

# ------------------------------------------------------- rewrite qbt save paths
say "== 5. rewrite qBittorrent save paths"
run "sed -i 's|^Session\\\\DefaultSavePath=/downloads$|Session\\\\DefaultSavePath=/data/torrents/qbittorrent|; s|^Session\\\\TempPath=/downloads/temp$|Session\\\\TempPath=/data/torrents/qbittorrent/temp|' '$HOME/.config/qbittorrent/config/qBittorrent.conf'"
run "sed -i 's|^Session\\\\DefaultSavePath=/data$|Session\\\\DefaultSavePath=/data/torrents/qbittorrentwhisparr|' '$HOME/.config/qbittorrentwhisparr/config/qBittorrent.conf'"

say
say "== done =="
if ((APPLY)); then
  say "Backup: $BK"
  say "Next:   cd $COMPOSE && docker compose up -d"
else
  say "Re-run with --apply once both qBittorrent instances are emptied."
fi
