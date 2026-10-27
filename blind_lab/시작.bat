@echo off
chcp 65001 > nul
echo ========================================
echo  블라인드 차트 라벨링 서버 시작
echo ========================================

cd /d "%~dp0"

if not exist "raw_data" (
    echo [1/3] 처음 실행 - 시세 데이터 다운로드 중...
    node fetch_data.mjs
    if errorlevel 1 ( echo 오류 발생. 종료합니다. & pause & exit /b 1 )
)

if not exist "cases.json" (
    echo [2/3] 케이스 생성 중...
    node prepare_cases.mjs
    if errorlevel 1 ( echo 오류 발생. 종료합니다. & pause & exit /b 1 )
)

if not exist "decisions.json" (
    node -e "require('fs').writeFileSync('decisions.json','[]')"
)

echo [3/3] 서버 시작 중... http://localhost:8731
echo 종료하려면 Ctrl+C 를 누르세요.
node server.mjs
pause
