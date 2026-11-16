@echo off
REM Interview AI Assistant launcher v0.6.0
for /f "usebackq tokens=1,2 delims==" %%a in ("%~dp0secrets.env") do (
  if "%%a"=="OPENROUTER_KEY" set "OPENROUTER_KEY=%%b"
  if "%%a"=="INTERVIEW_DEEP_TOKEN" set "INTERVIEW_DEEP_TOKEN=%%b"
  if "%%a"=="DEEPSEEK_API_KEY" set "DEEPSEEK_API_KEY=%%b"
  if "%%a"=="CURSOR_API_KEY" set "CURSOR_API_KEY=%%b"
)
set "HTTP_PROXY=http://127.0.0.1:10808"
set "HTTPS_PROXY=http://127.0.0.1:10808"
REM Прокси выше — дефолтные локальные порты v2rayN (10808 HTTP). Если прокси не нужен
REM или порт другой — измените эти две строки либо удалите их.
set PYTHONIOENCODING=utf-8
set THIRD_ENGINE=claude
REM Переходим в папку, где лежит этот .bat (а не в жёстко заданный путь)
cd /d "%~dp0"
echo Starting Interview Assistant v0.6.0...
python3.11 assistant.py
echo.
echo Process ended. Press any key to close.
pause > nul
