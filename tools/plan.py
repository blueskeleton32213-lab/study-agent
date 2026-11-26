"""시험 대비 공부 계획표: 시험 날짜에서 거꾸로 하루 할 일을 짜고, 밀리면 남은 날에 다시 나눈다.

계획은 모델이 아니라 이 스크립트가 짠다 (매번 같은 결과가 나오도록). 과목마다 data/plans/<과목>.json 하나.

- 분량: cache/목차/<과목>.md 의 파일별 쪽 수·소단원(쪽 범위)
- 가중치: 시험 스타일 문제 지도(★, 교수님이 찝어준 문제) + 퀴즈에서 틀린 문제 + 복습 카드에서 틀린 횟수
         퀴즈 정답률이 높은 강의는 줄인다
- 아직 안 올라온 주차: 과목의 한 주 평균 분량으로 자리를 잡아 두고(올라올 날짜 이후에만), 자료가 들어오면
               (목차에 그 번호 파일이 생기면) 실제 쪽 칸으로 바꿔서 다시 나눈다
- 하루 분량: 직접 정하지 않는다. 남은 진도를 남은 날에 고르게 나눈 값이 '시험까지 끝내려면 하루 얼마'
- 다시 나누기: 앱(서버)을 켤 때마다 한 번, 오늘 못 한 칸은 뒤로·미리 한 칸은 빼고 남은 진도를 오늘부터 다시 나눈다
              (오늘 이미 한 분량은 오늘 몫에서 뺀다). 켜 둔 채 날이 바뀌어 밀린 칸이 생겨도 다시 나눈다
- 자동 체크: 그 강의 퀴즈를 풀면 '퀴즈' 칸, 스튜디오 기출 유형 분석·시험대비 요약을 만들면 그 칸,
            약점 분석을 하면 그 칸, 시험 스타일 퀴즈를 풀면 그 칸

    python tools/plan.py show <과목>        # 계획 (오늘 할 일·밀린 칸 다시 나누기 포함) JSON
    python tools/plan.py candidates <과목>  # 계획에 넣을 수 있는 강의자료와 가중치 JSON
"""
from __future__ import annotations

import datetime as dt
import json
import re
import sys
import time
import unicodedata
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PLANS = ROOT / "data" / "plans"
TOC = ROOT / "cache" / "목차"
STYLES = ROOT / "data" / "styles"
QUIZZES = ROOT / "data" / "quizzes"
CHATS = ROOT / "data" / "chats"
STUDIO = ROOT / "studio"
CARDS = ROOT / "data" / "cards.json"

# 시험 관련 자료 (app.py도 이걸 쓴다)
EXAM_RE = re.compile(r"중간|기말|기출|모의|퀴즈|quiz|문제|해설|solution|답안|정답|시험|exam|형광펜|question|exercise|연습", re.I)
NUM_RE = re.compile(r"(\d+)\s*주차|week\s*_?(\d+)|lecture\s*_?(\d+)|(\d+)\s*강|ch(?:apter)?\s*\.?\s*(\d+)|챕터\s*(\d+)|(\d+)\s*장", re.I)
TUTORIAL_RE = re.compile(r"tutorial|튜토리얼", re.I)
SEC_RE = re.compile(r"^- (p\.|slide |part )(\d+)(?:–(\d+))?(?: 🖼)?\s*(.*)$")
HEAD_RE = re.compile(r"^## (.+) \((\d+)쪽, ")

MIN_PER = {"p.": 2.5, "slide ": 2.0, "part ": 8.0}  # 한 쪽(슬라이드·Word 단락) 읽고 정리하는 데 드는 분
QUIZ_MIN = 15
MAX_DAY = 8 * 60  # 하루 필요 시간이 이보다 길면 경고
CHUNK_MAX = 14  # 하루 칸 하나에 넣을 최대 쪽 수 (넘으면 소단원 경계에서 나눈다)


def nfc(s: str) -> str:
    return unicodedata.normalize("NFC", s or "")


