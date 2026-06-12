#!/usr/bin/env bash
# =============================================================================
# edgeX 하이브리드 트레이딩 봇 — VPS 원클릭 설치 스크립트
# 지원 OS : Ubuntu 22.04 LTS (권장) / Ubuntu 20.04 LTS
#
# 실행 방법:
#   chmod +x deploy/setup_vps.sh
#   sudo ./deploy/setup_vps.sh
# =============================================================================
set -euo pipefail

# ── 색상 출력 ──────────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
info()  { echo -e "${GREEN}[INFO]${NC}  $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*"; exit 1; }

[[ $EUID -ne 0 ]] && error "root 권한으로 실행하세요: sudo $0"

APP_DIR="${APP_DIR:-/opt/edgex-trader}"
APP_USER="trader"
REPO_URL="${REPO_URL:-}"   # 비어 있으면 git clone 단계를 건너뜀

info "===== edgeX Trader — VPS 환경 구성 시작 ====="

# ── 1. 시스템 패키지 업데이트 ─────────────────────────────────────────────────
info "[1/8] 시스템 패키지 업데이트"
apt-get update -qq
apt-get upgrade -y -qq
apt-get install -y -qq \
    curl wget git unzip \
    python3.11 python3.11-venv python3-pip \
    docker.io docker-compose \
    ufw fail2ban htop jq

# ── 2. 방화벽 설정 ────────────────────────────────────────────────────────────
info "[2/8] UFW 방화벽 설정 (SSH 22 허용, 나머지 차단)"
ufw --force reset
ufw default deny incoming
ufw default allow outgoing
ufw allow ssh
ufw --force enable
info "방화벽 상태: $(ufw status | head -1)"

# ── 3. 전용 서비스 계정 생성 ──────────────────────────────────────────────────
info "[3/8] 서비스 계정 '${APP_USER}' 생성"
if ! id "${APP_USER}" &>/dev/null; then
    useradd -r -m -s /bin/bash "${APP_USER}"
    usermod -aG docker "${APP_USER}"
    info "계정 생성 완료"
else
    warn "계정 '${APP_USER}'이 이미 존재합니다 — 건너뜀"
fi

# ── 4. 애플리케이션 디렉터리 준비 ────────────────────────────────────────────
info "[4/8] 애플리케이션 디렉터리 준비: ${APP_DIR}"
mkdir -p "${APP_DIR}" "${APP_DIR}/data" "${APP_DIR}/config" "${APP_DIR}/logs"
chown -R "${APP_USER}:${APP_USER}" "${APP_DIR}"

# 저장소 클론 (REPO_URL 지정 시)
if [[ -n "${REPO_URL}" ]]; then
    info "저장소 클론: ${REPO_URL} → ${APP_DIR}"
    sudo -u "${APP_USER}" git clone "${REPO_URL}" "${APP_DIR}" || \
        { warn "이미 클론된 디렉터리 — git pull 실행"; \
          sudo -u "${APP_USER}" git -C "${APP_DIR}" pull; }
else
    warn "REPO_URL 미설정 — 저장소 클론 건너뜀. 수동으로 ${APP_DIR}에 코드를 복사하세요."
fi

# ── 5. .env 파일 확인 ─────────────────────────────────────────────────────────
info "[5/8] 환경 변수 파일 확인"
if [[ ! -f "${APP_DIR}/.env" ]]; then
    if [[ -f "${APP_DIR}/.env.example" ]]; then
        cp "${APP_DIR}/.env.example" "${APP_DIR}/.env"
        chown "${APP_USER}:${APP_USER}" "${APP_DIR}/.env"
        chmod 600 "${APP_DIR}/.env"  # 소유자만 읽기/쓰기
        warn ".env.example을 복사했습니다. ${APP_DIR}/.env 를 편집하여 API 키를 입력하세요!"
    else
        warn ".env 파일이 없습니다. ${APP_DIR}/.env 를 직접 생성해야 합니다."
    fi
else
    chmod 600 "${APP_DIR}/.env"
    info ".env 파일 확인 완료 (권한 600 설정)"
fi

# ── 6. Docker 이미지 빌드 ─────────────────────────────────────────────────────
if [[ -f "${APP_DIR}/Dockerfile" ]]; then
    info "[6/8] Docker 이미지 빌드 (edgex-trader:latest)"
    cd "${APP_DIR}"
    sudo -u "${APP_USER}" docker build -t edgex-trader:latest .
else
    warn "[6/8] Dockerfile 없음 — Docker 빌드 건너뜀"
fi

# ── 7. systemd 서비스 설치 ────────────────────────────────────────────────────
info "[7/8] systemd 서비스 설치"
SERVICE_FILE="/etc/systemd/system/edgex-trader.service"

cat > "${SERVICE_FILE}" << EOF
[Unit]
Description=edgeX Hybrid Trading Bot
After=network-online.target docker.service
Wants=network-online.target
Requires=docker.service

[Service]
Type=simple
User=${APP_USER}
WorkingDirectory=${APP_DIR}
EnvironmentFile=${APP_DIR}/.env

# 실전 모드 실행 (드라이런은 아래 줄 주석 해제)
ExecStart=/usr/bin/docker-compose up --no-color trader
# ExecStart=/usr/bin/docker-compose --profile dryrun up --no-color trader_dryrun

ExecStop=/usr/bin/docker-compose down
Restart=on-failure
RestartSec=30
StandardOutput=append:${APP_DIR}/logs/trader.log
StandardError=append:${APP_DIR}/logs/trader-error.log

# 프로세스 자원 제한
LimitNOFILE=65536
MemoryMax=2G

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable edgex-trader
info "systemd 서비스 등록 완료: edgex-trader.service"

# ── 8. fail2ban SSH 보호 설정 ─────────────────────────────────────────────────
info "[8/8] fail2ban SSH 보호 활성화"
cat > /etc/fail2ban/jail.local << 'EOF'
[DEFAULT]
bantime  = 3600
findtime = 600
maxretry = 5

[sshd]
enabled = true
EOF
systemctl restart fail2ban

# ── 완료 메시지 ───────────────────────────────────────────────────────────────
echo ""
echo -e "${GREEN}╔══════════════════════════════════════════════════════════╗${NC}"
echo -e "${GREEN}║        VPS 환경 구성 완료!                              ║${NC}"
echo -e "${GREEN}╚══════════════════════════════════════════════════════════╝${NC}"
echo ""
echo "  다음 단계:"
echo "  1) ${APP_DIR}/.env  파일에 API 키 입력"
echo "  2) sudo systemctl start edgex-trader   # 봇 시작"
echo "  3) sudo systemctl status edgex-trader  # 상태 확인"
echo "  4) tail -f ${APP_DIR}/logs/trader.log  # 실시간 로그"
echo ""
echo "  주요 명령어:"
echo "  sudo systemctl stop    edgex-trader  # 봇 중지"
echo "  sudo systemctl restart edgex-trader  # 재시작"
echo "  sudo journalctl -u edgex-trader -f   # journald 로그"
