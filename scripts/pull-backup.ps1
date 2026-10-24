# Sunucudaki gece veritabanı yedeğini (squadmind-YYYY-MM-DD.tar.gz) bu bilgisayara kopyalar.
# Neden: yedekler yalnız sunucuda dururken sunucu kaybolursa yedek de gider (24.10.2026 OVH altyapı arızası).
# Görev Zamanlayıcı "SquadMind-Yedek" her gün 07:40'ta çalıştırır (sunucu yedeği 04:17 UTC = 07:17 TR).
# Kurulum: scripts/install-backup-pull.ps1. Son 30 kopya tutulur. Günlük: <hedef>\pull.log

$ErrorActionPreference = 'Stop'
$server = 'squadmind@213.32.17.246'
$key = Join-Path $env:USERPROFILE '.ssh\squadmind_vps'
$dest = 'C:\Users\furka\squadmind-data\sunucu-yedekleri'
$log = Join-Path $dest 'pull.log'
New-Item -ItemType Directory -Force $dest | Out-Null

function Write-Log($msg) { Add-Content -Path $log -Value ("{0:yyyy-MM-dd HH:mm:ss}  {1}" -f (Get-Date), $msg) -Encoding utf8 }

try {
    # Sunucudaki en yeni yedeğin adı
    $latest = (& ssh -i $key -o BatchMode=yes -o ConnectTimeout=20 $server `
        'ls -1t /var/lib/squadmind/backups/squadmind-*.tar.gz | head -1').Trim()
    if (-not $latest) { throw 'sunucuda yedek bulunamadı' }
    $name = Split-Path $latest -Leaf
    $target = Join-Path $dest $name
    if (Test-Path $target) { Write-Log "zaten var: $name"; exit 0 }
    & scp -i $key -o BatchMode=yes -o ConnectTimeout=20 -q "${server}:$latest" $target
    if ($LASTEXITCODE -ne 0) { throw "scp çıkış kodu $LASTEXITCODE" }
    $size = [math]::Round((Get-Item $target).Length / 1MB, 2)
    Write-Log "kopyalandı: $name ($size MB)"
    # Son 30 kopya kalsın
    Get-ChildItem $dest -Filter 'squadmind-*.tar.gz' | Sort-Object LastWriteTime -Descending |
        Select-Object -Skip 30 | Remove-Item -Force
}
catch {
    Write-Log "HATA: $($_.Exception.Message)"
    exit 1
}
