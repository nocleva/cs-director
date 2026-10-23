# Sunucuya geçiş (OVH VPS, Ubuntu 24.04)

Git Bash'ten, proje kökünde. `<ip>` = OVH'nin e-postadaki IPv4 adresi. Bağlantı anahtarı
`~/.ssh/squadmind_vps` (açık kısmı siparişte eklendi; eklenmediyse ilk girişte root şifresiyle
`~/.ssh/authorized_keys`'e yapıştırılır).

1. Hazırlık (bir kez, root):
   `scp -i ~/.ssh/squadmind_vps -r scripts/server root@<ip>:/root/server && ssh -i ~/.ssh/squadmind_vps root@<ip> 'bash /root/server/setup.sh'`
   (OVH root yerine `ubuntu` kullanıcısı verdiyse: `ubuntu@<ip>` ve `sudo bash ...`)
2. Kod: `scripts/server/deploy.sh <ip>` (ilk seferde .env olmadığı için web açılmaz, normal)
3. Veri + gizli ayarlar + tünel (bir kez): `scripts/server/migrate.sh <ip>`
   Bu bilgisayardaki web ve tünel durur, Görev Zamanlayıcı görevi kapatılır.
4. Sonraki her güncelleme: commit → `scripts/server/deploy.sh <ip>`

Sunucuda: kod `/opt/squadmind`, veri `/var/lib/squadmind` (günlükler `logs/`, gece yedeği
`backups/`, 04:17, son 14), servisler `squadmind-web`, `squadmind-tunnel`, `squadmind-backup.timer`.
Dışarıya açık tek port SSH; site Cloudflare tünelinden gelir.
Geri dönüş: `ssh root@<ip> systemctl stop squadmind-tunnel` → bu bilgisayarda
`Enable-ScheduledTask SquadMind` + `scripts/start-squadmind.ps1` (DB'yi sunucudan geri kopyala).