def norm(s: str) -> str:
    return re.sub(r"\s+", " ", nfc(s)).strip().lower()


def today() -> dt.date:
    return dt.date.today()


def read_json(p: Path, default):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


# 번호 붙는 모양 (NUM_RE 그룹 순서): 안 올라온 주차 이름을 과목의 파일 이름처럼 짓는다
NUM_LABEL = ["{}주차", "Week {}", "Lecture {}", "{}강", "Ch {}", "챕터 {}", "{}장"]


def lecture_num(name: str) -> int | None:
    m = NUM_RE.search(nfc(name))
    return int(next(g for g in m.groups() if g)) if m else None


def nums_in(text: str) -> set[int]:
    return {int(next(g for g in m.groups() if g)) for m in NUM_RE.finditer(nfc(text))}


# ───────────────────────── 분량: 목차 ─────────────────────────

def toc_files(subject: str) -> list[dict]:
    """목차에서 강의자료(시험 자료 빼고)만: [{file, pages, unit, secs: [(시작, 끝, 제목)]}] 강의 순서대로"""
    path = TOC / f"{subject}.md"
    if not path.exists():
        return []
    out, cur = [], None
    for line in path.read_text(encoding="utf-8").splitlines():
        h = HEAD_RE.match(line)
        if h:
            cur = None
            if EXAM_RE.search(nfc(Path(h.group(1)).stem)):
                continue
            cur = {"file": nfc(h.group(1)), "pages": int(h.group(2)), "unit": "p.", "secs": []}
            out.append(cur)
            continue
        s = SEC_RE.match(line) if cur else None
        if s:
            cur["unit"] = s.group(1)
            a = int(s.group(2))
            cur["secs"].append((a, int(s.group(3) or a), s.group(4).strip()))
    for f in out:
        f["num"] = lecture_num(f["file"])
        if not f["secs"] and f["pages"]:
            f["secs"] = [(1, f["pages"], "")]
    # 강의 번호 순 → 같은 주차면 강의 다음 튜토리얼 → (Contd)는 뒤에. 번호 없는 자료는 맨 뒤
    out.sort(key=lambda f: (f["num"] is None, f["num"] or 0, bool(TUTORIAL_RE.search(f["file"])),
                            "contd" in f["file"].lower(), f["file"]))
    return out


def chunks(f: dict) -> list[dict]:
    """파일 하나를 하루 칸 크기로: 작은 소단원은 합치고, 큰 소단원은 반으로"""
    pieces = []
    for a, b, title in f["secs"]:
        while b - a + 1 > CHUNK_MAX * 1.3:
            mid = a + CHUNK_MAX - 1
            pieces.append((a, mid, title))
            a = mid + 1
        pieces.append((a, b, title))
    merged: list[list] = []
    for a, b, title in pieces:
        span = b - merged[-1][0] + 1 if merged else 0
        small_prev = merged and (merged[-1][1] - merged[-1][0] + 1) < CHUNK_MAX * 0.6
        tiny = b - a + 1 <= 3  # 2~3쪽짜리 꼬리는 따로 칸을 만들지 않고 앞에 붙인다
        if merged and ((span <= CHUNK_MAX and small_prev) or (tiny and span <= CHUNK_MAX * 1.3)):
            merged[-1][1] = b
            if title and title not in merged[-1][2]:
                merged[-1][2].append(title)
        else:
            merged.append([a, b, [title] if title else []])
    return [{"a": a, "b": b, "titles": t[:3]} for a, b, t in merged]


# ───────────────────────── 가중치: 출제 비중·약점 ─────────────────────────

def file_of(source: str, files: list[str]) -> str | None:
    s = norm(source)
    best = None
    for f in files:
        n = norm(f)
        if s.startswith(n) or s.startswith(norm(Path(f).stem)):
            if not best or len(n) > len(norm(best)):
                best = f
    return best


