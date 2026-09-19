#!/bin/bash
set -e

# TransferStats Agent — Установщик
#
# Использование (ручная установка, вариант «скриптом»):
#   curl -sSL <INSTALL_URL> | bash -s -- <TOKEN> [--management bot|site] [--harden]
#
# Использование (автоматическая установка ботом по SSH):
#   bash setup.sh <TOKEN> --management bot --harden --non-interactive \
#        --endpoint https://bot.transfer-stats.online
#
# Флаги:
#   --token T / позиционный T   токен агента (выдаётся ботом /indirect_auth)
#   --management bot|site       где управлять группами: в боте или на локальном сайте
#   --endpoint URL              адрес сервера бота (ingest API)
#   --repo URL                  git-репозиторий с кодом агента
#   --harden                    усилить безопасность сервера (ufw + fail2ban + apt update)
#   --non-interactive           без ожидания терминала: сайт настройки под systemd,
#                               URL туннеля отправляется на сервер (бот его покажет)

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

TOKEN=""
MANAGEMENT="site"
ENDPOINT="${AGENT_ENDPOINT:-https://bot.transfer-stats.online}"
REPO="${AGENT_REPO:-https://github.com/Skorpion11215/telegram-agent.git}"
INSTALL_DIR="/opt/telegram-agent"
HARDEN=0
NONINTERACTIVE=0

while [ $# -gt 0 ]; do
    case "$1" in
        --token) TOKEN="$2"; shift 2 ;;
        --management) MANAGEMENT="$2"; shift 2 ;;
        --endpoint) ENDPOINT="$2"; shift 2 ;;
        --repo) REPO="$2"; shift 2 ;;
        --harden) HARDEN=1; shift ;;
        --non-interactive) NONINTERACTIVE=1; shift ;;
        -h|--help)
            grep '^#' "$0" | sed 's/^# \{0,1\}//'
            exit 0 ;;
        -*) echo -e "${RED}Неизвестный флаг: $1${NC}"; exit 1 ;;
        *)
            if [ -z "$TOKEN" ]; then TOKEN="$1"; fi
            shift ;;
    esac
done

if [ "$MANAGEMENT" != "bot" ] && [ "$MANAGEMENT" != "site" ]; then
    echo -e "${RED}Ошибка: --management должен быть bot или site${NC}"
    exit 1
fi

echo -e "${GREEN}TransferStats Agent — Установщик (management=$MANAGEMENT)${NC}"
echo ""

# Проверка root
if [ "$EUID" -ne 0 ]; then
    echo -e "${RED}Ошибка: запустите от root (sudo)${NC}"
    exit 1
fi

if [ -z "$TOKEN" ]; then
    echo -e "${RED}Ошибка: укажите токен, который выдал бот.${NC}"
    echo "Использование: curl -sSL <INSTALL_URL> | bash -s -- <TOKEN> --management bot"
    exit 1
fi

ENDPOINT="${ENDPOINT%/}"

echo -e "${YELLOW}[1/8] Проверка сервера...${NC}"
HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" "${ENDPOINT}/health")
if [ "$HTTP_CODE" != "200" ]; then
    echo -e "${RED}Ошибка: сервер недоступен (HTTP $HTTP_CODE): ${ENDPOINT}${NC}"
    exit 1
fi
echo -e "${GREEN}  OK${NC}"

echo -e "${YELLOW}[2/8] Установка зависимостей...${NC}"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3 python3-pip python3-venv git curl jq build-essential python3-dev 2>/dev/null
echo -e "${GREEN}  OK${NC}"

echo -e "${YELLOW}[3/8] Загрузка кода...${NC}"
if [ -d "$INSTALL_DIR/.git" ]; then
    cd "$INSTALL_DIR"
    git remote set-url origin "$REPO"
    git fetch --quiet origin
    git reset --hard --quiet origin/HEAD 2>/dev/null || git pull --quiet || true
else
    rm -rf "$INSTALL_DIR"
    git clone --depth=1 --quiet "$REPO" "$INSTALL_DIR"
    cd "$INSTALL_DIR"
