// PM2 프로세스 관리 설정 (Docker 없이 직접 Python 실행 방식)
//
// 설치:  npm install -g pm2
// 시작:  pm2 start deploy/ecosystem.config.js --env production
// 조회:  pm2 status / pm2 logs edgex-trader
// 저장:  pm2 save && pm2 startup   ← 서버 재부팅 시 자동 시작 설정

module.exports = {
  apps: [
    // ── 실전 모드 ─────────────────────────────────────────────────────────
    {
      name: "edgex-trader",
      script: "main.py",
      interpreter: "/opt/edgex-trader/.venv/bin/python",
      args: "--log-level INFO",
      cwd: "/opt/edgex-trader",

      // 재시작 정책
      autorestart: true,
      watch: false,           // 코드 변경 감지 재시작 OFF (운영 중 불필요)
      max_restarts: 10,       // 연속 10회 실패 시 PM2가 포기
      restart_delay: 30000,   // 재시작 간격 30초 (ms)
      min_uptime: "60s",      // 60초 이상 살아있어야 정상 재시작으로 인정

      // 로그
      out_file: "/opt/edgex-trader/logs/trader-out.log",
      error_file: "/opt/edgex-trader/logs/trader-error.log",
      merge_logs: true,
      log_date_format: "YYYY-MM-DD HH:mm:ss Z",
      max_size: "50M",         // 로그 로테이션 (50MB)
      retain: 7,               // 7개 보관

      // 환경 변수
      env_production: {
        NODE_ENV: "production",
        // .env는 dotenv (python-dotenv)가 자동 로드
      },
    },

    // ── 드라이런 모드 (테스트용) ──────────────────────────────────────────
    {
      name: "edgex-trader-dryrun",
      script: "main.py",
      interpreter: "/opt/edgex-trader/.venv/bin/python",
      args: "--dry-run --log-level DEBUG",
      cwd: "/opt/edgex-trader",
      autorestart: false,     // 드라이런은 1회만 실행
      instances: 1,
      out_file: "/opt/edgex-trader/logs/dryrun-out.log",
      error_file: "/opt/edgex-trader/logs/dryrun-error.log",
      env_production: {
        NODE_ENV: "production",
      },
    },
  ],
};
