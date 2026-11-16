"""개인 자료 폴더(materials·texts·studio·data·cache·notes)를 OneDrive의 "학습 튜터 자료"와 맞춘다.

앱은 항상 이 컴퓨터(로컬) 폴더를 읽고 쓴다. 이 스크립트는 앱을 켤 때(다른 컴퓨터의 변경 받기)와
끌 때(이 컴퓨터의 변경 올리기) launch.pyw가 부른다. 터미널에서 직접 돌려도 된다:
    python tools/onedrive_sync.py

- 마지막으로 맞춘 상태(local/onedrive_sync.json)와 비교해서 어느 쪽에서 바뀌었는지 판단한다 (3방향).
  한쪽만 바뀌었으면 그쪽을 다른 쪽에 반영(새 파일·수정·삭제 모두).
- 양쪽 다 바뀌었으면 수정 시각이 늦은 쪽을 남기고, 진 쪽은 local/sync_backup/<시각>/ 에 보관한다.
- 다른 컴퓨터에서 지워져서 이 컴퓨터에서 지우는 파일도 local/sync_backup/ 으로 옮겨 둔다.
- 예전 방식(폴더가 OneDrive로 가는 정션)이면 정션을 풀고 OneDrive 내용을 로컬로 복사한다 (OneDrive 쪽은 그대로).
- cache/hd, cache/pages (원본 쪽 이미지)는 다시 만들 수 있어서 동기화하지 않는다.
- OneDrive에 "학습 튜터 자료" 폴더가 없으면(처음 쓰는 사람) 로컬 폴더만 만들고 넘어간다.
  그 폴더를 OneDrive에 만들어 두면 다음 동기화 때 이 컴퓨터 자료가 전부 올라간다.
- 동기화 켜기/끄기는 컴퓨터마다 따로 (local/sync_settings.json, 앱 사이드바의 「OneDrive 동기화」).
  꺼져 있으면 아무것도 주고받지 않는다. 아직 고른 적이 없으면 OneDrive 폴더가 있을 때 켜진 것으로 본다.
    python tools/onedrive_sync.py on | off | status
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REMOTE = Path(os.environ.get("OneDrive") or Path.home() / "OneDrive") / "학습 튜터 자료"
FOLDERS = ("materials", "texts", "studio", "data", "cache", "notes")
SKIP_DIRS = {"cache/hd", "cache/pages"}
STATE = ROOT / "local" / "onedrive_sync.json"
BACKUP = ROOT / "local" / "sync_backup"
LOG = ROOT / "local" / "onedrive_sync.log"
SETTINGS = ROOT / "local" / "sync_settings.json"
DIR = (-1, 0)  # 폴더는 '있다/없다'만 본다
TMP_PREFIX = ".~sync-"

Stat = tuple[int, int]  # (크기, 수정 시각 초)
last_message = ""  # 마지막 로그 한 줄 (앱이 결과를 보여 줄 때 쓴다)


_received_cache: tuple[float, dict[str, Stat]] = (-1.0, {})


def received_files() -> dict[str, Stat]:
    """OneDrive에서 받아 온 뒤 이 컴퓨터에서 바뀌지 않은 파일들 {상대경로: (크기, 수정 시각)}.
    받아 오면 파일을 만든 시각(ctime)이 지금으로 바뀌어서, 앱이 '탐색기로 새로 넣은 자료'로 착각하지 않게 쓴다."""
    global _received_cache
    try:
        mtime = STATE.stat().st_mtime
        if mtime != _received_cache[0]:
            data = json.loads(STATE.read_text(encoding="utf-8")).get("received", {})
            _received_cache = (mtime, {k: tuple(v) for k, v in data.items()})
        return _received_cache[1]
    except Exception:
        return {}


def enabled() -> bool:
    """이 컴퓨터에서 OneDrive 동기화를 켰는지. 고른 적이 없으면 OneDrive 폴더가 있을 때 켜짐."""
    try:
        return bool(json.loads(SETTINGS.read_text(encoding="utf-8"))["enabled"])
    except Exception:
        return REMOTE.is_dir()


def set_enabled(on: bool) -> None:
    SETTINGS.parent.mkdir(parents=True, exist_ok=True)
    SETTINGS.write_text(json.dumps({"enabled": bool(on)}), encoding="utf-8")


def skip_file(name: str) -> bool:
    low = name.lower()
    return name.startswith(("~$", TMP_PREFIX)) or low.endswith(".tmp") or low == "desktop.ini"


def scan(base: Path) -> dict[str, Stat]:
    out: dict[str, Stat] = {}
    for top in FOLDERS:
        start = base / top
        if not start.is_dir():
            continue
        out[top] = DIR
        for dirpath, dirnames, filenames in os.walk(start):
            d = Path(dirpath)
            reld = d.relative_to(base).as_posix()
            dirnames[:] = [n for n in dirnames if f"{reld}/{n}" not in SKIP_DIRS and n != "__pycache__"]
            for n in dirnames:
                out[f"{reld}/{n}"] = DIR
            for n in filenames:
                if skip_file(n):
                    continue
                try:
                    st = (d / n).stat()
                except OSError:
                    continue
                out[f"{reld}/{n}"] = (st.st_size, int(st.st_mtime))
    return out


def same(a: Stat | None, b: Stat | None) -> bool:
    if a is None or b is None:
        return a is b
    if a == DIR or b == DIR:
        return a == b
    return a[0] == b[0] and abs(a[1] - b[1]) <= 2  # OneDrive를 거치면 시각이 1~2초 어긋날 수 있다


def stat_of(p: Path) -> Stat:
    if p.is_dir():
        return DIR
    st = p.stat()
    return (st.st_size, int(st.st_mtime))


def copy(src: Path, dst: Path) -> Stat:
    if src.is_dir():
        dst.mkdir(parents=True, exist_ok=True)
        return DIR
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(TMP_PREFIX + dst.name)
    shutil.copy2(src, tmp)  # 수정 시각도 그대로
    os.replace(tmp, dst)    # 중간에 끊겨도 반쯤 쓴 파일이 남지 않게
    return stat_of(dst)


class Sync:
    def __init__(self, log) -> None:
        self.log = log
        self.stamp = time.strftime("%Y%m%d-%H%M%S")
        self.received: dict[str, Stat] = {}  # OneDrive에서 받은 파일 (앱이 '새로 넣은 자료'로 보고 다시 추출하지 않게)
        self.counts = {"올림": 0, "받음": 0, "지움(OneDrive)": 0, "지움(이 컴퓨터)": 0, "충돌": 0, "실패": 0}

    def receive(self, rel: str) -> Stat:
        st = copy(REMOTE / rel, ROOT / rel)
        if st != DIR:
            self.received[rel] = st
        return st

    def backup(self, rel: str, src: Path, move: bool) -> None:
        dst = BACKUP / self.stamp / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        if move:
            shutil.move(src, dst)
        else:
            shutil.copy2(src, dst)

    def remove(self, side: Path, rel: str, dirs: list[tuple[Path, str]]) -> None:
        p = side / rel
        if p.is_dir():
            dirs.append((p, rel))  # 폴더는 안의 파일을 다 정리한 뒤에 (비었을 때만) 지운다
        elif p.exists():
            if side == ROOT:
                self.backup(rel, p, move=True)
            else:
                p.unlink()  # OneDrive에서 지운 파일은 OneDrive 휴지통에서 되살릴 수 있다

    def run(self) -> int:
        try:
            saved = json.loads(STATE.read_text(encoding="utf-8"))
            base: dict[str, Stat] = {k: tuple(v) for k, v in saved["files"].items()}
            received_before = {k: tuple(v) for k, v in saved.get("received", {}).items()}
        except Exception:
            base, received_before = {}, {}
        local, remote = scan(ROOT), scan(REMOTE)
        new: dict[str, Stat] = {}
        dirs_local: list[tuple[Path, str]] = []
        dirs_remote: list[tuple[Path, str]] = []
        for rel in sorted(set(local) | set(remote) | set(base), key=lambda p: (p.count("/"), p)):
            l, r, b = local.get(rel), remote.get(rel), base.get(rel)
            cl, cr = not same(l, b), not same(r, b)
            try:
                if not cl and not cr:
                    res = l
                elif cl and not cr:  # 이 컴퓨터에서만 바뀜 → OneDrive에 반영
                    if l is None:
                        self.remove(REMOTE, rel, dirs_remote); res = None
                        self.counts["지움(OneDrive)"] += r != DIR
                    else:
                        res = copy(ROOT / rel, REMOTE / rel); self.counts["올림"] += l != DIR
                elif cr and not cl:  # 다른 컴퓨터(OneDrive)에서만 바뀜 → 이 컴퓨터에 반영
                    if r is None:
                        self.remove(ROOT, rel, dirs_local); res = None
                        self.counts["지움(이 컴퓨터)"] += l != DIR
                    else:
                        res = self.receive(rel); self.counts["받음"] += r != DIR
                elif same(l, r):
                    res = l
                elif l is None:
                    res = self.receive(rel); self.counts["받음"] += 1
                elif r is None:
                    res = copy(ROOT / rel, REMOTE / rel); self.counts["올림"] += 1
                elif DIR in (l, r):
                    self.log(f"[건너뜀] 한쪽은 폴더, 한쪽은 파일: {rel}"); res = b
                else:  # 양쪽 다 고침 → 늦게 고친 쪽을 남기고 진 쪽은 보관
                    self.counts["충돌"] += 1
                    if l[1] >= r[1]:
                        self.backup(rel, REMOTE / rel, move=False)
                        res = copy(ROOT / rel, REMOTE / rel)
                        self.log(f"[충돌] {rel}: 이 컴퓨터 것을 남김 (OneDrive 것은 sync_backup/{self.stamp})")
                    else:
                        self.backup(rel, ROOT / rel, move=False)
                        res = self.receive(rel)
                        self.log(f"[충돌] {rel}: OneDrive 것을 남김 (이 컴퓨터 것은 sync_backup/{self.stamp})")
            except OSError as exc:  # 열려 있거나 OneDrive가 잡고 있는 파일 → 다음에 다시
                self.counts["실패"] += 1
                self.log(f"[실패] {rel}: {exc}")
                res = b
            if res is not None:
                new[rel] = res
        for side, dirs in ((ROOT, dirs_local), (REMOTE, dirs_remote)):
            for p, rel in sorted(dirs, key=lambda x: -x[1].count("/")):
                try:
                    p.rmdir()
                except OSError:  # 안에 새 파일이 생긴 폴더는 남긴다
                    if p.is_dir() and (ROOT / rel).is_dir() and (REMOTE / rel).is_dir():
                        new[rel] = DIR
        STATE.parent.mkdir(parents=True, exist_ok=True)
        tmp = STATE.with_suffix(".tmp")
        # 받은 파일 기록: 그 뒤로 이 컴퓨터에서 바뀌지 않은 것만 남긴다
        received = {k: v for k, v in received_before.items() if k in new and same(new[k], v)} | self.received
        tmp.write_text(json.dumps({"when": self.stamp, "files": new, "received": received}, ensure_ascii=False),
                       encoding="utf-8")
        os.replace(tmp, STATE)
        done = ", ".join(f"{k} {v}" for k, v in self.counts.items() if v)
        self.log(f"동기화 끝: {done or '바뀐 것 없음'}")
        return 1 if self.counts["실패"] else 0


def migrate(log) -> None:
    """예전 방식(OneDrive로 가는 정션)이면 정션을 풀고 OneDrive 내용을 로컬 폴더로 복사한다."""
    for top in FOLDERS:
        here = ROOT / top
        if not os.path.isjunction(here):
            continue
        os.rmdir(here)  # 정션(바로가기)만 지운다. OneDrive 쪽 파일은 그대로
        src = REMOTE / top
        if src.is_dir():
            shutil.copytree(src, here, ignore=lambda d, names: [
                n for n in names if skip_file(n) or n == "__pycache__"
                or f"{Path(d, n).relative_to(REMOTE).as_posix()}" in SKIP_DIRS])
        else:
            here.mkdir()
        log(f"{top}: OneDrive 정션 → 로컬 폴더로 바꿈")


def main() -> int:
    LOG.parent.mkdir(parents=True, exist_ok=True)
    out = sys.stdout

    def log(msg: str) -> None:
        global last_message
        last_message = msg
        line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}"
        if out:
            try:
                print(msg, file=out)
            except Exception:
                pass
        with LOG.open("a", encoding="utf-8") as f:
            f.write(line + "\n")

    if out:
        try:
            out.reconfigure(encoding="utf-8")
        except Exception:
            pass
    if not REMOTE.is_dir():
        # 처음 쓰는 사람·OneDrive를 안 쓰는 컴퓨터: 이 컴퓨터에만 저장하고 그대로 쓴다.
        # 나중에 OneDrive에 '학습 튜터 자료' 폴더를 만들면 다음 동기화부터 거기로 올라간다.
        for top in FOLDERS:
            if not os.path.isjunction(ROOT / top):
                (ROOT / top).mkdir(exist_ok=True)
        log(f"OneDrive에 '학습 튜터 자료' 폴더가 없어서 이 컴퓨터에만 저장해요 (동기화 안 함): {REMOTE}")
        return 0
    migrate(log)  # 켜짐·꺼짐과 상관없이: 정션이면 앱이 OneDrive에 바로 쓰게 되므로 로컬 폴더로 바꾼다
    if not enabled():
        log("OneDrive 동기화가 꺼져 있어서 이 컴퓨터에만 저장해요 (앱 사이드바 「OneDrive 동기화」로 켜기)")
        return 0
    return Sync(log).run()


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd in ("on", "off"):
        set_enabled(cmd == "on")
        print(f"OneDrive 동기화를 {'켰어요' if cmd == 'on' else '껐어요'}")
        if cmd == "off":
            sys.exit(0)
    elif cmd == "status":
        print(f"OneDrive 동기화: {'켜짐' if enabled() else '꺼짐'} ({REMOTE}{'' if REMOTE.is_dir() else ' 없음'})")
        sys.exit(0)
    sys.exit(main())