def evidence(subject: str, files: list[dict]) -> dict[str, dict]:
    names = [f["file"] for f in files]
    ev = {n: {"star": 0, "picked": 0, "answered": 0, "correct": 0.0, "wrong": 0.0, "lapses": 0} for n in names}
    by_num: dict[int, list[str]] = {}
    for f in files:
        if f["num"] is not None:
            by_num.setdefault(f["num"], []).append(f["file"])
    # ★ 문제 지도: 어느 주차 문제인지(why·topic)로 그 주차 강의에
    style = read_json(STYLES / f"{subject}.json", {})
    for p in style.get("pool") or []:
        hits = set()
        for n in nums_in(f"{p.get('why', '')} {p.get('topic', '')}"):
            hits.update(by_num.get(n, []))
        tgt = file_of(p.get("answer", "").split(":")[-1].strip(), names)
        if tgt:
            hits.add(tgt)
        for h in hits:
            ev[h]["star"] += 1
            ev[h]["picked"] += 1 if p.get("picked") else 0
    # 퀴즈: 문제의 출처 파일로
    for qf in QUIZZES.glob("*.json") if QUIZZES.exists() else []:
        r = read_json(qf, {})
        if (r.get("cfg") or {}).get("subject") != subject:
            continue
        for q in r.get("qs") or []:
            v = q.get("verdict")
            tgt = v and file_of(q.get("source", ""), names)
            if not tgt:
                continue
            e = ev[tgt]
            e["answered"] += 1
            e["correct"] += 1 if v == "correct" else 0.5 if v == "partial" else 0
            e["wrong"] += 1 if v == "wrong" else 0.5 if v == "partial" else 0
    # 복습 카드: 틀린 횟수
    for c in read_json(CARDS, []):
        if c.get("subject") != subject:
            continue
        tgt = file_of(c.get("source", ""), names)
        if tgt:
            ev[tgt]["lapses"] += int(c.get("lapses") or 0)
    return ev


def weigh(e: dict, top_star: int) -> tuple[float, list[str]]:
    """1.0 기준. 기출 비중은 과목 안에서 가장 많이 나온 강의 대비, 퀴즈는 틀린 비율, 카드는 틀린 횟수"""
    w, why = 1.0, []
    if e["star"]:
        w += 0.6 * e["star"] / max(top_star, 1) + (0.15 if e["picked"] else 0)
        why.append(f"기출·찝어준 문제 {e['star']}개" if e["picked"] else f"기출 {e['star']}문항")
    if e["answered"]:
        acc = e["correct"] / e["answered"]
        why.append(f"퀴즈 정답 {round(acc * 100)}% ({e['answered']}문제)")
        w += 0.8 * (1 - acc) * min(1, e["answered"] / 4)
        if e["answered"] >= 4 and acc >= 0.8:
            w *= 0.8
    if e["lapses"]:
        w += min(0.5, 0.08 * e["lapses"])
        why.append(f"복습 카드 틀림 {e['lapses']}번")
    return round(min(max(w, 0.6), 2.2), 2), why


def weights(subject: str, files: list[dict]) -> dict[str, tuple[float, list[str]]]:
    ev = evidence(subject, files)
    top = max((e["star"] for e in ev.values()), default=0)
    return {n: weigh(e, top) for n, e in ev.items()}


def candidates(subject: str) -> list[dict]:
    files = toc_files(subject)
    ws = weights(subject, files)
    out = []
    for f in files:
        w, why = ws[f["file"]]
        out.append({"file": f["file"], "pages": f["pages"], "unit": f["unit"].strip(), "num": f["num"],
                    "weight": w, "why": why, "minutes": round(f["pages"] * MIN_PER[f["unit"]] * w)})
    return out


# ───────────────────────── 날짜 나누기 ─────────────────────────

def study_days(start: dt.date, exam: dt.date, off: list[int]) -> list[dt.date]:
    days, d = [], start
    while d < exam:
        if d.weekday() not in off:
            days.append(d)
        d += dt.timedelta(days=1)
    return days


def review_count(n: int) -> int:
    return 3 if n >= 10 else 2 if n >= 5 else 1 if n >= 3 else 0


