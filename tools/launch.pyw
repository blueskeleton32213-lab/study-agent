"""바탕화면 바로가기가 실행하는 파일. 콘솔 창 없이 서버를 켜고 앱 창을 연다."""
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

TOOLS = Path(__file__).resolve().parent
PORT = int(os.environ.get("STUDY_APP_PORT", "8765"))
URL = f"http://127.0.0.1:{PORT}/"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
DETACHED = getattr(subprocess, "DETACHED_PROCESS", 0)


def server_up() -> bool:
    with socket.socket() as s:
        s.settimeout(0.3)
        return s.connect_ex(("127.0.0.1", PORT)) == 0


def stale_server() -> bool:
    """떠 있는 서버가 예전 코드로 돌고 있으면 True (그 뒤에 tools/*.py가 수정됨)."""
    import json
    import urllib.request
    try:
        with urllib.request.urlopen(f"{URL}api/ping", timeout=2) as r:
            return bool(json.loads(r.read()).get("stale", True))  # stale 필드가 없으면 아주 예전 서버
    except Exception:
        return False


if server_up() and stale_server():
    import urllib.request
    try:
        urllib.request.urlopen(urllib.request.Request(f"{URL}api/shutdown", method="POST", data=b"",
                                                      headers={"X-Study-App": "1"}), timeout=3)
    except Exception:
        pass
    for _ in range(25):
        if not server_up():
            break
        time.sleep(0.2)
    if server_up():  # /api/shutdown이 없는 아주 예전 서버 → 프로세스 종료
        subprocess.run(["powershell", "-NoProfile", "-Command",
                        f"$p=(Get-NetTCPConnection -LocalPort {PORT} -State Listen -ErrorAction SilentlyContinue).OwningProcess;"
                        "if($p){Stop-Process -Id $p -Force}"], creationflags=NO_WINDOW)
        time.sleep(0.5)

if not server_up():
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    subprocess.Popen([str(pythonw if pythonw.exists() else sys.executable), str(TOOLS / "app.py")],
                     cwd=TOOLS.parent, creationflags=NO_WINDOW | DETACHED, close_fds=True)
    for _ in range(50):
        if server_up():
            break
        time.sleep(0.2)

edge_candidates = [
    Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "Microsoft/Edge/Application/msedge.exe",
    Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Microsoft/Edge/Application/msedge.exe",
]
edge = next((p for p in edge_candidates if p.exists()), None)
if edge:
    # 앱 모드: 주소창·탭 없는 독립 창. 별도 프로필이라 창 크기도 기억됨
    profile = Path(os.environ["LOCALAPPDATA"]) / "StudyTutorApp"
    subprocess.Popen([str(edge), f"--app={URL}", f"--user-data-dir={profile}",
                      "--window-size=1200,820", "--no-first-run"], creationflags=NO_WINDOW)
else:
    import webbrowser
    webbrowser.open(URL)
