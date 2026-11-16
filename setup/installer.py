"""학습 튜터 설치 마법사 (PyInstaller로 '학습 튜터 설치.exe'를 만든다).

같이 묶인 setup.ps1을 -Wizard 로 돌리면서 진행 단계·기록을 창에 보여 준다.
빌드:
  python -m PyInstaller --noconfirm --onefile --noconsole --name "학습 튜터 설치" ^
    --icon tools\\static\\app.ico --add-data "setup\\setup.ps1;." --add-data "tools\\static\\app.ico;." ^
    --distpath setup\\dist setup\\installer.py
"""
from __future__ import annotations

import os
import queue
import subprocess
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import ttk

HERE = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
SCRIPT = HERE / "setup.ps1"
ICON = HERE / "app.ico" if (HERE / "app.ico").exists() else HERE.parent / "tools" / "static" / "app.ico"
STEPS = 6  # setup.ps1의 "▶" 단계 수
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
ONEDRIVE = Path(os.environ.get("OneDrive") or Path.home() / "OneDrive") / "학습 튜터 자료"

BG, FG, SUB, ACCENT = "#1b1b1f", "#ececf1", "#9a9aa6", "#7c8cff"


class Wizard:
    def __init__(self) -> None:
        self.root = tk.Tk()
        self.root.title("학습 튜터 설치")
        self.root.geometry("560x460")
        self.root.minsize(480, 400)
        self.root.configure(bg=BG)
        try:
            self.root.iconbitmap(str(ICON))
        except tk.TclError:
            pass
        self.q: queue.Queue[tuple[str, str]] = queue.Queue()
        self.step = 0
        self.launch: tuple[str, str] | None = None
        self.running = False

        font = ("Malgun Gothic", 10)
        tk.Label(self.root, text="학습 튜터 설치", bg=BG, fg=FG, font=("Malgun Gothic", 16, "bold")).pack(anchor="w", padx=20, pady=(18, 4))
        info = ("이 컴퓨터에 학습 튜터를 설치해요.\n"
                "Python·Git이 없으면 같이 설치해요.")
        if ONEDRIVE.exists():
            info += "\n\nOneDrive의 '학습 튜터 자료'에서 공부 기록·자료를 받아 이어서 써요."
        else:
            info += ("\n\n처음 쓰는 거면 그대로 설치하세요. 자료는 이 컴퓨터에 새로 만들어요.\n"
                     "다른 컴퓨터의 기록을 이어 쓰려면 OneDrive 동기화가 끝난 뒤 설치하세요.")
        self.info = tk.Label(self.root, text=info, bg=BG, fg=SUB, font=font, justify="left")
        self.info.pack(anchor="w", padx=20)

        style = ttk.Style()
        style.theme_use("clam")
        style.configure("A.Horizontal.TProgressbar", troughcolor="#2a2a31", background=ACCENT, bordercolor=BG,
                        lightcolor=ACCENT, darkcolor=ACCENT)
        self.status = tk.Label(self.root, text="", bg=BG, fg=FG, font=font)
        self.status.pack(anchor="w", padx=20, pady=(14, 4))
        self.bar = ttk.Progressbar(self.root, style="A.Horizontal.TProgressbar", maximum=STEPS, mode="determinate")
        self.bar.pack(fill="x", padx=20)

        frame = tk.Frame(self.root, bg=BG)
        frame.pack(fill="both", expand=True, padx=20, pady=12)
        self.log = tk.Text(frame, bg="#121216", fg=SUB, font=("Consolas", 9), relief="flat", wrap="word",
                           state="disabled", height=10, highlightthickness=0, padx=8, pady=6)
        self.log.pack(fill="both", expand=True)
        self.log.tag_configure("step", foreground=FG)
        self.log.tag_configure("err", foreground="#ff6b6b")

        btns = tk.Frame(self.root, bg=BG)
        btns.pack(fill="x", padx=20, pady=(0, 16))
        self.close_btn = tk.Button(btns, text="닫기", command=self.close, bg="#2a2a31", fg=FG, relief="flat",
                                   activebackground="#34343c", activeforeground=FG, font=font, padx=16, pady=4)
        self.close_btn.pack(side="right")
        self.main_btn = tk.Button(btns, text="설치", command=self.start, bg=ACCENT, fg="white",
                                  relief="flat", activebackground="#6a7af0", activeforeground="white",
                                  font=("Malgun Gothic", 10, "bold"), padx=18, pady=4)
        self.main_btn.pack(side="right", padx=(0, 8))
        self.root.protocol("WM_DELETE_WINDOW", self.close)

    def write(self, text: str, tag: str = "") -> None:
        self.log.configure(state="normal")
        self.log.insert("end", text + "\n", tag)
        self.log.see("end")
        self.log.configure(state="disabled")

    def start(self) -> None:
        if self.launch:  # 설치가 끝난 뒤 버튼은 "학습 튜터 실행"
            pythonw, folder = self.launch
            subprocess.Popen([pythonw, str(Path(folder) / "tools" / "launch.pyw")], cwd=folder,
                             creationflags=NO_WINDOW, close_fds=True)
            self.root.after(500, self.root.destroy)
            return
        self.running = True
        self.step = 0
        self.bar["value"] = 0
        self.main_btn.configure(state="disabled", text="설치 중...")
        self.status.configure(text="준비 중...")
        threading.Thread(target=self.run, daemon=True).start()
        self.root.after(100, self.poll)

    def run(self) -> None:
        cmd = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(SCRIPT), "-Wizard"]
        try:
            p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                 creationflags=NO_WINDOW)
        except OSError as e:
            self.q.put(("done", f"PowerShell을 실행하지 못했어요: {e}"))
            return
        buf = b""
        while chunk := p.stdout.read1(4096) if hasattr(p.stdout, "read1") else p.stdout.read(1):
            buf += chunk
            *lines, buf = buf.replace(b"\r\n", b"\n").replace(b"\r", b"\n").split(b"\n")
            for raw in lines:
                self.q.put(("line", self.decode(raw)))
        if buf:
            self.q.put(("line", self.decode(buf)))
        self.q.put(("done", "" if p.wait() == 0 else "fail"))

    @staticmethod
    def decode(raw: bytes) -> str:
        try:
            return raw.decode("utf-8").rstrip()
        except UnicodeDecodeError:
            return raw.decode("cp949", errors="replace").rstrip()

    def poll(self) -> None:
        try:
            while True:
                kind, text = self.q.get_nowait()
                if kind == "line":
                    self.handle(text)
                else:
                    self.finish(text)
                    return
        except queue.Empty:
            pass
        self.root.after(100, self.poll)

    def handle(self, line: str) -> None:
        s = line.strip()
        if not s or set(s) <= set("-\\|/ █▒░") or "%" in s and ("█" in s or "▒" in s):
            return  # winget·pip 진행 막대
        if s.startswith("LAUNCH|"):
            _, pythonw, folder = s.split("|", 2)
            self.launch = (pythonw, folder)
            return
        if s.startswith("▶"):
            self.step = min(self.step + 1, STEPS)
            self.bar["value"] = self.step - 1
            self.status.configure(text=f"{self.step}/{STEPS}  {s[1:].strip()}")
            self.write(s, "step")
        elif s.startswith("✖"):
            self.write(s, "err")
        else:
            self.write("  " + s if not line.startswith(" ") else line)

    def finish(self, err: str) -> None:
        self.running = False
        if err or not self.launch:
            if err and err != "fail":
                self.write(err, "err")
            self.status.configure(text="설치하지 못했어요. 위 기록을 확인하고 다시 눌러 보세요.", fg="#ff6b6b")
            self.main_btn.configure(state="normal", text="다시 시도")
            return
        self.bar["value"] = STEPS
        self.status.configure(text="설치 완료! 바탕화면의 '학습 튜터'로 언제든 실행할 수 있어요.", fg=FG)
        self.main_btn.configure(state="normal", text="학습 튜터 실행")
        self.close_btn.configure(text="닫기")

    def close(self) -> None:
        if self.running:
            self.write("설치가 진행 중이에요. 끝날 때까지 기다려 주세요.", "err")
            return
        self.root.destroy()


if __name__ == "__main__":
    Wizard().root.mainloop()