def spread(items: list[dict], days: list[dt.date], fallback: list[dt.date] | None = None, credit: float = 0) -> None:
    """items(순서 유지)를 days에 비슷한 분량씩. 누적 분량이 목표선을 넘기 전까지 같은 날에 넣는다.
    after(올라올 날짜)가 있는 칸은 그날 이후로만. days 안에 그런 날이 없으면 fallback(복습 기간 포함)에서 찾는다.
    credit: 첫날(오늘) 이미 한 분량 — 오늘 몫에서 빼고 나눈다"""
    if not days:
        return
    total = sum(i["min"] for i in items) + credit or 1
    di, cum = 0, float(credit)
    for it in items:
        while di < len(days) - 1 and cum + it["min"] / 2 > total * (di + 1) / len(days):
            di += 1
        after = it.get("after") or ""
        while di < len(days) - 1 and days[di].isoformat() < after:
            di += 1
        it["date"] = days[di].isoformat()
        if it["date"] < after:
            later = [d for d in (fallback or days) if d.isoformat() >= after]
            it["date"] = (later[0] if later else (fallback or days)[-1]).isoformat()
        cum += it["min"]


def lecture_items(f: dict, w: float, why: list[str]) -> list[dict]:
    """강의 파일 하나 → 읽기 칸들 + 확인 퀴즈 칸"""
    items = []
    per = MIN_PER[f["unit"]] * w
    unit = f["unit"]  # "p." / "slide " / "part " → p.4–14, slide 3–5 (출처 표기와 같은 모양)
    for c in chunks(f):
        rng = f"{unit}{c['a']}" if c["a"] == c["b"] else f"{unit}{c['a']}–{c['b']}"
        items.append({"kind": "read", "file": f["file"], "range": rng, "titles": c["titles"],
                      "min": round((c["b"] - c["a"] + 1) * per), "weight": w, "why": why})
    items.append({"kind": "quiz", "file": f["file"], "min": QUIZ_MIN, "weight": w, "why": why})
    return items


def future_hint(subject: str) -> dict:
    """설정 화면용: 다음 번호, 번호 하나(한 주)당 평균 쪽 수·시간, 이름 모양"""
    files = [f for f in toc_files(subject) if f["num"] is not None]
    if not files:
        return {"next": 1, "pages": 30, "unit": "p.", "minutes": round(30 * MIN_PER["p."]), "label": "{}주차", "have": []}
    pages: dict[int, int] = {}
    for f in files:
        pages[f["num"]] = pages.get(f["num"], 0) + f["pages"]
    unit = max(("p.", "slide ", "part "), key=lambda u: sum(1 for f in files if f["unit"] == u))
    avg = round(sum(pages.values()) / len(pages))
    kinds = [next(i for i, g in enumerate(NUM_RE.search(f["file"]).groups()) if g) for f in files]
    label = NUM_LABEL[max(set(kinds), key=kinds.count)]
    return {"next": max(pages) + 1, "pages": avg, "unit": unit, "minutes": round(avg * MIN_PER[unit]),
            "label": label, "have": sorted(pages)}


