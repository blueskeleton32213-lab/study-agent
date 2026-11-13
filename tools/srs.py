"""복습 카드 관리 + 간격반복(SM-2) 일정 계산.

일정 계산은 모델이 아니라 이 스크립트가 한다 (매번 같은 결과가 나오도록).
데이터는 data/cards.json 하나에 저장된다.

명령:
    add --subject S --topic T --question Q --answer A --source "파일 p.N" [--lapses 1] [--note N]
                        카드 한 장 추가 (인자 없이 실행하면 표준입력 JSON도 받음)
    due [과목] [--limit N]   오늘 복습할 카드 (JSON 출력)
    grade ID 점수       0~5점으로 채점 결과 반영 (0~2 = 틀림, 3 = 겨우, 4 = 맞음, 5 = 쉬움)
    stats [과목]        과목별 카드 수, 오늘 복습 수, 자주 틀린 주제
    list [과목]         전체 카드 (JSON 출력)
    delete ID           카드 삭제
"""
from __future__ import annotations

import json
import sys
from collections import Counter
from datetime import date, timedelta
from pathlib import Path

DB = Path(__file__).resolve().parent.parent / "data" / "cards.json"
# 앱의 복습 버튼 → SM-2 점수
BUTTONS = {"again": 1, "hard": 3, "good": 4, "easy": 5}


def load() -> list[dict]:
    if not DB.exists():
        return []
    return json.loads(DB.read_text(encoding="utf-8"))


