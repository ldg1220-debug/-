@echo off
chcp 65001 > nul
echo ========================================
echo  새 차트 셋 다운로드 및 케이스 생성
echo ========================================

cd /d "%~dp0"

echo [1/3] 기존 decisions.json 백업 중...
if exist "decisions.json" (
    for /f "tokens=1-4 delims=/ " %%a in ('date /t') do set d=%%a%%b%%c
    for /f "tokens=1-2 delims=: " %%a in ('time /t') do set t=%%a%%b
    copy decisions.json "decisions_backup_%d%_%t%.json" > nul
    echo     -> decisions_backup_%d%_%t%.json 저장됨
)

echo [2/3] 시세 데이터 다운로드 중...
node fetch_data.mjs
if errorlevel 1 ( echo 오류 발생. 종료합니다. & pause & exit /b 1 )

echo [3/3] 케이스 60개 생성 중...
node prepare_cases.mjs
if errorlevel 1 ( echo 오류 발생. 종료합니다. & pause & exit /b 1 )

echo decisions.json 초기화 중...
node -e "require('fs').writeFileSync('decisions.json','[]')"

echo.
echo 완료! 이제 시작.bat 을 실행하세요.
pause
