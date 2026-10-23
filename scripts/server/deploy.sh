#!/usr/bin/env bash
# Kodu sunucuya gönderir ve web'i yeniden başlatır (Git Bash'ten, yerel bilgisayarda).
# Kullanım: scripts/server/deploy.sh <sunucu-ip>
# Gönderilen: git'teki son commit (git archive; .env, veri ve .venv gitmez). Commit edilmemiş
# değişiklik varsa uyarır ve durur.
set -euo pipefail
HOST=${1:?"kullanım: deploy.sh <sunucu-ip>"}
KEY="$HOME/.ssh/squadmind_vps"
SSH=(ssh -i "$KEY" -o StrictHostKeyChecking=accept-new "squadmind@$HOST")
cd "$(git rev-parse --show-toplevel)"

if [ -n "$(git status --porcelain -- src scripts pyproject.toml uv.lock)" ]; then
  echo "Commit edilmemiş değişiklik var; önce commit et." >&2; exit 1
fi

echo "== kod ($(git log --oneline -1))"
# Eski kodu sil (.venv kalsın: bağımlılıklar yeniden kurulmasın), yenisini aç
git archive --format=tar HEAD | "${SSH[@]}" '
  set -e; cd /opt/squadmind
  find . -mindepth 1 -maxdepth 1 ! -name .venv ! -name .env -exec rm -rf {} +
  tar -x -f -'
echo "== bağımlılıklar"
"${SSH[@]}" 'cd /opt/squadmind && ~/.local/bin/uv sync --frozen --no-dev 2>&1 | tail -3'
echo "== yeniden başlat"
"${SSH[@]}" 'sudo /usr/bin/systemctl restart squadmind-web && sleep 4 && curl -s -o /dev/null -w "yerel %{http_code}\n" http://127.0.0.1:8000/'
