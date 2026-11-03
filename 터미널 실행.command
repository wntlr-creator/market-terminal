#!/bin/bash
cd "$(dirname "$0")"
echo "YH 마켓 터미널을 준비하고 있어요..."
command -v python3 >/dev/null || { echo "python3가 없어요. https://www.python.org/downloads/ 에서 설치해 주세요."; read; exit 1; }
python3 -m pip install -q -r requirements.txt
export SEC_USER_AGENT="${SEC_USER_AGENT:-YH Market Terminal personal-use}"
python3 serve.py
