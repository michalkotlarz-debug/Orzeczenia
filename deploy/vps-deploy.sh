#!/bin/bash
# Wdrozenie aplikacji na VPS (home.pl, portalorzeczen.pl).
# Uruchamiaj z katalogu glownego repo: bash deploy/vps-deploy.sh
set -euo pipefail

HOST="deploy@87.106.31.76"
SSH_KEY="$HOME/.ssh/orzeczenia_vps"
REMOTE_DIR="/home/deploy/orzeczenia"

SSH="ssh -i $SSH_KEY -o StrictHostKeyChecking=accept-new"
SCP="scp -i $SSH_KEY -o StrictHostKeyChecking=accept-new"

echo "==> Pakowanie zrodel..."
TMP_TAR=$(mktemp -u /tmp/orzeczenia-src-XXXXXX.tar.gz 2>/dev/null || echo "/tmp/orzeczenia-src-$$.tar.gz")
tar -czf "$TMP_TAR" \
  --exclude='.venv' --exclude='.git' --exclude='dane' --exclude='__pycache__' --exclude='.vercel' \
  Dockerfile app.py config.yaml pyproject.toml requirements.txt orzeczenia tests

echo "==> Wysylanie na serwer..."
$SCP "$TMP_TAR" "$HOST:$REMOTE_DIR/orzeczenia-src.tar.gz"
rm -f "$TMP_TAR"

echo "==> Rozpakowanie, build, restart kontenera..."
$SSH "$HOST" "
  set -e
  cd $REMOTE_DIR
  rm -rf orzeczenia app.py config.yaml pyproject.toml requirements.txt Dockerfile tests
  tar -xzf orzeczenia-src.tar.gz
  rm orzeczenia-src.tar.gz
  sudo docker build -t orzecznik:latest .
  sudo docker rm -f orzecznik || true
  sudo docker run -d --name orzecznik --network host --restart unless-stopped --env-file .env orzecznik:latest
  sleep 3
  echo '--- health check ---'
  curl -s http://127.0.0.1:8000/api/health
  echo
"

echo "==> Gotowe. Sprawdz https://portalorzeczen.pl"
