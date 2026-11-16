"""AI 사용량 기록과 요약.

앱(app.py)과 digest.py가 AI(Claude·GPT·Gemini)를 부를 때마다 data/usage.jsonl에 한 줄씩 남긴다.
사용법:
    python tools/usage.py          # 최근 7일, 기능별 합계
    python tools/usage.py 30       # 최근 30일
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOG = ROOT / "data" / "usage.jsonl"
KIND_NAMES = {"chat": "질문하기", "ask": "질문 창", "quiz": "퀴즈 출제", "style": "시험 스타일 분석",
              "studio": "스튜디오", "grade": "서술형 채점", "digest": "요약본 만들기"}


def log(kind: str, ev: dict, engine: str = "claude") -> None:
    """engines.py가 정리한 result 이벤트(Claude result 모양)에서 사용량만 뽑아 기록한다.
    비용은 Claude만 알려준다 (GPT·Gemini는 0)."""
    u = ev.get("usage") or {}
    if not u:
        return
    row = {
        "t": time.strftime("%Y-%m-%d %H:%M:%S"),
        "kind": kind,
        "engine": engine,
        "models": sorted((ev.get("modelUsage") or {}).keys()),
        "input": u.get("input_tokens", 0) + u.get("cache_creation_input_tokens", 0) + u.get("cache_read_input_tokens", 0),
        "cache_read": u.get("cache_read_input_tokens", 0),
        "output": u.get("output_tokens", 0),
        "turns": ev.get("num_turns"),
        "cost": round(ev.get("total_cost_usd") or 0, 5),
    }
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except OSError:
        pass  # 기록 실패가 답변을 막으면 안 된다


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 7
    since = time.strftime("%Y-%m-%d", time.localtime(time.time() - days * 86400))
    rows = []
    if LOG.exists():
        for line in LOG.read_text(encoding="utf-8").splitlines():
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if r["t"] >= since:
                rows.append(r)
    if not rows:
        print(f"최근 {days}일 기록 없음")
        return
    print(f"최근 {days}일 ({len(rows)}회)\n")
    print(f"{'기능':<12}{'횟수':>5}{'평균 입력':>11}{'평균 출력':>10}{'캐시 비율':>10}{'비용($)':>10}")
    total = 0.0
    for kind in sorted({r["kind"] for r in rows}, key=lambda k: -sum(r["cost"] for r in rows if r["kind"] == k)):
        rs = [r for r in rows if r["kind"] == kind]
        inp = sum(r["input"] for r in rs)
        cost = sum(r["cost"] for r in rs)
        total += cost
        print(f"{KIND_NAMES.get(kind, kind):<12}{len(rs):>5}{inp // len(rs):>11,}{sum(r['output'] for r in rs) // len(rs):>10,}"
              f"{(sum(r['cache_read'] for r in rs) / inp if inp else 0):>10.0%}{cost:>10.3f}")
    print(f"\n합계 ${total:.3f}  (Claude만 계산됨. 구독 요금제면 실제 청구액이 아니라 사용 한도 소모량의 참고치)")
    engines: dict[str, int] = {}
    for r in rows:
        e = r.get("engine", "claude")
        engines[e] = engines.get(e, 0) + 1
    if len(engines) > 1 or "claude" not in engines:
        names = {"claude": "Claude", "gpt": "GPT", "gemini": "Gemini"}
        print("엔진별 횟수: " + ", ".join(f"{names.get(e, e)} {n}회" for e, n in engines.items()))


if __name__ == "__main__":
    main()
