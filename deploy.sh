#!/data/data/com.termux/files/usr/bin/bash
# hanscloudserge.web.id — deploy (Termux)
set -e
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"; cd "$DIR"

if [[ "${1:-}" == "--update" ]]; then echo "> pkg update"; pkg update -y; fi
need=(); command -v python3 >/dev/null 2>&1 || need+=(python)
command -v git >/dev/null 2>&1 || need+=(git)
command -v curl >/dev/null 2>&1 || need+=(curl)
command -v cloudflared >/dev/null 2>&1 || need+=(cloudflared)
command -v termux-wake-lock >/dev/null 2>&1 || need+=(termux-tools)
if ((${#need[@]})); then echo "> pkg install ${need[*]}"; pkg install -y "${need[@]}"; fi

command -v termux-wake-lock >/dev/null 2>&1 && termux-wake-lock 2>/dev/null || true
echo "> stop old"
pkill -f "python3.*server.py" 2>/dev/null || true
pkill -f "cloudflared.*hans-portfolio" 2>/dev/null || true
sleep 1

[ -f config.json ] || { [ -f config.sample.json ] && cp config.sample.json config.json && echo "> seed config.json"; }

TOKEN_FILE="$HOME/.config/hans-portfolio/admin_token"
mkdir -p "$(dirname "$TOKEN_FILE")"
# --new-token forces rotation without prompt
if [[ "${1:-}" == "--new-token" || "${2:-}" == "--new-token" ]]; then
  ADMIN_TOKEN="$(openssl rand -hex 16 2>/dev/null || head -c 16 /dev/urandom | od -An -tx1 | tr -d ' \n' | head -c 32)"
  echo -n "$ADMIN_TOKEN" > "$TOKEN_FILE"; chmod 600 "$TOKEN_FILE"
  echo "> new token $TOKEN_FILE"
else
  if [ -n "${ADMIN_TOKEN:-}" ]; then
    echo -n "$ADMIN_TOKEN" > "$TOKEN_FILE"; chmod 600 "$TOKEN_FILE"
  elif [ -f "$TOKEN_FILE" ]; then
    ADMIN_TOKEN="$(cat "$TOKEN_FILE")"
    read -p "Generate new token? y/N: " ans < /dev/tty
    if [[ "$ans" == "y" || "$ans" == "Y" ]]; then
      ADMIN_TOKEN="$(openssl rand -hex 16 2>/dev/null || head -c 16 /dev/urandom | od -An -tx1 | tr -d ' \n' | head -c 32)"
      echo -n "$ADMIN_TOKEN" > "$TOKEN_FILE"; chmod 600 "$TOKEN_FILE"
      echo "> new token $TOKEN_FILE"
    fi
  else
    ADMIN_TOKEN="$(openssl rand -hex 16 2>/dev/null || head -c 16 /dev/urandom | od -An -tx1 | tr -d ' \n' | head -c 32)"
    echo -n "$ADMIN_TOKEN" > "$TOKEN_FILE"; chmod 600 "$TOKEN_FILE"
    echo "> new token $TOKEN_FILE"
  fi
fi

echo "> start server :8000"
ADMIN_TOKEN="$ADMIN_TOKEN" nohup python3 server.py --port 8000 > server.log 2>&1 &
sleep 1
echo "  server ok"

CF_DIR="$HOME/.cloudflared"; mkdir -p "$CF_DIR"
TUNNEL_ID="ed1282dc-37e6-4bd5-a4fc-30d06dc6c727"; CREDS="$CF_DIR/$TUNNEL_ID.json"
if [ -n "${TUNNEL_TOKEN:-}" ]; then echo "$TUNNEL_TOKEN" | base64 -d > "$CREDS" 2>/dev/null || echo "$TUNNEL_TOKEN" > "$CREDS"
elif [ ! -f "$CREDS" ]; then cat << 'EOF' > "$CREDS"
{"AccountTag":"YOUR_ACCOUNT_TAG","TunnelSecret":"YOUR_TUNNEL_SECRET","TunnelID":"YOUR_TUNNEL_ID","Endpoint":""}
EOF
fi
cat << EOF > "$CF_DIR/config.yml"
tunnel: hans-portfolio
credentials-file: $CREDS
ingress:
  - hostname: hanscloudserge.web.id
    service: http://localhost:8000
  - hostname: www.hanscloudserge.web.id
    service: http://localhost:8000
  - service: http_status:404
EOF

echo "> start tunnel"
nohup cloudflared tunnel run hans-portfolio > tunnel.log 2>&1 &
sleep 3
if pgrep -f "cloudflared.*hans-portfolio" >/dev/null 2>&1; then echo "  tunnel ok"; else echo "! tunnel not running — check tunnel.log"; fi
echo ""
echo "  https://hanscloudserge.web.id"
echo "  token $ADMIN_TOKEN"
echo "  local http://localhost:8000"
echo ""
