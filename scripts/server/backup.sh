#!/usr/bin/env bash
# Gece yedeği: DuckDB tek süreçli olduğu için web birkaç saniye durdurulur, dosya kopyalanır,
# web yeniden başlar. Son 14 yedek tutulur. (OVH'nin günlük disk yedeği bunun üstüne ikinci katman.)
set -euo pipefail
DATA=/var/lib/squadmind
STAMP=$(date +%Y-%m-%d)
systemctl stop squadmind-web
trap 'systemctl start squadmind-web' EXIT
cp "$DATA/squadmind.duckdb" "$DATA/backups/squadmind-$STAMP.duckdb"
gzip -f "$DATA/backups/squadmind-$STAMP.duckdb"
chown squadmind:squadmind "$DATA/backups/squadmind-$STAMP.duckdb.gz"
ls -1t "$DATA"/backups/squadmind-*.duckdb.gz | tail -n +15 | xargs -r rm --
