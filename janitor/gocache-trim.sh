#!/bin/sh
# Keeps the Go build cache bounded: removes entries not used for N minutes, N shrinking as free disk shrinks.
# Go refreshes an entry's mtime on use (at most hourly), so >= 3 h unused is never in use by a running build.
CACHE="${GOCACHE:-$HOME/Library/Caches/go-build}"
STATE="$HOME/.local/state/desk/disk-status.json"
LOCK=/tmp/gocache-trim.lock
mkdir "$LOCK" 2>/dev/null || exit 0
trap 'rmdir "$LOCK"' EXIT
free_gib() { df -g / | awk 'NR==2 {print $4}'; }
before=$(free_gib)
if   [ "$before" -lt 40 ]; then age=180        # 3 h
elif [ "$before" -lt 80 ]; then age=360        # 6 h
else age=1440; fi                              # 24 h
if [ -d "$CACHE" ]; then
  find "$CACHE" -type f -mmin +"$age" -delete 2>/dev/null
  find "$CACHE" -type d -empty -mindepth 2 -delete 2>/dev/null
fi
after=$(free_gib)
level=ok
[ "$after" -lt 40 ] && level=low
[ "$after" -lt 20 ] && level=critical
mkdir -p "$(dirname "$STATE")"
printf '{"free_gib": %s, "level": "%s", "trim_age_min": %s, "at": "%s"}\n' "$after" "$level" "$age" "$(date -u +%FT%TZ)" > "$STATE"
echo "$(date '+%F %T') free ${before}->${after} GiB, removed entries unused > ${age} min, level $level" >> "$HOME/tools/orchestrator-desk/janitor/trim.log"
