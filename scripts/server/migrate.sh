#!/usr/bin/env bash
# Bir kez: veriyi ve gizli ayarları bu bilgisayardan sunucuya taşır (Git Bash'ten).
# Kullanım: scripts/server/migrate.sh <sunucu-ip>
# Önce: sunucuda setup.sh çalışmış olmalı, deploy.sh kodu göndermiş olmalı.
# Taşınan: DuckDB, FACEIT önbelleği, .env (yollar sunucuya göre), Cloudflare tünel kimliği,
# awpy harita verisi (~/.awpy). Demo dosyaları (processed/) taşınmaz: veriler DB'de, dosyalar
# yeniden işleme için bu bilgisayarda kalır.
# Bu bilgisayarda web ve tünel durdurulur (aynı tünel iki yerde çalışmasın, DB kilidi açılsın).
set -euo pipefail
HOST=${1:?"kullanım: migrate.sh <sunucu-ip>"}
KEY="$HOME/.ssh/squadmind_vps"
SSHO=(-i "$KEY" -o StrictHostKeyChecking=accept-new)
cd "$(git rev-parse --show-toplevel)"
DATA=$(grep -E '^SQUADMIND_DATA_DIR=' .env | cut -d= -f2- | tr -d '\r')
TUNNEL_ID=$(grep -E '^tunnel:' ~/.cloudflared/config.yml | awk '{print $2}' | tr -d '\r')

echo "== yerel web ve tüneli durdur"
powershell.exe -NoProfile -Command "
  Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue | ForEach-Object { Stop-Process -Id \$_.OwningProcess -Force }
  Get-CimInstance Win32_Process -Filter \"Name = 'cloudflared.exe'\" | Where-Object { \$_.CommandLine -like '*squadmind*' } | ForEach-Object { Stop-Process -Id \$_.ProcessId -Force }
  Disable-ScheduledTask -TaskName SquadMind -ErrorAction SilentlyContinue | Out-Null"
sleep 2

echo "== .env (yollar sunucuya göre; DOWNLOADS_DIR sunucuda yok)"
sed -e 's|^SQUADMIND_DATA_DIR=.*|SQUADMIND_DATA_DIR=/var/lib/squadmind|' -e '/^DOWNLOADS_DIR=/d' -e 's/\r$//' .env > /tmp/squadmind.env

echo "== veri, önbellek, .env"
# DuckDB son yazımları .wal dosyasında tutabilir: ikisi birlikte taşınır
tar -C "$DATA" -c squadmind.duckdb $( [ -f "$DATA/squadmind.duckdb.wal" ] && echo squadmind.duckdb.wal ) cache | ssh "${SSHO[@]}" "squadmind@$HOST" 'tar -x -C /var/lib/squadmind'
scp "${SSHO[@]}" -q /tmp/squadmind.env "squadmind@$HOST:/opt/squadmind/.env"
rm -f /tmp/squadmind.env
ssh "${SSHO[@]}" "squadmind@$HOST" 'chmod 600 /opt/squadmind/.env'

echo "== awpy harita verisi"
tar -C "$HOME" -c .awpy | ssh "${SSHO[@]}" "squadmind@$HOST" 'tar -x -C /home/squadmind'

echo "== tünel kimliği ve ayarı (root)"
scp "${SSHO[@]}" -q "$HOME/.cloudflared/$TUNNEL_ID.json" "ubuntu@$HOST:/tmp/$TUNNEL_ID.json"
ssh "${SSHO[@]}" "ubuntu@$HOST" "sudo install -m 600 /tmp/$TUNNEL_ID.json /etc/cloudflared/$TUNNEL_ID.json && rm /tmp/$TUNNEL_ID.json && sudo tee /etc/cloudflared/config.yml >/dev/null <<EOF
tunnel: $TUNNEL_ID
credentials-file: /etc/cloudflared/$TUNNEL_ID.json
ingress:
  - hostname: app.squadmind.info
    service: http://127.0.0.1:8000
  - service: http_status:404
EOF
sudo systemctl restart squadmind-web squadmind-tunnel; sleep 6; sudo systemctl --no-pager --lines=0 status squadmind-web squadmind-tunnel | grep -E 'Active|●'"

echo "== canlı kontrol"
curl -s -o /dev/null -w "app.squadmind.info %{http_code}\n" https://app.squadmind.info/
echo "Bitti. Bu bilgisayardaki otomatik başlatma (Görev Zamanlayıcı 'SquadMind') kapatıldı."