def future_items(e: dict) -> list[dict]:
    """안 올라온 주차 하나 → 평균 분량을 하루 칸 크기로 나눈 자리 + 확인 퀴즈 자리"""
    n = max(1, -(-e["pages"] // CHUNK_MAX))
    per = round(e["pages"] * MIN_PER[e["unit"]] / n)
    items = [{"kind": "future", "num": e["num"], "label": e["label"], "part": i + 1, "of": n, "min": per,
              "after": e["after"]} for i in range(n)]
    items.append({"kind": "future", "num": e["num"], "label": e["label"], "quiz": True, "min": QUIZ_MIN, "after": e["after"]})
    return items


def review_tasks(subject: str, days: list[dt.date], has_exam: bool) -> list[dict]:
    """시험 직전 날들: 새 진도 없이 기출 유형·약점·시험 스타일 퀴즈·요약 훑기"""
    groups = [
        ([{"kind": "patterns", "label": "기출 유형 분석 만들어서 보기", "min": 40}] if has_exam else [])
        + [{"kind": "weak", "label": "약점 분석 받고 부족한 개념 다시 보기", "min": 40}],
        [{"kind": "examquiz", "label": "시험 스타일 퀴즈 (전체 범위 15문제)", "min": 50}],
        [{"kind": "summary", "label": "시험대비 요약 만들어서 훑기", "min": 40},
         {"kind": "retry", "label": "틀린 퀴즈 문제 다시 풀기", "min": 30}],
    ]
    if len(days) == 1:
        groups = [[t for g in groups for t in g]]
    elif len(days) == 2:
        groups = [groups[0], groups[1] + groups[2]]
    out = []
    for d, g in zip(days, groups):
        for t in g:
            out.append({**t, "date": d.isoformat()})
    return out


def make(subject: str, exam: str, files: list[str], minutes: int | None, off: list[int], future: dict | None = None) -> dict:
    """future: 아직 안 올라온 강의 {start: 첫 번호, count: 개수, first: 첫 강의가 올라올 날, every: 간격(일)}"""
    exam_d = dt.date.fromisoformat(exam)
    start = today()
    if exam_d <= start:
        raise ValueError("시험 날짜는 내일 이후로 골라주세요")
    days = study_days(start, exam_d, off)
    if not days:
        raise ValueError("시험 전까지 공부할 수 있는 날이 없어요 (쉬는 요일을 줄여 보세요)")
    toc = {f["file"]: f for f in toc_files(subject)}
    picked = [toc[f] for f in toc if f in set(map(nfc, files))]  # 강의 순서대로
    fut = []
    if future and int(future.get("count") or 0) > 0:
        hint = future_hint(subject)
        first = dt.date.fromisoformat(future.get("first") or start.isoformat())
        every = max(1, int(future.get("every") or 7))
        n0 = int(future.get("start") or hint["next"])
        for i in range(min(int(future["count"]), 12)):
            num = n0 + i
            fut.append({"num": num, "label": hint["label"].format(num), "pages": hint["pages"], "unit": hint["unit"],
                        "after": (first + dt.timedelta(days=every * i)).isoformat(), "arrived": []})
    if not picked and not fut:
        raise ValueError("계획에 넣을 강의자료를 하나 이상 골라주세요")
    ws = weights(subject, list(toc.values()))  # 설정 화면과 같은 기준 (과목 전체 대비)
    has_exam = bool(read_json(STYLES / f"{subject}.json", {}).get("pool")) or any(
        EXAM_RE.search(nfc(p.stem)) for p in (ROOT / "materials" / subject).glob("*.*"))
    k = review_count(len(days))
    learn_days, rev_days = days[:len(days) - k], days[len(days) - k:]
    items = []
    for f in picked:
        items += lecture_items(f, *ws[f["file"]])
    for e in fut:
        items += future_items(e)
    spread(items, learn_days or days, days)
    tasks = items + review_tasks(subject, rev_days, has_exam)
    for i, t in enumerate(tasks):
        t["id"] = i + 1
        t["done"] = False
    plan = {"subject": subject, "exam": exam, "created": time.strftime("%Y-%m-%d %H:%M:%S"),
            "off": sorted(set(off)), "files": [f["file"] for f in picked], "reviewDays": [d.isoformat() for d in rev_days],
            "tasks": tasks, "notice": "", "future": fut,
            "futureCfg": {k: future.get(k) for k in ("start", "count", "first", "every")} if fut else None}
    save(plan)
    _balanced.add(subject)  # 방금 나눴으니 이번 실행에서 다시 나눌 필요 없음
    return view(subject)


# ───────────────────────── 저장·자동 체크·다시 나누기 ─────────────────────────

def plan_path(subject: str) -> Path:
    if not subject or "/" in subject or "\\" in subject or subject.startswith("."):
        raise ValueError("잘못된 과목이에요")
    return PLANS / f"{subject}.json"


def load(subject: str) -> dict | None:
    p = plan_path(subject)
    return read_json(p, None) if p.exists() else None


def save(plan: dict) -> None:
    p = plan_path(plan["subject"])
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(plan, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(p)


def delete(subject: str) -> None:
    plan_path(subject).unlink(missing_ok=True)


def toggle(subject: str, tid: int, done: bool) -> dict:
    plan = load(subject)
    if not plan:
        raise ValueError("계획이 없어요")
    for t in plan["tasks"]:
        if t["id"] == tid:
            mark(t, bool(done))
            t["auto"] = False
    save(plan)
    return view(subject)


def mark(t: dict, done: bool) -> None:
    """체크하면 한 날(doneOn)을 남기고, 뒷날 칸을 미리 했으면 오늘 칸으로 옮긴다 (달력에 한 날로 보이게)"""
    iso = today().isoformat()
    t["done"] = done
    if done:
        t["doneOn"] = iso
        if t["date"] > iso:
            t["date"], t["early"] = iso, True
    else:
        t.pop("doneOn", None)


def auto_marks(plan: dict) -> bool:
    """앱에서 한 일로 칸 채우기. 계획을 만든 뒤에 한 것만 센다"""
    subject, since = plan["subject"], plan["created"]
    quizzes = []
    for qf in QUIZZES.glob("*.json") if QUIZZES.exists() else []:
        r = read_json(qf, {})
        cfg = r.get("cfg") or {}
        if cfg.get("subject") != subject or (r.get("updated") or "") < since:
            continue
        qs = r.get("qs") or []
        answered = [q for q in qs if q.get("verdict")]
        if len(answered) >= min(3, len(qs) or 3):
            quizzes.append({"cfg": cfg, "qs": qs, "retry": bool(r.get("retry"))})
    studio = []
    folder = STUDIO / subject
    for f in folder.glob("*.md") if folder.exists() else []:
        m = re.match(r"(\d{8})-(\d{6})_(\w+)\.md$", f.name)
        if m:
            when = dt.datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S").strftime("%Y-%m-%d %H:%M:%S")
            if when >= since:
                studio.append(m.group(3))
    weak = False
    for cf in CHATS.glob("*.json") if CHATS.exists() else []:
        c = read_json(cf, {})
        if (c.get("updated") or "") >= since and any(
                (m.get("text") or "").startswith(f"약점 분석 ({subject})") for m in c.get("messages", [])):
            weak = True
            break

    def quiz_on(file: str) -> bool:
        for q in quizzes:
            srcs = [file_of(x.get("source", ""), [file]) for x in q["qs"]]
            if srcs and sum(1 for s in srcs if s) * 2 >= len(srcs):
                return True
            if norm(q["cfg"].get("scope", "")) in (norm(file), norm(Path(file).stem)):
                return True
        return False

    changed = False
    for t in plan["tasks"]:
        if t["done"] or t.get("auto") is False:
            continue
        k = t["kind"]
        hit = (k == "quiz" and quiz_on(t["file"])) or (k == "patterns" and "patterns" in studio) \
            or (k == "summary" and "summary" in studio) or (k == "weak" and weak) \
            or (k == "examquiz" and any(q["cfg"].get("style") == "exam" and not q["retry"] for q in quizzes)) \
            or (k == "retry" and any(q["retry"] for q in quizzes))
        if hit:
            mark(t, True)
            t["auto"] = True
            changed = True
    return changed


PROGRESS = ("read", "quiz", "future")  # 진도 칸 (밀리면 다시 나누는 대상)


def respread(plan: dict) -> None:
    """안 한 진도 전부를 오늘부터 복습 기간 전까지 다시 나누고, 지나간 복습 칸은 남은 복습 날로"""
    now = today()
    iso = now.isoformat()
    days = study_days(now, dt.date.fromisoformat(plan["exam"]), plan["off"])
    if not days:
        return
    rev = [d for d in days if d.isoformat() in plan["reviewDays"]]
    learn = [d for d in days if d not in rev] or days  # 진도 날이 다 지났으면 복습 기간에라도 넣는다
    credit = sum(t["min"] for t in plan["tasks"] if t["done"] and t["kind"] in PROGRESS and t.get("doneOn") == iso)
    spread([t for t in plan["tasks"] if not t["done"] and t["kind"] in PROGRESS], learn, days, credit if learn[0] == now else 0)
    for t in plan["tasks"]:
        if not t["done"] and t["kind"] not in PROGRESS and t["date"] < iso:
            t["date"] = (rev[0] if rev else days[-1]).isoformat()


def set_notice(plan: dict, text: str) -> None:
    iso = today().isoformat()
    old = plan.get("notice") if plan.get("noticeDate") == iso else ""
    plan["notice"] = f"{old} · {text}" if old and text not in old else text
    plan["noticeDate"] = iso


def replan(plan: dict) -> bool:
    """어제까지 못 한 진도가 있으면 다시 나눈다"""
    iso = today().isoformat()
    late = [t for t in plan["tasks"] if not t["done"] and t["kind"] in PROGRESS and t["date"] < iso]
    if not late:
        return False
    respread(plan)
    set_notice(plan, f"밀린 {len(late)}칸을 남은 날에 다시 나눴어요")
    return True


_balanced: set[str] = set()  # 이번 실행(앱을 켠 뒤)에 이미 다시 나눈 과목


def rebalance(plan: dict) -> bool:
    """앱을 켤 때마다 한 번: 오늘 못 한 칸·미리 한 칸을 반영해 남은 진도를 오늘부터 다시 나눈다"""
    if plan["subject"] in _balanced:
        return False
    _balanced.add(plan["subject"])
    iso = today().isoformat()
    late = sum(1 for t in plan["tasks"] if not t["done"] and t["kind"] in PROGRESS and t["date"] < iso)
    early = [t for t in plan["tasks"] if t.pop("early", None)]
    before = {t["id"]: t["date"] for t in plan["tasks"] if not t["done"]}
    respread(plan)
    moved = sum(1 for t in plan["tasks"] if not t["done"] and before.get(t["id"]) != t["date"])
    if moved:
        why = [f"못 한 {late}칸은 뒤로" if late else "", f"미리 한 {len(early)}칸만큼 덜어서" if early else ""]
        set_notice(plan, f"앱을 켜서 남은 진도를 다시 나눴어요 ({', '.join(w for w in why if w) or f'{moved}칸 날짜 바뀜'})")
    return bool(moved or early)


def arrivals(plan: dict) -> bool:
    """안 올라온 주차의 자료가 들어왔으면(목차에 그 번호의 새 파일) 자리 칸을 실제 쪽 칸으로 바꾼다.
    강의는 왔는데 튜토리얼이 나중에 오는 것처럼 같은 번호 파일이 또 오면 그것도 더한다"""
    fut = plan.get("future") or []
    if not fut:
        return False
    toc = toc_files(plan["subject"])
    have = set(plan["files"])
    got = []
    for e in fut:
        new = [f for f in toc if f["num"] == e["num"] and f["file"] not in have]
        if not new:
            continue
        ws = weights(plan["subject"], toc)
        items = [it for f in new for it in lecture_items(f, *ws[f["file"]])]
        tasks = plan["tasks"]
        spots = [i for i, t in enumerate(tasks) if t["kind"] == "future" and t["num"] == e["num"]]
        # 넣을 자리: 그 주차 자리 칸이 있던 곳, 없으면(이미 바뀜) 그 번호 다음 진도 칸 앞
        at = spots[0] if spots else next((i for i, t in enumerate(tasks) if t["kind"] not in PROGRESS
                                          or (t["kind"] == "future" and t["num"] > e["num"])), len(tasks))
        removed = {i for i in spots if not tasks[i]["done"]}  # 해 둔 자리 칸은 남긴다
        at -= sum(1 for i in removed if i < at)
        plan["tasks"] = [t for i, t in enumerate(tasks) if i not in removed]
        nid = max((t["id"] for t in plan["tasks"]), default=0)
        for k, it in enumerate(items):
            it.update(id=nid + k + 1, done=False, date=today().isoformat())
        plan["tasks"][at:at] = items
        plan["files"] += [f["file"] for f in new]
        have.update(f["file"] for f in new)
        e["arrived"] = e.get("arrived", []) + [f["file"] for f in new]
        got.append(e["label"])
    if not got:
        return False
    respread(plan)
    set_notice(plan, f"{', '.join(got)} 자료가 올라와서 실제 쪽으로 계획에 넣었어요")
    return True


def view(subject: str) -> dict | None:
    """화면에 보일 계획: 자동 체크·밀린 칸 다시 나누기를 한 뒤 날짜별로 묶고 오늘 할 일·과부하 경고를 붙인다"""
    plan = load(subject)
    if not plan:
        return None
    changed = arrivals(plan)
    changed = auto_marks(plan) or changed
    changed = rebalance(plan) or changed
    changed = replan(plan) or changed
    if changed:
        save(plan)
    iso = today().isoformat()
    exam = dt.date.fromisoformat(plan["exam"])
    by_day: dict[str, list] = {}
    for t in plan["tasks"]:
        by_day.setdefault(t["date"], []).append(t)
    days = [{"date": d, "tasks": ts, "min": sum(t["min"] for t in ts), "review": d in plan["reviewDays"]}
            for d, ts in sorted(by_day.items())]
    left = [t for t in plan["tasks"] if not t["done"]]
    remaining_days = study_days(today(), exam, plan["off"])
    # 시험까지 끝내려면 하루 얼마: 남은 칸 전부 ÷ 남은 공부 날 (오늘 이미 한 건 빼고)
    need = round(sum(t["min"] for t in left) / len(remaining_days)) if remaining_days and left else 0
    warn = ""
    if need > MAX_DAY:
        warn = (f"시험까지 끝내려면 하루 약 {need // 60}시간 {need % 60}분이 필요해요. "
                "가중치가 낮은 강의를 빼거나 쉬는 요일을 줄여 보세요.")
    total = len(plan["tasks"])
    done = total - len(left)
    return {**plan, "days": days, "today": by_day.get(iso, []), "dday": (exam - today()).days,
            "progress": {"done": done, "total": total}, "warn": warn, "need": need,
            "notice": plan.get("notice") if plan.get("noticeDate") == iso else ""}


def summary(subjects: list[str]) -> list[dict]:
    """처음 화면 '오늘 할 일'용: 계획이 있는 과목마다 오늘 칸"""
    out = []
    for s in subjects:
        try:
            v = view(s)
        except (ValueError, OSError):
            continue
        if v and v["dday"] >= 0:
            out.append({"subject": s, "exam": v["exam"], "dday": v["dday"], "today": v["today"],
                        "progress": v["progress"], "warn": v["warn"], "notice": v["notice"]})
    return sorted(out, key=lambda x: x["dday"])


def calendar(subjects: list[str]) -> list[dict]:
    """계획표 달력: 계획이 있는 과목 전부의 전체 일정 (시험이 지난 계획은 뺀다)"""
    out = []
    for s in subjects:
        try:
            v = view(s)
        except (ValueError, OSError):
            continue
        if v and v["dday"] >= 0:
            out.append({k: v[k] for k in ("subject", "exam", "dday", "tasks", "reviewDays", "need", "off",
                                          "progress", "warn", "notice", "today")})
    return sorted(out, key=lambda x: x["dday"])


def main() -> None:
    if len(sys.argv) < 3 or sys.argv[1] not in ("show", "candidates"):
        print(__doc__)
        sys.exit(1)
    sys.stdout.reconfigure(encoding="utf-8")
    res = view(sys.argv[2]) if sys.argv[1] == "show" else {"files": candidates(sys.argv[2]), "future": future_hint(sys.argv[2])}
    print(json.dumps(res, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
