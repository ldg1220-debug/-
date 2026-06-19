#!/usr/bin/env bash
# =============================================================================
# edgeX Trader 헬스체크 스크립트
# cron 또는 systemd timer로 5분마다 실행하여 봇 상태를 모니터링합니다.
#
# cron 등록 예시 (5분마다):
#   crontab -e
#   */5 * * * * /opt/edgex-trader/deploy/healthcheck.sh >> /opt/edgex-trader/logs/healthcheck.log 2>&1
# =============================================================================
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/edgex-trader}"
LOG_FILE="${APP_DIR}/trading.log"
DB_FILE="${APP_DIR}/data/trading_state.db"
TELEGRAM_BOT_TOKEN="${TELEGRAM_BOT_TOKEN:-}"
TELEGRAM_CHAT_ID="${TELEGRAM_CHAT_ID:-}"
MAX_LOG_SILENCE_SEC="${MAX_LOG_SILENCE_SEC:-300}"   # 5분 이상 로그 없으면 경보
MAX_DB_AGE_SEC="${MAX_DB_AGE_SEC:-600}"             # 10분 이상 DB 미갱신 시 경보

TIMESTAMP=$(date '+%Y-%m-%d %H:%M:%S')
ALERT_MSG=""

# ── 텔레그램 알림 함수 ────────────────────────────────────────────────────────
send_telegram() {
    local msg="$1"
    if [[ -n "${TELEGRAM_BOT_TOKEN}" && -n "${TELEGRAM_CHAT_ID}" ]]; then
        curl -s -X POST \
            "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
            -d chat_id="${TELEGRAM_CHAT_ID}" \
            -d text="🚨 [edgeX Trader] ${msg}" \
            -d parse_mode="HTML" > /dev/null
    fi
}

# ── 1. 프로세스 생존 확인 ─────────────────────────────────────────────────────
check_process() {
    if docker ps --filter "name=edgex" --filter "status=running" | grep -q edgex 2>/dev/null; then
        echo "[${TIMESTAMP}] [OK] Docker 컨테이너 실행 중"
        return 0
    fi
    # PM2로 실행 중인지 확인
    if command -v pm2 &>/dev/null && pm2 list 2>/dev/null | grep -q "edgex-trader.*online"; then
        echo "[${TIMESTAMP}] [OK] PM2 프로세스 실행 중"
        return 0
    fi
    # 직접 python 프로세스 확인
    if pgrep -f "python.*main.py" > /dev/null 2>&1; then
        echo "[${TIMESTAMP}] [OK] Python 프로세스 실행 중"
        return 0
    fi
    ALERT_MSG="봇 프로세스가 감지되지 않습니다!"
    echo "[${TIMESTAMP}] [FAIL] ${ALERT_MSG}"
    return 1
}

# ── 2. 로그 활성도 확인 ───────────────────────────────────────────────────────
check_log_activity() {
    if [[ ! -f "${LOG_FILE}" ]]; then
        echo "[${TIMESTAMP}] [WARN] 로그 파일 없음: ${LOG_FILE}"
        return 0
    fi
    local now; now=$(date +%s)
    local mtime; mtime=$(stat -c %Y "${LOG_FILE}" 2>/dev/null || echo 0)
    local age=$(( now - mtime ))
    if (( age > MAX_LOG_SILENCE_SEC )); then
        ALERT_MSG="로그 파일이 ${age}초 동안 갱신되지 않았습니다."
        echo "[${TIMESTAMP}] [WARN] ${ALERT_MSG}"
        return 1
    fi
    echo "[${TIMESTAMP}] [OK] 로그 최근 갱신: ${age}초 전"
    return 0
}

# ── 3. DB 파일 상태 확인 ─────────────────────────────────────────────────────
check_db() {
    if [[ ! -f "${DB_FILE}" ]]; then
        echo "[${TIMESTAMP}] [WARN] DB 파일 없음: ${DB_FILE}"
        return 0
    fi
    local now; now=$(date +%s)
    local mtime; mtime=$(stat -c %Y "${DB_FILE}" 2>/dev/null || echo 0)
    local age=$(( now - mtime ))
    if (( age > MAX_DB_AGE_SEC )); then
        echo "[${TIMESTAMP}] [WARN] DB ${age}초 미갱신 (허용=${MAX_DB_AGE_SEC}초)"
    else
        echo "[${TIMESTAMP}] [OK] DB 최근 갱신: ${age}초 전"
    fi
    # DB 크기
    local size; size=$(du -sh "${DB_FILE}" | cut -f1)
    echo "[${TIMESTAMP}] [INFO] DB 크기: ${size}"
    return 0
}

# ── 4. 디스크 여유 공간 확인 ─────────────────────────────────────────────────
check_disk() {
    local usage; usage=$(df "${APP_DIR}" | tail -1 | awk '{print $5}' | tr -d '%')
    if (( usage > 85 )); then
        ALERT_MSG="디스크 사용률 ${usage}% 경고! 로그를 정리하세요."
        echo "[${TIMESTAMP}] [WARN] ${ALERT_MSG}"
        return 1
    fi
    echo "[${TIMESTAMP}] [OK] 디스크 사용률: ${usage}%"
    return 0
}

# ── 5. 최근 오류 로그 집계 ────────────────────────────────────────────────────
check_error_rate() {
    if [[ ! -f "${LOG_FILE}" ]]; then return 0; fi
    local errors; errors=$(tail -500 "${LOG_FILE}" 2>/dev/null | grep -c "\[ERROR\]" || true)
    if (( errors > 20 )); then
        echo "[${TIMESTAMP}] [WARN] 최근 500줄에서 ERROR ${errors}건 감지"
    else
        echo "[${TIMESTAMP}] [OK] 최근 ERROR 수: ${errors}건"
    fi
    return 0
}

# ── 실행 ─────────────────────────────────────────────────────────────────────
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "[${TIMESTAMP}] 헬스체크 시작"

FAILED=0
check_process  || FAILED=1
check_log_activity || true
check_db       || true
check_disk     || true
check_error_rate || true

if (( FAILED == 1 )) && [[ -n "${ALERT_MSG}" ]]; then
    send_telegram "${ALERT_MSG} (서버: $(hostname))"
    echo "[${TIMESTAMP}] 텔레그램 알림 전송: ${ALERT_MSG}"
fi

echo "[${TIMESTAMP}] 헬스체크 완료 (status=${FAILED})"
exit ${FAILED}
