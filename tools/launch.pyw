"""바탕화면 바로가기가 실행하는 파일. 콘솔 창 없이 서버를 켜고 앱 창(pywebview)을 연다."""
import ctypes
import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

FROZEN = getattr(sys, "frozen", False)  # PyInstaller exe면 True (exe는 프로젝트 폴더 맨 위에 둔다)
ROOT = Path(sys.executable).resolve().parent if FROZEN else Path(__file__).resolve().parent.parent
TOOLS = ROOT / "tools"
PORT = int(os.environ.get("STUDY_APP_PORT", "8765"))
URL = f"http://127.0.0.1:{PORT}/"
TITLE = "학습 튜터"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
DETACHED = getattr(subprocess, "DETACHED_PROCESS", 0)
WINDOW_FILE = ROOT / "local" / "window.json"  # 창 크기·위치는 화면마다 달라서 컴퓨터마다 따로


def git(*args: str, timeout: int = 15) -> subprocess.CompletedProcess | None:
    import shutil
    exe = shutil.which("git")
    if not exe or not (ROOT / ".git").exists():
        return None
    try:
        return subprocess.run([exe, *args], cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=timeout, creationflags=NO_WINDOW)
    except Exception:
        return None


def sync_pull() -> None:
    """다른 컴퓨터에서 고친 코드를 받아온다. 고치다 만 파일이 있거나 갈라졌으면 건드리지 않는다."""
    st = git("status", "--porcelain", "--untracked-files=no")
    if st is None or st.returncode or st.stdout.strip():
        return
    git("pull", "--ff-only", "--quiet", timeout=20)


def sync_push() -> None:
    """이 컴퓨터에서 커밋한 코드를 올려 둔다 (커밋 안 한 변경은 올리지 않음)."""
    ahead = git("rev-list", "--count", "@{u}..HEAD")
    if ahead and ahead.returncode == 0 and ahead.stdout.strip() not in ("", "0"):
        git("push", "--quiet", timeout=30)


def sync_onedrive() -> None:
    """개인 자료 폴더를 OneDrive와 맞춘다 (켤 때 받고, 끌 때 올림). 실패해도 앱은 그대로 쓴다."""
    try:
        sys.path.insert(0, str(TOOLS))
        import onedrive_sync
        onedrive_sync.main()
    except Exception:
        pass

user32 = ctypes.windll.user32
user32.FindWindowW.restype = ctypes.c_void_p
user32.LoadImageW.restype = ctypes.c_void_p
user32.LoadImageW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_uint, ctypes.c_int, ctypes.c_int, ctypes.c_uint]
user32.SendMessageW.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t, ctypes.c_void_p]
user32.ShowWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]
user32.SetForegroundWindow.argtypes = [ctypes.c_void_p]
# 이미 창이 떠 있으면 새 창 대신 그 창을 앞으로
_mutex = ctypes.windll.kernel32.CreateMutexW(None, False, "StudyTutorAppWindow")
if ctypes.windll.kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
    hwnd = user32.FindWindowW(None, TITLE)
    if hwnd:
        user32.ShowWindow(hwnd, 9)  # SW_RESTORE
        user32.SetForegroundWindow(hwnd)
    sys.exit()


def server_up() -> bool:
    with socket.socket() as s:
        s.settimeout(0.3)
        return s.connect_ex(("127.0.0.1", PORT)) == 0


def stale_server() -> bool:
    """떠 있는 서버가 예전 코드로 돌고 있으면 True (그 뒤에 tools/*.py가 수정됨)."""
    try:
        with urllib.request.urlopen(f"{URL}api/ping", timeout=2) as r:
            return bool(json.loads(r.read()).get("stale", True))  # stale 필드가 없으면 아주 예전 서버
    except Exception:
        return False


def shutdown_server() -> None:
    try:
        urllib.request.urlopen(urllib.request.Request(f"{URL}api/shutdown", method="POST", data=b"",
                                                      headers={"X-Study-App": "1"}), timeout=3)
    except Exception:
        pass


