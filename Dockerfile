FROM python:3.11-slim

WORKDIR /app

# 시스템 의존성 (빌드 툴 + 헬스체크용 curl)
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc curl \
    && rm -rf /var/lib/apt/lists/*

# 의존성 먼저 설치 (캐시 레이어 활용)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 소스 복사
COPY . .

# 상태 DB 및 로그 볼륨 마운트 포인트
VOLUME ["/app/data", "/app/logs", "/app/config"]

ENV STATE_DB_PATH=/app/data/trading_state.db \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=UTC

# 전용 비root 계정
RUN useradd -m -u 1001 trader \
    && mkdir -p /app/data /app/logs /app/config \
    && chown -R trader:trader /app
USER trader

# 헬스체크: 매 60초마다 프로세스 생존 + trading.log 갱신 여부 확인
HEALTHCHECK --interval=60s --timeout=10s --start-period=30s --retries=3 \
    CMD python -c "
import os, time, sys
log = '/app/trading.log'
if os.path.exists(log):
    age = time.time() - os.path.getmtime(log)
    sys.exit(0 if age < 300 else 1)
sys.exit(0)
"

ENTRYPOINT ["python", "main.py"]
CMD ["--dry-run"]
