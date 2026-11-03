"""
내 컴퓨터에서 실행:  python serve.py   (또는 '터미널 실행.bat' 더블클릭)
서버(app.py)를 켜고 브라우저로 http://localhost:8000 을 열어요.
- 시세·뉴스 10분마다, 포트폴리오 6시간마다 자동 갱신
- 검색창에서 아무 종목이나 고르면 바로 불러와요
- 이 창을 닫으면 터미널도 꺼져요
"""
import threading
import time
import webbrowser

import uvicorn

PORT = 8000

if __name__ == "__main__":
    url = f"http://localhost:{PORT}"
    print(f"\nYH 마켓 터미널 실행 중 → {url}\n처음 시세를 받는 데 1분 정도 걸릴 수 있어요. 이 창을 닫으면 꺼져요. (종료: Ctrl+C)\n", flush=True)
    threading.Thread(target=lambda: (time.sleep(2), webbrowser.open(url)), daemon=True).start()
    uvicorn.run("app:app", host="127.0.0.1", port=PORT, log_level="warning")
