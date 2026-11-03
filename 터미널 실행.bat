@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo YH 마켓 터미널을 준비하고 있어요...
where python >nul 2>nul
if errorlevel 1 (
  echo.
  echo 파이썬이 설치되어 있지 않아요. https://www.python.org/downloads/ 에서 설치할 때
  echo "Add python.exe to PATH" 를 꼭 체크한 뒤 다시 실행해 주세요.
  pause
  exit /b 1
)
python -m pip install -q -r requirements.txt
if not defined SEC_USER_AGENT set SEC_USER_AGENT=YH Market Terminal personal-use
python serve.py
pause
