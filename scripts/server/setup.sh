#!/usr/bin/env bash
# SquadMind sunucu hazırlığı (Ubuntu 24.04, bir kez, root olarak).
# Yapar: güncellemeler + otomatik güvenlik güncellemesi, `squadmind` kullanıcısı, SSH sertleştirme
# (yalnız anahtar), güvenlik duvarı (yalnız SSH; site Cloudflare tüneliyle çıkış bağlantısından
# gelir, dışarıya port açılmaz), uv + Python, cloudflared, systemd servisleri ve gece yedeği.
# Kod, veri, .env ve tünel kimliği ayrı adımda `deploy.ps1` / `migrate.ps1` ile gelir.
set -euo pipefail

APP_USER=squadmind
APP_DIR=/opt/squadmind          # kod
DATA_DIR=/var/lib/squadmind     # DuckDB, önbellek, günlükler, yedekler

# --- sistem -------------------------------------------------------------------------------
export DEBIAN_FRONTEND=noninteractive
apt-get update -q
apt-get -yq upgrade
apt-get -yq install unattended-upgrades ufw curl git ca-certificates fail2ban
dpkg-reconfigure -f noninteractive unattended-upgrades

# --- kullanıcı ve klasörler ---------------------------------------------------------------
id -u "$APP_USER" >/dev/null 2>&1 || useradd --system --create-home --shell /bin/bash "$APP_USER"
mkdir -p "$APP_DIR" "$DATA_DIR"/{cache,logs,backups,demos/inbox,demos/processed,demos/tmp}
chown -R "$APP_USER:$APP_USER" "$APP_DIR" "$DATA_DIR"

# Root'un yetkili anahtarı (OVH kurulumda ekledi) dağıtım için squadmind kullanıcısına da
install -d -m 700 -o "$APP_USER" -g "$APP_USER" "/home/$APP_USER/.ssh"
if [ -f /root/.ssh/authorized_keys ]; then
  install -m 600 -o "$APP_USER" -g "$APP_USER" /root/.ssh/authorized_keys "/home/$APP_USER/.ssh/authorized_keys"
fi
# OVH'nin varsayılan kullanıcısı (ubuntu) varsa onun anahtarları da
if [ -f /home/ubuntu/.ssh/authorized_keys ]; then
  cat /home/ubuntu/.ssh/authorized_keys >> "/home/$APP_USER/.ssh/authorized_keys"
  sort -u -o "/home/$APP_USER/.ssh/authorized_keys" "/home/$APP_USER/.ssh/authorized_keys"
fi
# Servisi yeniden başlatma yetkisi (yalnız bu servisler, şifresiz)
cat > /etc/sudoers.d/squadmind <<EOF
$APP_USER ALL=(root) NOPASSWD: /usr/bin/systemctl restart squadmind-web, /usr/bin/systemctl stop squadmind-web, /usr/bin/systemctl start squadmind-web, /usr/bin/systemctl status squadmind-web
EOF
chmod 440 /etc/sudoers.d/squadmind

# --- SSH: yalnız anahtar ------------------------------------------------------------------
cat > /etc/ssh/sshd_config.d/10-squadmind.conf <<'EOF'
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin prohibit-password
EOF
systemctl reload ssh || systemctl reload sshd || true

# --- güvenlik duvarı: yalnız SSH ----------------------------------------------------------
ufw default deny incoming
ufw default allow outgoing
ufw allow OpenSSH
ufw --force enable

# --- uv + Python (squadmind kullanıcısı için) ---------------------------------------------
sudo -u "$APP_USER" bash -lc 'curl -LsSf https://astral.sh/uv/install.sh | sh'

# --- cloudflared (Cloudflare'in resmi paket deposu) ---------------------------------------
mkdir -p --mode=0755 /usr/share/keyrings
curl -fsSL https://pkg.cloudflare.com/cloudflare-main.gpg -o /usr/share/keyrings/cloudflare-main.gpg
echo "deb [signed-by=/usr/share/keyrings/cloudflare-main.gpg] https://pkg.cloudflare.com/cloudflared any main" \
  > /etc/apt/sources.list.d/cloudflared.list
apt-get update -q && apt-get -yq install cloudflared
mkdir -p /etc/cloudflared

# --- systemd servisleri -------------------------------------------------------------------
install -m 644 "$(dirname "$0")/squadmind-web.service" /etc/systemd/system/squadmind-web.service
install -m 644 "$(dirname "$0")/squadmind-tunnel.service" /etc/systemd/system/squadmind-tunnel.service
install -m 644 "$(dirname "$0")/squadmind-backup.service" /etc/systemd/system/squadmind-backup.service
install -m 644 "$(dirname "$0")/squadmind-backup.timer" /etc/systemd/system/squadmind-backup.timer
install -m 755 "$(dirname "$0")/backup.sh" /usr/local/bin/squadmind-backup
systemctl daemon-reload
systemctl enable squadmind-web squadmind-tunnel squadmind-backup.timer

echo "Hazır. Sıradaki: migrate.ps1 (kod + veri + .env + tünel kimliği), sonra servisleri başlat."