fi
echo -e "${GREEN}  OK${NC}"

echo -e "${YELLOW}[4/8] Настройка Python...${NC}"
python3 -m venv venv
./venv/bin/pip install --quiet --upgrade pip
./venv/bin/pip install --quiet -r requirements.txt
echo -e "${GREEN}  OK${NC}"

echo -e "${YELLOW}[5/8] Компиляция Cython...${NC}"
./venv/bin/pip install --quiet setuptools cython 2>/dev/null
if ./venv/bin/python core/setup.py build_ext --inplace 2>/dev/null; then
    echo -e "${GREEN}  Скомпилировано${NC}"
else
    echo -e "${YELLOW}  Предупреждение: Cython не скомпилирован, используется fallback${NC}"
fi

echo -e "${YELLOW}[6/8] Установка cloudflared...${NC}"
if ! command -v cloudflared &> /dev/null; then
    curl -L -s -o /usr/local/bin/cloudflared \
        "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64"
    chmod +x /usr/local/bin/cloudflared
fi
echo -e "${GREEN}  OK${NC}"

echo -e "${YELLOW}[7/8] Настройка systemd...${NC}"
cat > /etc/systemd/system/telegram-agent.service << EOF
[Unit]
Description=TransferStats Agent
After=network.target

[Service]
Type=simple
WorkingDirectory=${INSTALL_DIR}
ExecStart=${INSTALL_DIR}/venv/bin/python agent.py
Restart=always
RestartSec=10
User=root
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
echo -e "${GREEN}  OK${NC}"

# ── Конфигурация агента ─────────────────────────────────────────────────────
cat > "$INSTALL_DIR/agent.ini" << EOF
[telegram]
api_id = 
api_hash = 
phone = 
session = 

[agent]
token = ${TOKEN}
api_key = 
api_secret = 
endpoint = ${ENDPOINT}
management = ${MANAGEMENT}

[groups]
ids = 
EOF
chmod 600 "$INSTALL_DIR/agent.ini"

# ── [8/8] Безопасность сервера (по флагу --harden) ───────────────────────────
echo -e "${YELLOW}[8/8] Безопасность сервера...${NC}"
if [ "$HARDEN" = "1" ]; then
    apt-get install -y -qq ufw fail2ban 2>/dev/null

    # apt: свежие патчи безопасности
    apt-get upgrade -y -qq -o Dpkg::Options::="--force-confdef" -o Dpkg::Options::="--force-confold" 2>/dev/null || true

    # ufw: закрываем всё, кроме SSH
    if ! ufw status | grep -q "Status: active"; then
        ufw --force default deny incoming >/dev/null
        ufw --force default allow outgoing >/dev/null
        ufw --force allow OpenSSH >/dev/null
        ufw --force enable >/dev/null
    fi
    systemctl enable --now ufw >/dev/null 2>&1 || true

    # fail2ban: бан брутфорса SSH
    cat > /etc/fail2ban/jail.d/sshd.local << 'EOF'
[sshd]
enabled = true
backend = systemd
port = ssh
maxretry = 3
findtime = 300
bantime = 3600
EOF
    systemctl enable --now fail2ban >/dev/null 2>&1 || systemctl restart fail2ban >/dev/null 2>&1 || true
    echo -e "${GREEN}  ufw + fail2ban настроены${NC}"
else
    echo -e "${YELLOW}  Пропущено (флаг --harden). Рекомендуется при авто-установке.${NC}"
fi

# ── Запуск сайта настройки ───────────────────────────────────────────────────
echo ""
echo -e "${GREEN}=========================================${NC}"
echo -e "${GREEN}Установка завершена!${NC}"
echo -e "${YELLOW}Запуск локального сайта авторизации...${NC}"

cd "$INSTALL_DIR"
source venv/bin/activate

run_first_setup_site() {
    # фон: cloudflared; извлекаем URL; сообщаем на сервер (бот покажет пользователю)
    cloudflared tunnel --url http://127.0.0.1:8080 > /tmp/cf_tunnel.log 2>&1 &
    local cf_pid=$!
    local url=""
    for _ in $(seq 1 30); do
        sleep 1
        url=$(grep -oE "https://[a-z0-9-]+\.trycloudflare\.com" /tmp/cf_tunnel.log 2>/dev/null | head -1)
        [ -n "$url" ] && break
    done
    SETUP_URL="$url"
    SETUP_CF_PID="$cf_pid"
}

