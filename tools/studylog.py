"""약점 분석용 공부 기록 모으기: 질문하기 대화·질문 창 대화·복습 카드를 과목별로 짧게 정리해 출력한다.

    python tools/studylog.py <과목> [--max 150]

/weak 스킬이 이 출력을 보고 "어떤 개념"과 "어떤 유형"이 부족한지 판단한다.
앱(app.py)은 counts()로 기록이 있는지만 확인한다 (약점 분석 버튼).
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CHATS = ROOT / "data" / "chats"
ASKS = ROOT / "data" / "asks"
CARDS = ROOT / "data" / "cards.json"

# 앱 버튼이 대신 보낸 메시지(내가 직접 물은 게 아님)는 뺀다
APP_MSG = re.compile(r"^(「.*」에 새 자료를|약점 분석 \(|자료 요약 \(|틀린 주제 정리:|헷갈린 주제 정리)")


def _talks(base: Path, subject: str) -> list[dict]:
    out = []
    for f in base.glob("*.json") if base.exists() else []:
        try:
            c = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if c.get("subject") != subject:
            continue
        qs = [m for m in c.get("messages", []) if m.get("role") == "user" and not APP_MSG.match(m.get("text", ""))]
        if qs:
            label = (c.get("scope") or {}).get("label")  # 질문 창: 어느 퀴즈·정리 자료에서 물었는지
            out.append({"title": f"[{label}] {c.get('title', '')}" if label else c.get("title", ""),
                        "updated": c.get("updated") or "", "qs": qs})
    return sorted(out, key=lambda c: c["updated"])


def _cards(subject: str) -> list[dict]:
    try:
        cards = json.loads(CARDS.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return [c for c in cards if c.get("subject") == subject]


def _struggle(c: dict) -> int:
    """헷갈린 정도: 틀린 횟수 + 복습에서 '다시·어려움'을 누른 횟수.
    카드는 대부분 퀴즈에서 틀려 만들어져 lapses가 1부터라, 복습 평가가 있어야 구분된다"""
    return c.get("lapses", 0) + sum(q <= 3 for _, q in c.get("history", []))


def _missed(c: dict) -> bool:
    """복습에서 '다시·어려움'을 누른 적이 있거나 두 번 이상 틀린 카드"""
    return c.get("lapses", 0) >= 2 or any(q <= 3 for _, q in c.get("history", []))


def counts(subject: str) -> dict:
    cards = _cards(subject)
    return {"cards": len(cards), "missed": sum(_missed(c) for c in cards),
            "chats": sum(len(c["qs"]) for c in _talks(CHATS, subject)),
            "asks": sum(len(c["qs"]) for c in _talks(ASKS, subject))}


def _short(s: str, n: int) -> str:
    s = re.sub(r"\s+", " ", s or "").strip()
    return s if len(s) <= n else s[:n] + "…"


def render(subject: str, cap: int = 150) -> str:
    """약점 분석에 넘길 공부 기록 (앱은 이 글을 /weak 요청에 붙여 보낸다)"""
    lines: list[str] = []
    print = lines.append  # noqa: A001  아래 출력 코드를 그대로 글로 모은다

    n = counts(subject)
    print(f"# {subject} 공부 기록")
    print(f"복습 카드 {n['cards']}장 (복습에서 다시·어려움이 나온 카드 {n['missed']}장) · 질문하기 질문 {n['chats']}개 · 질문 창 질문 {n['asks']}개\n")
    print("## 복습 카드 (헷갈린 것 먼저. 카드는 대부분 퀴즈에서 틀려서 만든 것)")
    print("형식: [주제] 틀린 횟수 / 복습 평가(1 다시·3 어려움·4 좋음·5 쉬움) | 질문 | 출처")
    cards = sorted(_cards(subject), key=lambda c: (-_struggle(c), c.get("ease", 2.5)))
    for c in cards:
        grades = "".join(str(q) for _, q in c.get("history", [])) or "-"
        mark = "⚠ " if _missed(c) else ""
        print(f"- {mark}[{c.get('topic') or '주제 없음'}] 틀림 {c.get('lapses', 0)} / 평가 {grades} | "
              f"{_short(c.get('question', ''), 160)} | {c.get('source', '')}")
    if not cards:
        print("(없음)")

    # 최근 것 위주로 cap개까지 (대화별로 묶어서, 이어 물은 흐름이 보이게)
    for label, base, note in (("질문하기 대화", CHATS, "대화 제목 아래 내가 한 질문 순서대로"),
                              ("질문 창 대화", ASKS, "«드래그한 부분»(어디서) → 내가 한 질문")):
        print(f"\n## {label} ({note})")
        talks, left, shown = _talks(base, subject)[::-1], cap, []
        for t in talks:
            if left <= 0:
                break
            shown.append((t, t["qs"][-left:] if len(t["qs"]) > left else t["qs"]))
            left -= len(shown[-1][1])
        for t, qs in shown[::-1]:
            print(f"### {t['updated'][:10]} {_short(t['title'], 60)}")
            for m in qs:
                q = m.get("quote")
                pre = f"«{_short(q['text'], 120)}»({q.get('where', '')}) → " if q else ""
                print(f"- {pre}{_short(m['text'], 220)}")
        if not shown:
            print("(없음)")

    return "\n".join(lines)


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    args = sys.argv[1:]
    if not args:
        sys.exit("사용법: python tools/studylog.py <과목> [--max 150]")
    cap = int(args[args.index("--max") + 1]) if "--max" in args else 150
    print(render(args[0], cap))


if __name__ == "__main__":
    main()
