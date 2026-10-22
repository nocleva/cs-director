# SquadMind'ı başlatır: web sunucusu (port 8000) + kalıcı Cloudflare tüneli (app.squadmind.info).
# Görev Zamanlayıcı bunu oturum açılınca gizli pencerede çalıştırır (scripts/install-autostart.ps1).
# Zaten çalışıyorsa ikinci kopyayı başlatmaz. Günlükler: <veri klasörü>\logs\

$ErrorActionPreference = 'Stop'
$project = Split-Path -Parent $PSScriptRoot
$cloudflared = 'C:\Program Files (x86)\cloudflared\cloudflared.exe'

# Veri klasörü .env'deki SQUADMIND_DATA_DIR'dan (yoksa proje içi data/)
$dataDir = Join-Path $project 'data'
$envFile = Join-Path $project '.env'
if (Test-Path $envFile) {
    $line = Select-String -Path $envFile -Pattern '^SQUADMIND_DATA_DIR=(.+)$' | Select-Object -First 1
    if ($line) { $dataDir = $line.Matches[0].Groups[1].Value.Trim() }
}
$logs = Join-Path $dataDir 'logs'
New-Item -ItemType Directory -Force $logs | Out-Null

# Türkçe karakterli çıktı için UTF-8
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'

# Web sunucusu: port 8000 boşsa başlat
$portBusy = Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue
if (-not $portBusy) {
    Start-Process -FilePath (Join-Path $project '.venv\Scripts\squadmind.exe') -ArgumentList 'web', '--port', '8000' `
        -WorkingDirectory $project -WindowStyle Hidden `
        -RedirectStandardOutput (Join-Path $logs 'web.log') -RedirectStandardError (Join-Path $logs 'web.err.log')
}

# Kalıcı tünel: çalışmıyorsa başlat (ayar: ~/.cloudflared/config.yml). `cloudflared` Windows
# hizmeti, ayarı sistem profilinde (C:\Windows\System32\config\systemprofile\.cloudflared)
# bulursa onun yanında ikinci bağlantı olur; ikisi de aynı sunucuya gider, zararı yok.
$tunnelUp = Get-CimInstance Win32_Process -Filter "Name = 'cloudflared.exe'" |
    Where-Object { $_.CommandLine -like '*tunnel*run*squadmind*' }
if (-not $tunnelUp) {
    Start-Process -FilePath $cloudflared -ArgumentList 'tunnel', '--no-autoupdate', 'run', 'squadmind' `
        -WindowStyle Hidden `
        -RedirectStandardOutput (Join-Path $logs 'tunnel.log') -RedirectStandardError (Join-Path $logs 'tunnel.err.log')
}