sync_pull()  # 받은 코드가 있으면 아래 stale 검사가 예전 서버를 내리고 새로 켠다

if server_up() and stale_server():
    shutdown_server()
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
    sync_onedrive()  # 다른 컴퓨터에서 바뀐 공부 기록·자료 받기 (서버가 파일을 쓰기 전에)
    if FROZEN:  # 서버·도구는 설치된 Python으로 돌린다 (tools/ 코드를 고쳐도 exe를 다시 만들 필요 없음)
        import shutil
        python = shutil.which("pythonw") or shutil.which("python")
        if not python:
            user32.MessageBoxW(None, "Python을 찾을 수 없어요. Python 3.12 이상을 설치하고 PATH에 추가해 주세요.", TITLE, 0x10)
            sys.exit(1)
    else:
        pythonw = Path(sys.executable).with_name("pythonw.exe")
        python = str(pythonw if pythonw.exists() else sys.executable)
    subprocess.Popen([python, str(TOOLS / "app.py")],
                     cwd=TOOLS.parent, creationflags=NO_WINDOW | DETACHED, close_fds=True)
    for _ in range(50):
        if server_up():
            break
        time.sleep(0.2)

import webview  # noqa: E402

# 작업표시줄에서 python이 아니라 별도 앱으로 묶이게
ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("StudyTutor.App")

try:
    geo = json.loads(WINDOW_FILE.read_text(encoding="utf-8"))
except Exception:
    geo = {}

class Api:
    """화면(index.html)에서 window.pywebview.api.* 로 부르는 함수들"""

    def toggle_fullscreen(self) -> bool:
        state["fullscreen"] = not state["fullscreen"]
        window.toggle_fullscreen()
        return state["fullscreen"]


window = webview.create_window(TITLE, URL, js_api=Api(), width=geo.get("width", 1200), height=geo.get("height", 820),
                               x=geo.get("x"), y=geo.get("y"), maximized=geo.get("maximized", False),
                               min_size=(720, 480), text_select=True, zoomable=True, background_color="#1b1b1f")
state = {"maximized": geo.get("maximized", False), "fullscreen": False}
window.events.maximized += lambda: state.update(maximized=True)
window.events.restored += lambda: state.update(maximized=False)


def set_icon() -> None:
    # WinForms 창은 아이콘 인자를 안 받아서 직접 WM_SETICON
    ico = TOOLS / "static" / "app.ico"
    hwnd = user32.FindWindowW(None, TITLE)
    if not (hwnd and ico.exists()):
        return
    for size, kind in ((16, 0), (32, 1)):  # ICON_SMALL, ICON_BIG
        h = user32.LoadImageW(None, str(ico), 1, size, size, 0x10)  # IMAGE_ICON, LR_LOADFROMFILE
        if h:
            user32.SendMessageW(hwnd, 0x80, kind, h)


def on_closing() -> None:
    if not (state["maximized"] or state["fullscreen"]):
        geo.update(width=window.width, height=window.height, x=window.x, y=window.y)
    geo["maximized"] = state["maximized"]
    try:
        WINDOW_FILE.write_text(json.dumps(geo), encoding="utf-8")
    except Exception:
        pass


window.events.shown += set_icon
window.events.closing += on_closing
# private_mode=False + storage_path: 테마 등 localStorage 설정이 다음 실행에도 남게
webview.start(private_mode=False, storage_path=str(Path(os.environ["LOCALAPPDATA"]) / "StudyTutorApp" / "webview"))
shutdown_server()  # 창을 닫으면 서버(와 돌던 Claude)도 같이 끈다
for _ in range(25):  # 서버가 파일을 다 쓰고 내려간 뒤에 올린다
    if not server_up():
        break
    time.sleep(0.2)
sync_onedrive()  # 이 컴퓨터에서 바뀐 공부 기록·자료를 OneDrive에 올림
sync_push()
