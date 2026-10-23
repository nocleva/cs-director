#!/usr/bin/env bash
# Gece yedeği: DuckDB tek süreçli olduğu için web birkaç saniye durdurulur, veritabanı (ve varsa
# son yazımları tutan .wal dosyası) tek arşive alınır, web yeniden başlar. Son 14 yedek tutulur.
# (OVH'nin günlük disk yedeği bunun üstüne ikinci katman.)
set -euo pipefail
DATA=/var/lib/squadmind
OUT="$DATA/backups/squadmind-$(date +%Y-%m-%d).tar.gz"
systemctl stop squadmind-web
trap 'systemctl start squadmind-web' EXIT
FILES=(squadmind.duckdb)
[ -f "$DATA/squadmind.duckdb.wal" ] && FILES+=(squadmind.duckdb.wal)
tar -C "$DATA" -czf "$OUT" "${FILES[@]}"
chown squadmind:squadmind "$OUT"
ls -1t "$DATA"/backups/squadmind-*.tar.gz | tail -n +15 | xargs -r rm --
