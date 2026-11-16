"""Stop 훅: 앱 코드·설정을 고쳤는데 작업 기록이 그보다 오래됐으면, 멈추기 전에 기록부터 하게 한다.

- 앱(학습 튜터 화면)이 띄운 Claude는 STUDY_APP=1 이라 건너뛴다 (공부 대화는 CLAUDE.md 규칙만 따름).
- 이미 한 번 막혀서 다시 멈추는 중이면(stop_hook_active) 그냥 통과 → 무한 반복 없음.
"""
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LOG = ROOT / "notes" / "학습 튜터 작업 기록.md"  # notes/는 OneDrive와 연결된 폴더 (git에는 안 올림)
WATCH_FILES = ["CLAUDE.md", "README.md", ".gitignore", "requirements.txt"]
WATCH_DIRS = ["tools", ".claude"]
SKIP_PARTS = {"__pycache__"}


def changed_files(since: float) -> list[str]:
    paths = [ROOT / f for f in WATCH_FILES]
    for d in WATCH_DIRS:
        paths += [p for p in (ROOT / d).rglob("*") if p.is_file() and not SKIP_PARTS & set(p.parts)]
    return [p.relative_to(ROOT).as_posix() for p in paths if p.exists() and p.stat().st_mtime > since + 1]


def main() -> None:
    try:
        data = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError:
        data = {}
    if os.environ.get("STUDY_APP") == "1" or data.get("stop_hook_active"):
        return
    since = LOG.stat().st_mtime if LOG.exists() else 0
    files = changed_files(since)
    if not files:
        return
    sys.stdout.reconfigure(encoding="utf-8")  # Windows 기본(cp949)이면 한글이 깨져서 전달된다
    shown = ", ".join(files[:8]) + (f" 외 {len(files) - 8}개" if len(files) > 8 else "")
    print(json.dumps({
        "decision": "block",
        "reason": (f"작업 기록보다 최근에 바뀐 파일이 있어요: {shown}. "
                   "CLAUDE.md의 '작업 기록' 규칙대로 이번 대화에서 한 일을 "
                   "'notes/학습 튜터 작업 기록.md'에 덧붙인 뒤, 한 줄로 알리고 마치세요."),
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
