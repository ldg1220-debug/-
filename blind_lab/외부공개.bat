@echo off
chcp 65001 > nul
echo ========================================
echo  cloudflared 터널 (외부 접속용)
echo  먼저 시작.bat 으로 서버를 켜 둔 상태여야 합니다.
echo ========================================

where cloudflared > nul 2>&1
if errorlevel 1 (
    echo cloudflared 가 없어 설치합니다...
    winget install --id Cloudflare.cloudflared -e --accept-source-agreements --accept-package-agreements
    echo.
    echo 설치 완료. 이 창을 닫고 외부공개.bat 을 다시 실행하세요.
    pause
    exit /b 0
)

cloudflared tunnel --url http://localhost:8731
pause
