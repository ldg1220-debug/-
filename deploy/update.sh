#!/usr/bin/env bash
# =============================================================================
# 무중단 배포 스크립트 — git pull → 이미지 재빌드 → 서비스 재시작
#
# 사용법:
#   ./deploy/update.sh              # main 브랜치 최신으로 업데이트
#   ./deploy/update.sh my-branch    # 특정 브랜치로 업데이트
# =============================================================================
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/edgex-trader}"
BRANCH="${1:-main}"
TIMESTAMP=$(date '+%Y-%m-%d %H:%M:%S')

echo "[${TIMESTAMP}] 배포 시작 — 브랜치: ${BRANCH}"

cd "${APP_DIR}"

# ── 1. 현재 커밋 저장 (롤백용) ────────────────────────────────────────────────
PREV_COMMIT=$(git rev-parse --short HEAD 2>/dev/null || echo "unknown")
echo "  이전 커밋: ${PREV_COMMIT}"

# ── 2. git pull ───────────────────────────────────────────────────────────────
git fetch origin "${BRANCH}"
git checkout "${BRANCH}"
git pull origin "${BRANCH}"
NEW_COMMIT=$(git rev-parse --short HEAD)
echo "  새 커밋: ${NEW_COMMIT}"

if [[ "${PREV_COMMIT}" == "${NEW_COMMIT}" ]]; then
    echo "  변경 사항 없음 — 배포 건너뜀"
    exit 0
fi

# ── 3. Docker 이미지 재빌드 ────────────────────────────────────────────────────
echo "  Docker 이미지 빌드 중..."
docker build -t edgex-trader:latest -t "edgex-trader:${NEW_COMMIT}" .
echo "  이미지 빌드 완료"

# ── 4. 서비스 재시작 (graceful) ───────────────────────────────────────────────
if systemctl is-active edgex-trader &>/dev/null; then
    echo "  systemd 서비스 재시작..."
    systemctl restart edgex-trader
    sleep 5
    if systemctl is-active edgex-trader &>/dev/null; then
        echo "  서비스 정상 가동 확인"
    else
        echo "  [ERROR] 재시작 실패 — 롤백 시도"
        git checkout "${PREV_COMMIT}"
        docker build -t edgex-trader:latest .
        systemctl start edgex-trader
        exit 1
    fi
elif command -v pm2 &>/dev/null && pm2 list 2>/dev/null | grep -q "edgex-trader"; then
    echo "  PM2 프로세스 재시작..."
    pm2 restart edgex-trader
else
    echo "  [WARN] 실행 중인 서비스를 감지하지 못함 — 수동 재시작 필요"
fi

# ── 5. 오래된 Docker 이미지 정리 ─────────────────────────────────────────────
echo "  오래된 이미지 정리..."
docker image prune -f --filter "label=app=edgex-trader" 2>/dev/null || true

echo "[$(date '+%Y-%m-%d %H:%M:%S')] 배포 완료: ${PREV_COMMIT} → ${NEW_COMMIT}"