if [ "$NONINTERACTIVE" = "1" ]; then
    # Авто-установка: сайт настройки живёт в отдельном systemd-сервисе,
    # URL туннеля репортится на сервер, бот показывает его пользователю.
    cat > /etc/systemd/system/telegram-agent-setup.service << EOF
[Unit]
Description=TransferStats Agent — первичная авторизация (сайт настройки)
After=network.target

[Service]
Type=simple
WorkingDirectory=${INSTALL_DIR}
ExecStart=${INSTALL_DIR}/run_setup_web.sh
Restart=no
User=root
Environment=PYTHONUNBUFFERED=1
EOF

    cat > "$INSTALL_DIR/run_setup_web.sh" << EOF
#!/bin/bash
cd ${INSTALL_DIR}
source venv/bin/activate
cloudflared tunnel --url http://127.0.0.1:8080 > /tmp/cf_tunnel.log 2>&1 &
CF_PID=\$!
URL=""
for _ in \$(seq 1 30); do
    sleep 1
    URL=\$(grep -oE "https://[a-z0-9-]+\\\\.trycloudflare\\\\.com" /tmp/cf_tunnel.log 2>/dev/null | head -1)
    [ -n "\$URL" ] && break
done
echo "SETUP_URL=\$URL"
if [ -n "\$URL" ]; then
    curl -s -m 15 -X POST "${ENDPOINT}/api/install-url" \\
        -H "Content-Type: application/json" \\
        -d "{\\"token\\": \\"${TOKEN}\\", \\"url\\": \\"\$URL\\"}" || true
fi
python3 setup_web.py --token="${TOKEN}" --management="${MANAGEMENT}"
kill \$CF_PID 2>/dev/null
# после завершения настройки сайт больше не нужен
systemctl disable telegram-agent-setup.service 2>/dev/null || true
EOF
    chmod 700 "$INSTALL_DIR/run_setup_web.sh"

    systemctl daemon-reload
    systemctl enable --now telegram-agent-setup.service
    echo -e "${GREEN}  Сайт авторизации запущен; ссылку бот пришлёт в чат.${NC}"
    exit 0
fi

# Интерактивный режим: как раньше — показываем URL в консоль
python setup_web.py --token="$TOKEN" --management="$MANAGEMENT" &
WEB_PID=$!
run_first_setup_site
URL="$SETUP_URL"

if [ -z "$URL" ]; then
    echo -e "${RED}Ошибка: не удалось получить публичный URL${NC}"
    echo "Попробуйте запустить вручную:"
    echo "  cd $INSTALL_DIR && source venv/bin/activate"
    echo "  python setup_web.py --token=$TOKEN --management=$MANAGEMENT &"
    echo "  cloudflared tunnel --url http://127.0.0.1:8080"
    kill $WEB_PID 2>/dev/null
    exit 1
fi

# Сообщим URL и серверу — бот покажет его в чате (для обоих режимов полезно)
curl -s -m 15 -X POST "${ENDPOINT}/api/install-url" \
    -H "Content-Type: application/json" \
    -d "{\"token\": \"${TOKEN}\", \"url\": \"${URL}\"}" >/dev/null 2>&1 || true

echo ""
if [ "$MANAGEMENT" = "bot" ]; then
    echo -e "${GREEN}Откройте на телефоне — нужно только авторизовать Telegram:${NC}"
else
    echo -e "${GREEN}Откройте на телефоне или компьютере:${NC}"
fi
echo -e "${GREEN}  ${URL}${NC}"
echo -e "${YELLOW}  Ссылка действует до завершения настройки.${NC}"
echo ""

wait $WEB_PID 2>/dev/null || true
kill $SETUP_CF_PID 2>/dev/null
echo -e "${GREEN}Настройка завершена. Агент запущен.${NC}"
