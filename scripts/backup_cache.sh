#!/usr/bin/env bash
# Backup van Fly cache-volume → lokaal naar ~/Backups/buurtscan/
#
# Wat zit er in cache?
# - cache/analytics.jsonl       — alle event-tracking sinds analytics-launch
# - cache/ep_online.sqlite      — RVO energielabel SQLite (1 GB, regenereerbaar)
# - cache/onderwijs.json        — DUO + LRK data (regenereerbaar via sync)
# - cache/woz/*.json            — WOZ-cache per BAG-VBO (1 jaar TTL)
# - cache/voorzieningen/*.json  — OSM Overpass cache
#
# DE BELANGRIJKSTE: analytics.jsonl. De rest is regenereerbaar via sync-scripts.
#
# Usage:
#   ./scripts/backup_cache.sh                  # alle files (~1 GB)
#   ./scripts/backup_cache.sh --analytics      # alleen analytics (KB)
#   ./scripts/backup_cache.sh --sample         # snelle test, eerste 100 files
#
# Cron-tip: zet in macOS launchd of cron:
#   0 4 * * * /pad/naar/scripts/backup_cache.sh --analytics

set -euo pipefail

APP="${FLY_APP:-buurtscan}"
BACKUP_DIR="${HOME}/Backups/buurtscan"
DATE=$(date +%Y-%m-%d_%H%M)
TARGET="${BACKUP_DIR}/${DATE}"

mkdir -p "${TARGET}"

MODE="${1:-full}"

case "${MODE}" in
  --analytics)
    echo "[backup] Alleen analytics.jsonl van app=${APP}"
    fly ssh sftp shell --app "${APP}" <<EOF
get /app/apps/api/cache/analytics.jsonl ${TARGET}/analytics.jsonl
EOF
    ;;
  --sample)
    echo "[backup] Sample van metadata + analytics"
    fly ssh sftp shell --app "${APP}" <<EOF
get /app/apps/api/cache/analytics.jsonl ${TARGET}/analytics.jsonl
EOF
    ;;
  full|--full)
    echo "[backup] Volledige cache van app=${APP} → ${TARGET}"
    echo "  (kan enkele minuten duren bij grote SQLite-cache)"
    # Tar inside the VM, then SCP — dit is sneller dan per file
    fly ssh console --app "${APP}" -C "tar czf /tmp/cache-backup.tar.gz -C /app/apps/api cache" 2>&1 | tail -3
    fly ssh sftp shell --app "${APP}" <<EOF
get /tmp/cache-backup.tar.gz ${TARGET}/cache-backup.tar.gz
EOF
    fly ssh console --app "${APP}" -C "rm /tmp/cache-backup.tar.gz" 2>&1 | tail -1
    ;;
  *)
    echo "Onbekende modus: ${MODE}"
    echo "Gebruik: --analytics | --sample | --full (default)"
    exit 1
    ;;
esac

echo
echo "[backup] Klaar — opgeslagen in:"
ls -lah "${TARGET}/"
echo
echo "[backup] Houd alleen laatste 30 backups aan…"
cd "${BACKUP_DIR}"
ls -1t | tail -n +31 | xargs -I{} rm -rf {} 2>/dev/null || true
echo "[backup] Totaal backups bewaard: $(ls -1 | wc -l | tr -d ' ')"