def save(cards: list[dict]) -> None:
    DB.parent.mkdir(parents=True, exist_ok=True)
    tmp = DB.with_suffix(".tmp")
    tmp.write_text(json.dumps(cards, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(DB)  # 쓰다가 끊겨도 기존 파일이 깨지지 않게


def out(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=1))


def today() -> str:
    return date.today().isoformat()


def next_state(card: dict, q: int) -> dict:
    """SM-2 변형. q: 0~5. 카드를 바꾸지 않고 다음 상태만 계산한다 (버튼별 미리보기에도 사용).
    원래 SM-2는 어려움·좋음·쉬움의 간격이 같게 나오는 경우가 많아서 Anki처럼 차이를 둔다."""
    reps, interval, lapses = card["reps"], card["interval"], card["lapses"]
    ease = max(1.3, card["ease"] + 0.1 - (5 - q) * (0.08 + (5 - q) * 0.02))
    if q < 3:
        reps, interval, lapses = 0, 1, lapses + 1
    else:
        reps += 1
        if reps == 1:
            interval = {3: 1, 4: 1, 5: 3}.get(q, 1)
        elif reps == 2:
            interval = {3: 3, 4: 6, 5: 8}.get(q, 6)
        else:
            base = max(interval, 1)
            interval = {3: base * 1.2, 4: base * card["ease"], 5: base * card["ease"] * 1.3}.get(q, base)
            interval = max(round(interval), base + 1)
    return {"reps": reps, "interval": interval, "ease": round(ease, 3), "lapses": lapses}


def sm2(card: dict, q: int) -> None:
    card.update(next_state(card, q))
    card["due"] = (date.today() + timedelta(days=card["interval"])).isoformat()
    card["history"].append([today(), q])


def preview(card: dict) -> dict:
    """버튼별 다음 복습까지 남은 일수."""
    return {name: next_state(card, q)["interval"] for name, q in BUTTONS.items()}


def due_cards(subject: str | None = None) -> list[dict]:
    cards = [c for c in load() if (not subject or c["subject"] == subject) and c["due"] <= today()]
    cards.sort(key=lambda c: (c["due"], -c["lapses"]))
    return [{**c, "preview": preview(c)} for c in cards]


def grade(card_id: int, q: int) -> dict:
    cards = load()
    card = next((c for c in cards if c["id"] == card_id), None)
    if card is None:
        raise KeyError(f"카드 {card_id} 없음")
    sm2(card, q)
    save(cards)
    return card


def add_cards(items: list[dict]) -> list[int]:
    """앱에서 바로 카드 추가 (퀴즈에서 틀린 문제). 필드는 add 명령과 같다."""
    cards = load()
    next_id = max((c["id"] for c in cards), default=0) + 1
    ids = []
    for it in items:
        cards.append({
            "id": next_id, "subject": it["subject"], "topic": it.get("topic", ""),
            "question": it["question"], "answer": it["answer"], "source": it.get("source", ""),
            "note": it.get("note", ""), "created": today(), "due": today(), "interval": 0, "reps": 0,
            "ease": 2.5, "lapses": int(it.get("lapses", 0)), "history": [],
        })
        ids.append(next_id)
        next_id += 1
    save(cards)
    return ids


def delete(card_id: int) -> None:
    save([c for c in load() if c["id"] != card_id])


ADD_FIELDS = ("subject", "topic", "question", "answer", "source", "note", "lapses")


def parse_add_args(args: list[str]) -> dict:
    """--subject 운영체제 --question "..." 형식을 dict로."""
    item, i = {}, 0
    while i < len(args):
        key = args[i].lstrip("-")
        if not args[i].startswith("--") or key not in ADD_FIELDS or i + 1 >= len(args):
            sys.exit(f"잘못된 인자: {args[i]} (사용 가능: {', '.join('--' + f for f in ADD_FIELDS)})")
        item[key] = int(args[i + 1]) if key == "lapses" else args[i + 1]
        i += 2
    return item


def cmd_add(cards: list[dict], args: list[str]) -> None:
    if args:
        items = [parse_add_args(args)]
    else:
        data = json.loads(sys.stdin.read())
        items = data if isinstance(data, list) else [data]
    next_id = max((c["id"] for c in cards), default=0) + 1
    added = []
    for it in items:
        missing = [k for k in ("subject", "question", "answer", "source") if not it.get(k)]
        if missing:
            sys.exit(f"필수 필드 누락 {missing}: {it}")
        cards.append({
            "id": next_id, "subject": it["subject"], "topic": it.get("topic", ""),
            "question": it["question"], "answer": it["answer"], "source": it["source"],
            "note": it.get("note", ""), "created": today(),
            # 틀려서 만든 카드는 내일부터, 바로 복습할 것은 오늘 due
            "due": it.get("due", today()), "interval": 0, "reps": 0, "ease": 2.5,
            "lapses": it.get("lapses", 0), "history": [],
        })
        added.append(next_id)
        next_id += 1
    save(cards)
    print(f"추가됨: {added}")


def by_subject(cards: list[dict], subject: str | None) -> list[dict]:
    return [c for c in cards if not subject or c["subject"] == subject]


def stats(subject: str | None = None) -> dict:
    sel = by_subject(load(), subject)
    result = {}
    for s in Counter(c["subject"] for c in sel):
        sc = [c for c in sel if c["subject"] == s]
        weak = Counter()
        for c in sc:
            if c["lapses"]:
                weak[c["topic"] or c["source"]] += c["lapses"]
        result[s] = {
            "카드수": len(sc),
            "오늘복습": sum(c["due"] <= today() for c in sc),
            "자주틀린주제": weak.most_common(10),
        }
    return result


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stdin.reconfigure(encoding="utf-8")
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    cmd, args = sys.argv[1], sys.argv[2:]
    limit = None
    if "--limit" in args:
        i = args.index("--limit")
        limit = int(args[i + 1])
        args = args[:i] + args[i + 2:]
    cards = load()

    if cmd == "add":
        cmd_add(cards, args)
    elif cmd == "due":
        due = [{k: v for k, v in c.items() if k != "preview"} for c in due_cards(args[0] if args else None)]
        out(due[:limit] if limit else due)
    elif cmd == "grade":
        cid, q = int(args[0]), int(args[1])
        if not 0 <= q <= 5:
            sys.exit("점수는 0~5")
        try:
            card = grade(cid, q)
        except KeyError as exc:
            sys.exit(str(exc))
        print(f"카드 {cid}: 다음 복습 {card['due']} ({card['interval']}일 후), 틀린 횟수 {card['lapses']}")
    elif cmd == "stats":
        out(stats(args[0] if args else None))
    elif cmd == "list":
        out(by_subject(cards, args[0] if args else None))
    elif cmd == "delete":
        delete(int(args[0]))
        print(f"삭제됨: {args[0]}")
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
