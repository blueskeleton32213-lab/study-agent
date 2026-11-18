"""학습 튜터 앱 서버.

브라우저 화면(tools/static/index.html)과 Claude Code(헤드리스 `claude -p`) 사이를 이어준다.
내 PC(127.0.0.1)에서만 접속되며, 화면이 닫히고 몇 분 지나면 스스로 종료된다.

    python tools/app.py          # 직접 실행 (보통은 launch.pyw가 실행함)
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

ROOT = Path(__file__).resolve().parent.parent
TOOLS = ROOT / "tools"
MATERIALS = ROOT / "materials"
TEXTS = ROOT / "texts"
STATIC = TOOLS / "static"
STATIC_TYPES = {".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8", ".woff2": "font/woff2",
                ".woff": "font/woff", ".ttf": "font/ttf", ".ico": "image/x-icon", ".png": "image/png", ".svg": "image/svg+xml"}
PORT = int(os.environ.get("STUDY_APP_PORT", "8765"))
IDLE_EXIT_SECONDS = 180  # 화면 heartbeat가 이만큼 없으면 종료
ALLOWED_EXT = {".pdf", ".pptx", ".docx"}
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

sys.path.insert(0, str(TOOLS))
import srs  # noqa: E402  복습 카드는 Claude 없이 직접 처리

srs_lock = threading.Lock()
PYTHON = sys.executable.replace("pythonw.exe", "python.exe")
# AI 엔진(Claude Code / Codex / Antigravity)·요금제·기능별 모델은 engines.py와 data/ai.json ('AI 설정' 창).
# 앱이 띄우는 AI에는 튜터에 필요한 도구만 주고 개인 커넥터·스킬은 싣지 않는다 (토큰 절약).
# 사용량은 engines.py가 data/usage.jsonl에 기록 (요약: python tools/usage.py)
import engines  # noqa: E402
import onedrive_sync  # noqa: E402  OneDrive 동기화 켜기/끄기 (사이드바)

def code_version() -> float:
    """서버 코드(tools/*.py)의 최신 수정 시각. 실행 중에 코드가 바뀌면 launch.pyw가 서버를 교체한다."""
    return max(p.stat().st_mtime for p in TOOLS.glob("*.py"))


STARTED_VERSION = code_version()
state_lock = threading.Lock()
# Claude 실행 칸: chat(질문·설명) / quiz(퀴즈 출제·시험 스타일 분석) / studio(스튜디오 만들기).
# 칸마다 하나씩이라 퀴즈를 만드는 동안에도 질문하거나 스튜디오 자료를 만들 수 있다
procs: dict[str, subprocess.Popen] = {}


def is_busy(slot: str | None = None) -> bool:
    with state_lock:
        return any(p.poll() is None for k, p in procs.items() if slot in (None, k))


# 인터넷이 끊기면 AI CLI가 재시도만 하며 답이 없어 화면이 무한 로딩된다 → AI를 부르기 전에 먼저 확인
OFFLINE_MSG = "인터넷에 연결되어 있지 않아요. 인터넷을 연결해서 사용해주세요."
AI_HOSTS = {"claude": "api.anthropic.com", "gpt": "chatgpt.com", "gemini": "generativelanguage.googleapis.com"}
AI_PATHS = {"/api/chat", "/api/quiz/generate", "/api/studio/generate", "/api/quiz/grade", "/api/quiz/websolve"}
_net = {"ok": True, "at": 0.0}


def online() -> bool:
    """AI 서버(없으면 1.1.1.1)에 3초 안에 붙으면 인터넷 있음. 결과는 연결되면 30초, 끊기면 바로 다시 확인"""
    import socket
    if _net["ok"] and time.time() - _net["at"] < 30:
        return True
    host = AI_HOSTS.get(engines.load().get("engine"), "api.anthropic.com")
    ok = False
    for addr in (host, "1.1.1.1"):
        try:
            socket.create_connection((addr, 443), timeout=3).close()
            ok = True
            break
        except OSError:
            pass
    _net.update(ok=ok, at=time.time())
    return ok
last_seen = time.time()


# ───────────────────────── 데이터 ─────────────────────────
def safe_name(name: str) -> str:
    name = name.strip()
    if not name or name in {".", ".."} or re.search(r'[\\/:*?"<>|]', name):
        raise ValueError(f"사용할 수 없는 이름: {name!r}")
    return name


def run_tool(*args: str) -> str:
    r = subprocess.run([PYTHON, str(TOOLS / args[0]), *args[1:]], cwd=ROOT, capture_output=True,
                       text=True, encoding="utf-8", errors="replace", creationflags=NO_WINDOW)
    return (r.stdout + r.stderr).strip()


# ───────────────────────── 대화 기록 ─────────────────────────
CHATS = ROOT / "data" / "chats"
ASKS = ROOT / "data" / "asks"  # 질문 창(드래그해서 묻기) 대화는 따로 모은다. 같은 모양 + 사용자 메시지에 quote(드래그한 부분)
TALKS = {"chats": CHATS, "asks": ASKS}  # /api/chats/*, /api/asks/* 가 같은 코드를 쓴다
CHAT_ID = re.compile(r"^[0-9a-f]{32}$")


def chat_path(cid: str, base: Path = CHATS) -> Path:
    if not CHAT_ID.match(cid or ""):
        raise ValueError("잘못된 대화 기록이에요")
    return base / f"{cid}.json"


def chat_list(base: Path = CHATS) -> list[dict]:
    out = []
    for f in base.glob("*.json") if base.exists() else []:
        try:
            c = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        # 검색용으로 대화 내용을 짧게 합쳐서 같이 보낸다 (전체 내용은 열 때 받음)
        text = " ".join(m.get("text", "") for m in c.get("messages", []))
        out.append({"id": c["id"], "title": c.get("title", ""), "subject": c.get("subject"), "created": c.get("created"),
                    "updated": c.get("updated"), "count": len(c.get("messages", [])), "search": text[:4000],
                    "scope": (c.get("scope") or {}).get("label")})
    return sorted(out, key=lambda c: c.get("updated") or "", reverse=True)


def chat_save(data: dict, base: Path = CHATS) -> dict:
    import uuid

    def keep(m):
        r = {"role": m["role"], "text": str(m.get("text", ""))}
        q = m.get("quote")
        if isinstance(q, dict) and q.get("text"):  # 질문 창: 어디서 어떤 부분을 드래그해서 물었는지
            r["quote"] = {"where": str(q.get("where", ""))[:40], "text": str(q["text"])[:1500]}
        return r
    msgs = [keep(m) for m in data.get("messages", []) if m.get("role") in ("user", "bot", "notice")]
    if not msgs:
        raise ValueError("저장할 대화가 없어요")
    cid = data.get("id") or uuid.uuid4().hex
    path = chat_path(cid, base)
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    old = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    fm = next((m for m in msgs if m["role"] == "user"), {"text": "새 대화"})
    first = fm["text"]
    if fm.get("quote"):  # 질문 창은 "쉽게 설명해줘"만으로는 구분이 안 돼서 드래그한 부분을 제목 앞에
        q = re.sub(r"\s+", " ", fm["quote"]["text"]).strip()[:24]
        first = f"「{q}」 {first}"
    title = old.get("title") or re.sub(r"\s+", " ", first).strip()[:48]
    base.mkdir(parents=True, exist_ok=True)
    sc = data.get("scope") or old.get("scope")  # 질문 창: 이 대화의 범위 (이 퀴즈·이 정리 자료 …)
    sc = {"key": str(sc.get("key", ""))[:300], "label": str(sc.get("label", ""))[:120]} if isinstance(sc, dict) else None
    rec = {"id": cid, "title": title, "subject": data.get("subject") or old.get("subject"), **({"scope": sc} if sc else {}),
           "session_id": data.get("session_id") or old.get("session_id"),
           "created": old.get("created", now), "updated": now, "messages": msgs}
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(rec, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)
    return {"ok": True, "id": cid, "title": title}


# ───────────────────────── 퀴즈 기록 ─────────────────────────
# 퀴즈마다 data/quizzes/<id>.json. 문제를 만들면 바로 저장하고, 풀 때마다 진행 상황을 덮어쓴다
QUIZZES = ROOT / "data" / "quizzes"
QUIZ_KEYS = ("cfg", "qs", "idx", "phase", "retry", "styleUsed", "saved", "savedCount")
# 복습도 한 번 할 때마다 data/reviews/<id>.json (어떤 카드를 어떻게 매겼는지)
REVIEWS = ROOT / "data" / "reviews"
REVIEW_KEYS = ("scope", "total", "done", "items")
RECORDS = {"quizzes": QUIZZES, "reviews": REVIEWS}  # 제목 바꾸기·삭제·열기는 둘 다 같은 방식


def record_path(kind: str, rid: str) -> Path:
    if kind not in RECORDS or not CHAT_ID.match(rid or ""):
        raise ValueError("잘못된 기록이에요")
    return RECORDS[kind] / f"{rid}.json"


def quiz_path(qid: str) -> Path:
    return record_path("quizzes", qid)


def write_record(path: Path, rec: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(rec, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)


def quiz_list() -> list[dict]:
    out = []
    for f in QUIZZES.glob("*.json") if QUIZZES.exists() else []:
        try:
            r = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        qs, cfg = r.get("qs") or [], r.get("cfg") or {}
        v = [q.get("verdict") for q in qs]
        out.append({"id": r["id"], "title": r.get("title", ""), "subject": cfg.get("subject"), "level": cfg.get("level"),
                    "created": r.get("created"), "updated": r.get("updated"), "total": len(qs),
                    "answered": sum(1 for x in v if x), "correct": v.count("correct"), "partial": v.count("partial"),
                    "done": r.get("phase") == "done", "retry": bool(r.get("retry")),
                    "search": " ".join(q.get("question", "")[:200] for q in qs)[:3000]})
    return sorted(out, key=lambda r: r.get("updated") or "", reverse=True)


def quiz_save(data: dict) -> dict:
    import uuid
    if not data.get("qs"):
        raise ValueError("저장할 문제가 없어요")
    qid = data.get("id") or uuid.uuid4().hex
    path = quiz_path(qid)
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    old = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    cfg = data.get("cfg") or {}
    title = old.get("title") or data.get("title") or \
        f"{'다시 풀기 · ' if data.get('retry') else ''}{cfg.get('subject', '')} · {cfg.get('scope') or '전체 범위'}"
    rec = {"id": qid, "title": title[:80], "created": old.get("created", now), "updated": now,
           **{k: data.get(k) for k in QUIZ_KEYS}}
    write_record(path, rec)
    return {"ok": True, "id": qid, "title": rec["title"]}


def review_list() -> list[dict]:
    out = []
    for f in REVIEWS.glob("*.json") if REVIEWS.exists() else []:
        try:
            r = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        items = r.get("items") or []
        counts = {b: sum(1 for it in items if it.get("button") == b) for b in srs.BUTTONS}
        subjects = sorted({it.get("subject") for it in items if it.get("subject")})
        out.append({"id": r["id"], "title": r.get("title", ""), "scope": r.get("scope"), "subjects": subjects,
                    "created": r.get("created"), "updated": r.get("updated"), "count": len(items),
                    "total": r.get("total") or len(items), "done": bool(r.get("done")), "counts": counts,
                    "search": " ".join(f"{it.get('topic', '')} {it.get('question', '')[:150]}" for it in items)[:3000]})
    return sorted(out, key=lambda r: r.get("updated") or "", reverse=True)


def review_save(data: dict) -> dict:
    import uuid
    if not data.get("items"):
        raise ValueError("저장할 복습이 없어요")
    rid = data.get("id") or uuid.uuid4().hex
    path = record_path("reviews", rid)
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    old = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    title = old.get("title") or f"{data.get('scope') or '전체 과목'} 복습"
    rec = {"id": rid, "title": title[:80], "created": old.get("created", now), "updated": now,
           **{k: data.get(k) for k in REVIEW_KEYS}}
    write_record(path, rec)
    return {"ok": True, "id": rid, "title": rec["title"]}


ORDER_FILE = ROOT / "data" / "subject_order.json"


def load_order() -> list[str]:
    try:
        return json.loads(ORDER_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []


def save_order(order: list[str]) -> None:
    ORDER_FILE.parent.mkdir(parents=True, exist_ok=True)
    ORDER_FILE.write_text(json.dumps([safe_name(n) for n in order], ensure_ascii=False), encoding="utf-8")


syncing = {"on": False}


def background_sync() -> bool:
    """어긋나 있으면 백그라운드에서 맞추기 시작. 맞추는 중이면 True."""
    if syncing["on"]:
        return True
    if not materials_out_of_sync():
        return False

    def work():
        try:
            sync_materials()
        finally:
            syncing["on"] = False
    syncing["on"] = True
    threading.Thread(target=work, daemon=True).start()
    return True


def app_state() -> dict:
    MATERIALS.mkdir(exist_ok=True)
    is_syncing = background_sync()
    with srs_lock:
        stats = srs.stats()
    subjects = []
    names = {p.name for p in MATERIALS.iterdir() if p.is_dir()} | set(stats)
    order = {n: i for i, n in enumerate(load_order())}  # 사용자가 정한 순서, 새 과목은 뒤에 이름순
    for name in sorted(names, key=lambda n: (order.get(n, len(order)), n)):
        folder = MATERIALS / name
        files = sorted(f.name for f in folder.rglob("*") if f.suffix.lower() in ALLOWED_EXT) if folder.exists() else []
        s = stats.get(name, {})
        subjects.append({"name": name, "files": files, "cards": s.get("카드수", 0),
                         "due": s.get("오늘복습", 0), "weak": s.get("자주틀린주제", [])[:3]})
    return {"subjects": subjects, "due": sum(s["due"] for s in subjects), "syncing": is_syncing,
            "ai": engines.current_name(), "onedrive": onedrive_sync.enabled(), "codeSync": take_code_sync()}


CODE_SYNC_FILE = ROOT / "local" / "code_sync.json"


def take_code_sync() -> str:
    """앱을 켤 때 launch.pyw가 남긴 코드 동기화 안내 (받아옴·실패 이유). 한 번 보여 주면 지운다."""
    try:
        msg = json.loads(CODE_SYNC_FILE.read_text(encoding="utf-8")).get("message", "")
        CODE_SYNC_FILE.unlink()
        return msg
    except Exception:
        return ""


onedrive_lock = threading.Lock()


def set_onedrive(on: bool) -> dict:
    """사이드바 「OneDrive 동기화」 스위치. 켜면 바로 한 번 맞춘다 (다른 컴퓨터 변경 받기 + 이 컴퓨터 변경 올리기)."""
    onedrive_sync.set_enabled(on)
    if not on:
        return {"ok": True, "onedrive": False, "message": "동기화를 껐어요. 이제 이 컴퓨터에만 저장해요."}
    with onedrive_lock, srs_lock:  # 맞추는 동안 복습 카드를 쓰지 않게
        code = onedrive_sync.main()
    msg = onedrive_sync.last_message
    if code:
        msg += " (일부 파일은 다음 동기화 때 다시 시도해요)"
    return {"ok": True, "onedrive": True, "message": msg}


# ───────────────────────── 출처 보기 ─────────────────────────
CACHE_HD = ROOT / "cache" / "hd"


def material_path(rel: str) -> Path:
    """materials/ 안의 파일만 허용.
    materials/가 정션·링크일 수도 있어서 resolve()하면 실제 경로가 달라지므로,
    검사는 실제 경로끼리 하고 돌려줄 땐 MATERIALS 기준 경로로 되돌린다 (relative_to(MATERIALS)가 깨지지 않게)."""
    real = MATERIALS.resolve()
    p = (MATERIALS / rel).resolve()
    if not p.is_relative_to(real) or not p.is_file() or p.suffix.lower() not in ALLOWED_EXT:
        raise ValueError(f"자료를 찾을 수 없어요: {rel}")
    return MATERIALS / p.relative_to(real)


def norm(s: str) -> str:
    return re.sub(r"[\s_\-]+", "", s).lower()


def find_source(name: str, subject: str | None) -> Path | None:
    """답변에 적힌 파일명(확장자가 .md여도 됨)을 실제 자료 파일로 찾는다. 같은 이름이면 현재 과목 우선."""
    stem = norm(Path(name).stem if Path(name).suffix.lower() in ALLOWED_EXT | {".md"} else name)
    hits = [f for f in MATERIALS.rglob("*") if f.suffix.lower() in ALLOWED_EXT and norm(f.stem) == stem]
    hits.sort(key=lambda f: f.parent.name != subject)
    return hits[0] if hits else None


def text_sections(src: Path) -> list[tuple[str, str]]:
    """texts/의 추출본을 ## [..] 머리말 단위로 나눈다 → [(머리말, 본문)]"""
    md = TEXTS / src.relative_to(MATERIALS).with_suffix(".md")
    if not md.exists():
        run_tool("extract.py", src.parent.name)
    if not md.exists():
        return []
    chunks = re.split(r"^## \[(.+?)\]\s*$", md.read_text(encoding="utf-8"), flags=re.M)
    return [(chunks[i], chunks[i + 1].strip()) for i in range(1, len(chunks) - 1, 2)]


def best_section(secs: list[tuple[str, str]], hint: str) -> int:
    """번호 없이 "3장 CDF 부분"처럼 적힌 출처 → 가장 알맞은 구간 번호(1부터).
    목차처럼 모든 단어가 다 들어 있는 구간에 끌려가지 않도록, 본문 등장 횟수보다 '제목에 있는지'를 크게 본다."""
    words = [w for w in re.split(r"[\s,.·()]+", hint) if len(w) >= 2 and w not in {"부분", "내용", "같은", "참고"}]
    if not words:
        return 1

    def in_title(w: str, t: str) -> bool:
        m = re.fullmatch(r"(\d+)장", w)  # "3장" ↔ "3.3 누적분포함수"
        return w.lower() in t.lower() or bool(m and re.match(rf"{m.group(1)}\.\d", t))

    scores = []
    for title, body in secs:
        title = re.sub(r"^(?:part|slide|p\.)\s*\d+\s*·?\s*", "", title)  # "part 106 · 3.3 누적분포함수" → "3.3 누적분포함수"
        s = 10 * sum(in_title(w, title) for w in words) + sum(w.lower() in body.lower() for w in words)
        if re.search(r"목록|목차|차례|요약$", title):
            s -= 5
        scores.append(s)
    return scores.index(max(scores)) + 1 if max(scores) > 0 else 1


def source_info(name: str, subject: str | None) -> dict:
    src = find_source(name, subject)
    if src is None:
        raise ValueError(f"자료 목록에 없는 파일이에요: {name}")
    rel = src.relative_to(MATERIALS).as_posix()
    if src.suffix.lower() == ".pdf":
        import pymupdf
        with pymupdf.open(src) as doc:
            count = len(doc)
        return {"path": rel, "name": src.name, "kind": "pdf", "count": count}
    return {"path": rel, "name": src.name, "kind": src.suffix.lower()[1:], "count": len(text_sections(src))}


def render_page(rel: str, page: int) -> bytes:
    import pymupdf
    src = material_path(rel)
    out = CACHE_HD / src.relative_to(MATERIALS).parent / f"{src.stem}_p{page}@220.png"
    if not out.exists() or out.stat().st_mtime < src.stat().st_mtime:
        with pymupdf.open(src) as doc:
            if not 1 <= page <= len(doc):
                raise ValueError(f"{page}쪽이 없어요 (전체 {len(doc)}쪽)")
            out.parent.mkdir(parents=True, exist_ok=True)
            doc[page - 1].get_pixmap(dpi=220).save(out)
    return out.read_bytes()


# ───────────────────────── Claude 호출 ─────────────────────────
def tool_label(name: str, inp: dict) -> str:
    if name == "WebSearch":
        return f"🌐 인터넷 검색: {inp.get('query', '')}"[:80]
    if name == "WebFetch":
        return f"🌐 웹 페이지 읽는 중: {urlparse(inp.get('url', '')).netloc}"[:80]
    if name in ("Grep", "Glob"):
        return f"자료 검색: {inp.get('pattern', '')}"[:80]
    if name == "Read":
        return f"읽는 중: {Path(inp.get('file_path', '')).name}"
    if name == "Bash":
        cmd = inp.get("command", "")
        if "srs.py add" in cmd:
            return "복습 카드 저장"
        if "srs.py grade" in cmd:
            return "채점 기록"
        if "srs.py" in cmd:
            return "학습 기록 확인"
        if "extract.py" in cmd:
            return "자료 텍스트 추출"
        if "render.py" in cmd:
            return "원본 쪽 그림으로 확인"
        if "digest.py" in cmd:
            return "요약본 읽는 중 (처음이면 만드는 중)"
        # GPT(Codex)는 검색·읽기도 셸 명령으로 한다
        if re.search(r"\b(rg|grep|Select-String|findstr)\b", cmd):
            m = re.search(r"""(?:rg|grep|Select-String(?:\s+-Pattern)?|findstr)\s+(?:-\S+\s+)*["']([^"']+)["']""", cmd)
            return f"자료 검색: {m.group(1) if m else ''}"[:80]
        if re.search(r"\b(Get-Content|cat|sed|head|tail|type)\b", cmd):
            m = re.findall(r"[\w가-힣 .\-]+\.(?:md|txt|json|py)", cmd)
            return f"읽는 중: {Path(m[-1].strip()).name}" if m else "자료 읽는 중"
        if re.search(r"\b(ls|dir|Get-ChildItem|find)\b", cmd):
            return "자료 목록 확인"
        return "명령 실행"
    if name == "Skill":
        return f"스킬 실행: {inp.get('skill', '')}"
    if name == "StructuredOutput":
        return "문제 정리 중"
    # Gemini(Antigravity) 도구 이름은 엔진마다 달라서 뜻으로 묶는다
    low = name.lower()
    vals = [v for v in inp.values() if isinstance(v, str)] if isinstance(inp, dict) else []
    first = vals[0] if vals else ""
    if any(w in low for w in ("command", "shell", "terminal")):
        return tool_label("Bash", {"command": inp.get("command") or inp.get("CommandLine") or first})
    if any(w in low for w in ("grep", "search", "find")):
        return f"자료 검색: {first}"[:80]
    if any(w in low for w in ("view", "read")):
        return f"읽는 중: {Path(first).name}" if first else "자료 읽는 중"
    if "list" in low or "dir" in low:
        return "자료 목록 확인"
    if low == "web_search":
        return "웹 검색"
    return name


def stream_claude(message: str, session_id: str | None, send, schema: dict | None = None,
                  on_result=None, slot: str = "chat", kind: str | None = None, web: bool = False) -> None:
    """설정에서 고른 AI 엔진을 실행하며 이벤트를 send로 흘려보낸다. on_result가 있으면 result 이벤트를 넘겨준다.
    schema: 답을 이 JSON 모양으로 받는다 (result의 structured_output). kind: 모델 선택·사용량 기록용 이름 (없으면 slot)
    web: 이번 실행에만 인터넷 검색을 켠다 (퀴즈의 '인터넷에서 답 찾기' — 사용자가 개수를 고른 뒤에만)"""
    run = engines.start(kind or slot, message, session_id, schema, web=web)
    proc = run.proc
    with state_lock:
        procs[slot] = proc
    try:
        for ev in run.events():
            t = ev["type"]
            if t == "tool":
                send({"type": "tool", "label": tool_label(ev["name"], ev.get("input") or {})})
            elif t == "result":
                if on_result:
                    on_result(ev)
                if ev.get("is_error"):
                    send({"type": "error", "message": str(ev.get("result") or ev.get("subtype"))})
                send({"type": "done", "session_id": ev.get("session_id") or session_id})
            else:
                send(ev)
    finally:
        with state_lock:
            if procs.get(slot) is proc:
                del procs[slot]
        if proc.poll() is None:
            proc.kill()


# ───────────────────────── 퀴즈 ─────────────────────────
QUIZ_SCHEMA = {
    "type": "object",
    "properties": {"questions": {"type": "array", "items": {
        "type": "object",
        "properties": {
            "type": {"type": "string", "enum": ["mcq", "short"]},
            "question": {"type": "string"},
            "choices": {"type": "array", "items": {"type": "string"}},
            "answer_index": {"type": "integer"},
            "answer": {"type": "string"},
            "explanation": {"type": "string"},
            "source": {"type": "string"},
            "topic": {"type": "string"},
            # 답의 근거: 해설(해설·정답 파일) / 자료(강의자료에서 찾아 직접 풂) / 근거 없음(자료에서 못 찾음 → 앱이 인터넷으로 찾을지 묻는다)
            "basis": {"type": "string", "enum": ["해설", "자료", "근거 없음"]},
        },
        "required": ["type", "question", "choices", "answer_index", "answer", "explanation", "source", "topic", "basis"],
    }}},
    "required": ["questions"],
}
GRADE_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["correct", "partial", "wrong"]},
        "feedback": {"type": "string"},
        "missing": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["verdict", "feedback", "missing"],
}
LEVELS = {
    "기초": "정의·용어·기본 개념을 확인하는 문제",
    "보통": "개념 이해·비교·간단한 적용 문제",
    "시험": "실제 시험 수준: 기출·모의고사 스타일의 계산·응용·함정 문제",
}
QTYPES = {"mix": "객관식과 서술형을 반반 정도 섞어서", "mcq": "모두 객관식(4지선다)으로", "short": "모두 서술형(단답·계산 포함)으로"}


# ───────────────────────── 과목별 시험 스타일 ─────────────────────────
# 파일명으로 시험·문제 자료를 찾는다 (중간고사·기출·모의고사·퀴즈·문제지·해설·형광펜 문항·연습문제·Question 등)
# 강의 파일 속에 섞인 예제·연습문제는 파일명으로 못 찾으니, 분석할 때 AI가 본문을 검색해서 찾는다 (style_prompt)
EXAM_RE = re.compile(r"중간|기말|기출|모의|퀴즈|quiz|문제|해설|solution|답안|정답|시험|exam|형광펜|question|exercise|연습", re.I)
STYLES = ROOT / "data" / "styles"
# pool: 문제 지도 — 따로 준 문제·강의 속 예제/연습문제·시험에 뽑힌 문제의 위치와 답이 있는지 (문제 본문은 안 옮김)
POOL_ITEM = {"type": "object", "properties": {
    "source": {"type": "string"}, "page": {"type": "string"}, "label": {"type": "string"},
    "kind": {"type": "string", "enum": ["시험지", "연습문제", "예제"]}, "picked": {"type": "boolean"},
    "why": {"type": "string"}, "topic": {"type": "string"}, "answer": {"type": "string"}},
    "required": ["source", "page", "label", "kind", "picked", "why", "topic", "answer"]}
STYLE_SCHEMA = {"type": "object", "properties": {"guide": {"type": "string"}, "pool": {"type": "array", "items": POOL_ITEM}},
                "required": ["guide", "pool"]}


def exam_files(subject: str) -> list[Path]:
    folder = MATERIALS / subject
    if not folder.exists():
        return []
    import unicodedata  # iClass·Mac에서 온 파일 이름은 한글이 풀어쓴 형태(NFD)라 "퀴즈"·"문제"가 안 맞는다 → NFC로 맞춰서 찾는다
    return sorted(f for f in folder.rglob("*")
                  if f.suffix.lower() in ALLOWED_EXT and EXAM_RE.search(unicodedata.normalize("NFC", f.stem)))


def style_info(subject: str) -> dict:
    files = exam_files(subject)
    sig = {f.name: int(f.stat().st_mtime) for f in files}
    path = STYLES / f"{subject}.json"
    rec = None
    if path.exists():
        try:
            rec = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            rec = None
    note_path = STYLES / f"{subject}.note.txt"  # 사용자가 직접 적은 교수님 출제 스타일 (분석과 별도로 보관)
    note = note_path.read_text(encoding="utf-8").strip() if note_path.exists() else ""
    return {"subject": subject, "files": [f.name for f in files], "has_exam": bool(files), "note": note,
            "analyzed": rec.get("created") if rec else None, "guide": rec.get("guide") if rec else None,
            "pool": (rec.get("pool") or []) if rec else [],
            # 시험 자료가 추가·수정·삭제되면 다시 분석해야 한다
            "stale": bool(files) and (not rec or rec.get("files") != sig), "sig": sig}


def style_prompt(subject: str, files: list[str], note: str = "") -> str:
    names = "\n".join(f"- {n} → texts/{subject}/{Path(n).with_suffix('.md').name}" for n in files) or "- (파일명으로 찾은 시험·문제 파일 없음)"
    return f"""[시험 스타일 분석 — 결과는 JSON(guide, pool)으로만]
과목: {subject}
이 과목 교수님은 문제를 세 가지 방식으로 낼 수 있다: ① 문제를 파일로 따로 준다 ② 개념 설명 사이에 예제·연습문제를 넣는다 ③ 연습문제 중 몇 개만 골라 시험에 낸다.
셋을 모두 찾아 "문제 지도"(pool)를 만들고, 이 과목 시험이 어떤 형식으로 나오는지 출제자가 그대로 따라 할 수 있는 스타일 가이드(guide)를 만든다.

## 입력
- 시험·문제 파일 (파일명으로 찾음, **모두** 읽는다):
{names}
- 강의 파일 목록: cache/목차/{subject}.md
- 교수님 출제 메모 (학생이 적은 것, 가장 우선): {note or "없음"}

## 1단계: 문제 찾기 (토큰 절약 — 강의 원문을 통째로 읽지 않는다)
A. 따로 준 문제: 위 시험·문제 파일을 읽는다. 해설·정답·solution 파일이 있으면 어느 문제의 답인지 짝짓는다.
B. 강의 속 문제: texts/{subject}/ 전체를 Grep으로 찾는다 (패턴 예: `예제|Example|Exercise|연습\\s*문제|Problem|확인\\s*문제|풀어\\s*보|Quiz|Q\\d`).
   찾은 쪽만 앞뒤 20~40줄 읽어 학생이 직접 풀어야 하는 문제인지 확인한다. 개념 설명 속 짧은 예시는 뺀다. 풀이가 같은 쪽·다음 쪽에 있으면 답이 있는 것.
C. 시험에 뽑힌 문제 (picked):
   - 메모에 "Ch3 연습문제 2·5번" 같은 지정이 있으면 그대로 표시한다.
   - 형광펜·시험대비·범위 공지 파일에 문항 번호가 있으면 표시한다.
   - 기출·모의고사 문제가 어떤 연습문제·예제와 같은 구조면(숫자만 다르거나 그대로) 그 연습문제를 표시하고 why에 짝을 적는다.
   - 근거가 없으면 picked는 false. 추측하지 않는다.

## 2단계: guide (마크다운, 1500~3000자)
1. 출제 방식: 위 ①②③ 중 이 과목은 어떤 방식인지와 비율 (예: "시험 6문제 중 4문제가 연습문제 변형")
2. 문항 구성: 한 시험(또는 한 세트)의 문항 수, 유형 비율(계산·서술·단답·객관식·OX·빈칸), 배점 표시 방식
3. 문장 형식: 실제 문제의 말투와 지시문 템플릿 3~6개 (원문 표현을 짧게 인용, 예: "~를 구하시오", "(10점)")
4. 구조: 소문항 (1)(2)/(a)(b) 사용 여부, 조건·표·그림·수식을 주는 방식
5. 연습문제 → 시험 변형 방식: 숫자만 바꿈 / 조건 추가 / 소문항 합침 / 그대로 (짧은 예 1~2개)
6. 자주 나오는 주제와 강의 범위, 난이도와 함정
7. 답안 요구 형식: 풀이 과정 요구 여부, 단위·최종 답 표기
8. 출제 체크리스트: 이 과목 스타일로 새 문제를 낼 때 지킬 것 5~8개

## pool (최대 60개, 시험에 뽑힌 것·시험지 문제 먼저)
source: 원본 파일명 그대로 / page: "p.N"·"slide N" / label: "예제 3.2", "연습문제 5", "중간 3번" / kind: 시험지·연습문제·예제
picked·why: 위 C / topic: 짧은 개념명 / answer: 답이 있으면 "해설 있음: 파일명 p.N", 없으면 "없음"

규칙: 수식은 LaTeX. `(🖼 ...)` 표시가 있는 쪽의 수식·그림이 필요하면 render.py로 원본을 본다. 문제를 통째로 옮기지 말고 형식을 요약한다 (pool에는 위치만). 인사말·진행 멘트 없이."""


def save_style(subject: str, guide: str, sig: dict, pool: list | None = None) -> None:
    STYLES.mkdir(parents=True, exist_ok=True)
    (STYLES / f"{subject}.json").write_text(json.dumps(
        {"subject": subject, "created": time.strftime("%Y-%m-%d %H:%M"), "files": sig, "guide": guide, "pool": pool or []},
        ensure_ascii=False, indent=1), encoding="utf-8")


def pool_text(pool: list) -> str:
    """퀴즈 출제에 넘길 문제 지도 (한 줄씩, 시험에 뽑힌 것 먼저)"""
    rows = sorted(pool or [], key=lambda p: not p.get("picked"))[:60]
    return "\n".join(f"- {'★ ' if p.get('picked') else ''}{p.get('label', '')} | {p.get('source', '')} {p.get('page', '')} | "
                     f"{p.get('kind', '')} | {p.get('topic', '')} | 답: {p.get('answer', '')}"
                     + (f" | {p.get('why')}" if p.get("picked") and p.get("why") else "") for p in rows)


def save_note(subject: str, note: str) -> None:
    STYLES.mkdir(parents=True, exist_ok=True)
    p = STYLES / f"{subject}.note.txt"
    note = (note or "").strip()[:4000]
    if note:
        p.write_text(note, encoding="utf-8")
    elif p.exists():
        p.unlink()


def quiz_prompt(subject: str, scope: str, n: int, level: str, qtype: str, guide: str | None = None,
                note: str = "", pool: list | None = None) -> str:
    style_block = ""
    if pool:
        style_block += f"""

## 문제 지도 (★ = 시험에 뽑힌 문제. 형식: 이름 | 위치 | 종류 | 주제 | 답 유무)
{pool_text(pool)}
- 범위 안에 ★ 문제가 있으면 그 문제들을 **먼저** 출제한다. 원문 그대로가 아니라 숫자·조건을 바꾼 변형으로 내고, 문제 끝에 "(연습문제 5 변형)"처럼 원래 문제를 밝힌다. 원래 문제는 위치를 보고 그 쪽만 읽는다.
- 남은 문제 수는 아래 스타일대로 강의자료에서 새로 낸다."""
    if guide or note:
        qtype_text = "이 과목 시험 스타일의 유형 비율대로" if qtype == "mix" else QTYPES.get(qtype, QTYPES["mix"])
        parts = []
        if note:
            parts.append(f"## 교수님 출제 스타일 (학생이 직접 적은 것 — 가장 우선한다)\n{note}")
        if guide:
            parts.append(f"## 시험 자료에서 분석한 스타일{' (위 메모와 다르면 메모를 따른다)' if note else ''}\n{guide}")
        style_block += "\n\n" + "\n\n".join(parts) + f"""

시험 스타일 출제 규칙 (반드시 따른다):
- 위 스타일의 유형 비율, 말투, 지시문, 소문항 구조, 배점 표시를 그대로 따라 **실제 시험지처럼** 만든다. 배점을 표시하는 과목이면 문제 앞에 "(10점)"처럼 붙인다.
- 유형: {qtype_text}. 시험에 객관식이 없는 과목이면 서술·계산형 위주로 낸다.
- 내용은 강의자료 범위에서 새로 만든다. 기출·모의고사 문항을 숫자만 바꿔 그대로 옮기지 않는다 (구조와 형식은 같아도 된다).
- 소문항이 있는 문제는 하나의 서술형(short)으로 만들고, answer에 소문항별 모범답안을 모두 쓴다."""
    return f"""[퀴즈 출제 — 앱 화면용, 결과는 JSON으로만]
과목: {subject} / 범위: {scope or "전체"} / 문제 수: {n} / 난이도: {level} ({LEVELS.get(level, LEVELS["보통"])}) / 유형: {"시험 스타일을 따름" if (guide or note) and qtype == "mix" else QTYPES.get(qtype, QTYPES["mix"])}{style_block}

출제 규칙:
1. 범위에 해당하는 강의자료 내용에서만 출제한다. 범위가 "전체"면 강의 전반에 고르게.
   읽는 순서(토큰 절약): `cache/목차/{subject}.md`로 파일을 고르고 → `python tools/digest.py "texts/{subject}/A.md" ...`(여러 개 한 번에, Bash timeout 600000)로 요약본을 받아 출제할 개념을 정한 뒤 → 문제로 낼 부분만 원문 texts에서 그 쪽만 Read(offset/limit)로 확인한다. 원문 파일 통째로 Read 금지 (digest.py가 거부되면 목차로 고른 쪽만 읽는다).
2. `python tools/srs.py stats {subject}`의 자주틀린주제가 범위 안에 있으면 1~2문제는 그 주제로.
3. 파일명에 solution·해설·답안·모의고사·문제지가 있는 자료는 출제 스타일과 자주 나오는 유형을 참고만 하고, 문제를 그대로 베끼지 않는다.
4. 수식은 LaTeX(`$...$`). `(🖼 ...)` 표시가 있는 쪽의 수식이 필요하면 render.py로 원본을 확인한다. 추측으로 수식을 만들지 않는다.
5. 객관식(mcq): choices에 보기 정확히 4개, answer_index는 정답 위치(0~3). 오답 보기는 학생이 실제로 헷갈릴 만한 오개념으로. "모두 정답/정답 없음" 보기는 쓰지 않는다. 정답 위치는 문제마다 골고루 섞는다. answer에는 정답 보기의 내용을 쓴다.
6. 서술형(short): 2~4문장이나 계산 결과로 답할 수 있게. choices는 [], answer_index는 -1. answer는 채점 기준이 되는 모범답안(꼭 들어가야 할 핵심 키워드 포함).
7. explanation: 왜 그게 정답인지 2~4문장. 흔한 실수가 있으면 함께.
8. source: 원본 파일명 + 위치, 예) `Lecture05_TFIDF_Part1.pdf p.12`, `보고서.docx part 4`, `2강.pptx slide 3`.
9. topic: 짧고 일관된 개념명 (예: "TF-IDF-정의").
10. basis — 답의 근거 (인터넷은 쓰지 않는다):
    - "해설": 해설·정답·solution 파일이나 강의의 풀이에 답이 있어 그것을 따랐다. 내가 푼 답과 다르면 해설을 따르고 차이를 explanation에 한 줄로 적는다.
    - "자료": 답은 없지만 강의자료의 정의·공식·비슷한 예제로 직접 풀었다. explanation에 근거가 된 쪽을 출처로 단다.
    - "근거 없음": 강의자료에서 풀이 근거를 찾지 못했다. 그래도 가장 그럴듯한 답을 쓰되 explanation 앞에 "⚠ 강의자료에서 근거를 찾지 못했어요."를 붙인다. (학생이 고르면 앱이 인터넷에서 다시 찾는다)
11. 복습 카드 저장, 확인 질문, 인사말은 하지 않는다. 문제는 정확히 {n}개."""


# 강의자료에서 근거를 못 찾은 문제(basis "근거 없음")를 학생이 고른 개수만큼 인터넷에서 다시 풀기
WEBSOLVE_SCHEMA = {"type": "object", "properties": {"items": {"type": "array", "items": {"type": "object", "properties": {
    "i": {"type": "integer"}, "answer": {"type": "string"}, "answer_index": {"type": "integer"},
    "explanation": {"type": "string"}, "basis": {"type": "string", "enum": ["자료", "인터넷"]},
    "sources": {"type": "array", "items": {"type": "string"}}, "confident": {"type": "boolean"}},
    "required": ["i", "answer", "answer_index", "explanation", "basis", "sources", "confident"]}}}, "required": ["items"]}


def websolve_prompt(subject: str, items: list[tuple[int, dict]]) -> str:
    blocks = []
    for i, q in items:
        ch = "\n".join(f"  {k}. {c}" for k, c in zip("ABCD", q.get("choices") or []))
        blocks.append(f"### 문제 {i}\n{q.get('question', '')}\n" + (f"보기:\n{ch}\n" if ch else "")
                      + f"(출제 때 쓴 답: {q.get('answer', '')} / 출처: {q.get('source', '')})")
    body = "\n\n".join(blocks)
    return f"""[인터넷으로 답 찾기 — 결과는 JSON(items)으로만]
과목: {subject}
아래 문제들은 퀴즈를 만들 때 강의자료에서 답의 근거를 찾지 못한 것이다. 학생이 인터넷으로 찾아보라고 골랐다.
문제마다:
1. 먼저 texts/{subject}/에서 Grep으로 한 번 더 찾는다 (핵심 용어를 한국어·영어로). 근거가 있으면 basis "자료", explanation에 쪽 출처를 단다.
2. 없으면 WebSearch로 찾는다 (필요하면 WebFetch로 그 페이지만). 대학 강의노트·교재·표준 문서처럼 믿을 만한 곳을 쓴다. 문제마다 검색은 2번 안팎.
   basis "인터넷", sources에 실제로 참고한 URL, explanation 끝에 "🌐 인터넷 참고 — 강의자료에는 없는 내용: 사이트 이름" 한 줄.
3. 객관식은 answer_index(0~3)와 answer(정답 보기의 내용), 서술형은 answer_index -1과 채점 기준이 되는 모범답안.
4. 찾아도 확신이 낮으면 confident false로 하고 explanation 앞에 "⚠ 검토 필요:"와 이유를 쓴다.
5. i는 아래 문제 번호 그대로. 수식은 LaTeX. 인사말 없이.

{body}"""


def clean_questions(raw: list[dict]) -> list[dict]:
    out = []
    for q in raw:
        if not q.get("question"):
            continue
        if q.get("type") == "mcq" and (len(q.get("choices") or []) != 4 or not 0 <= q.get("answer_index", -1) <= 3):
            q["type"] = "short"  # 보기가 깨진 객관식은 서술형으로
        if q["type"] == "short":
            q["choices"], q["answer_index"] = [], -1
        out.append(q)
    return out


def grade_answer(q: dict, user_answer: str) -> dict:
    prompt = f"""[서술형 채점 — 결과는 JSON으로만]
문제: {q.get("question", "")}
모범답안: {q.get("answer", "")}
해설: {q.get("explanation", "")}
학생 답: {user_answer}

채점 기준:
- correct: 핵심 개념이 맞음. 표현이 달라도 의미가 같으면 정답. 계산 문제는 최종 값이 맞으면 정답.
- partial: 방향은 맞지만 핵심 키워드나 조건이 일부 빠짐, 또는 계산 과정은 맞고 실수가 있음.
- wrong: 틀렸거나 핵심이 빠짐.
feedback: 존댓말 2~3문장. 학생 답에서 맞은 점과 틀린/빠진 점을 짚는다. 수식은 LaTeX($...$).
확인 질문, 복습 카드 저장 제안, "자료를 확인하지 않았다" 같은 말은 쓰지 않는다.
missing: 학생 답에 빠진 핵심 키워드 (없으면 빈 배열)."""
    # 채점은 문제·답만 보면 되니 도구 없이, CLAUDE.md도 안 읽히게 프로젝트 밖(임시 폴더)에서 실행
    try:
        data = engines.run_once("grade", prompt, schema=GRADE_SCHEMA, timeout=180)
    except (RuntimeError, TimeoutError, OSError) as exc:
        raise ValueError(str(exc))
    res = data.get("structured_output")
    if not res or res.get("verdict") not in ("correct", "partial", "wrong"):
        raise ValueError(str(data.get("result") or "채점 결과가 비어 있어요")[-500:])
    res.setdefault("feedback", "")
    res["missing"] = res.get("missing") or []
    return res


# ───────────────────────── 스튜디오 (정리 자료 만들기) ─────────────────────────
STUDIO = ROOT / "studio"
DOC_SCHEMA = {
    "type": "object",
    "properties": {"title": {"type": "string"}, "markdown": {"type": "string"}},
    "required": ["title", "markdown"],
}
STUDIO_TYPES = {
    "summary": ("시험대비 요약", """시험 직전에 보는 **요약 노트**를 만든다.
- 강의(파일) 순서대로 `## 강의/주차 제목` 섹션을 만들고, 각 섹션에 핵심 개념 3~7개를 bullet로. 개념마다 1~2문장 설명.
- 꼭 외워야 할 것은 ⭐, 학생들이 자주 틀리거나 헷갈리는 포인트는 ⚠ 로 표시.
- 비교가 필요한 개념(A vs B)은 작은 표로.
- 마지막에 `## 시험 전 체크리스트`: "□ ~를 설명할 수 있다" 형식으로 10~20개."""),
    "formulas": ("핵심 공식표", """**공식 정리표**를 만든다.
- 주제별 `## 소제목` 아래에 표: `| 공식 | 의미 · 언제 쓰나 | 조건 · 주의 | 출처 |`
- 공식은 LaTeX `$...$`. `(🖼 ...)` 표시가 있는 쪽의 수식은 반드시 render.py로 원본을 보고 옮긴다. 확인 못 한 수식은 넣지 않는다.
- 공식 사이의 관계(예: A에서 B 유도)가 중요하면 표 아래에 한두 줄로.
- 마지막에 `## 헷갈리기 쉬운 공식` (비슷하게 생긴 공식 비교)."""),
    "patterns": ("기출 유형 분석", """**출제 유형 분석**을 만든다.
- 근거 자료: 파일명에 solution·해설·답안·모의고사·문제지·기출·퀴즈가 있는 파일. 이 시험 자료는 문제 형식이 중요하니 요약본 대신 원문을 읽는다 (강의자료 쪽은 요약본으로 충분).
- `## 유형별 정리` 표: `| 유형 | 나온 횟수 | 대표 문제(요약) | 관련 강의 | 출처 |`
- `## 자주 나오는 함정`: 오답을 유도하는 포인트와 피하는 법.
- `## 배점·난이도 경향` (자료에 있을 때만).
- `## 대비 전략`: 유형별로 무엇을 연습해야 하는지, 우선순위 순서로.
- 이런 파일이 없으면 그 사실을 맨 위에 밝히고, 강의에서 강조된 부분(반복, '중요', 예제) 기준으로 예상 포인트를 정리한다."""),
    "glossary": ("용어집", """**용어집**을 만든다.
- 강의 순서대로 `## 강의 제목` 섹션, 각 섹션에 표: `| 용어 | 영어 | 한 줄 정의 | 출처 |`
- 영어 강의면 한국어 번역 용어를, 한국어 강의면 영어 원어를 함께.
- 마지막에 `## 헷갈리는 용어 비교`: 비슷한 용어 쌍을 표로 (차이점 한 줄)."""),
    "intro": ("입문 가이드", """이 과목을 **처음 듣는 사람**을 위한 입문 가이드를 만든다. 시험 요약이 아니라 "처음 읽어도 흐름이 잡히는" 설명서다.
- 맨 앞 `## 이 과목은 무엇을 배우나`: 과목 전체가 풀려는 질문을 3~5줄로, 강의들이 어떤 순서로 이어지는지 한 문단으로. 강의 간 연결은 mermaid 흐름도로 한 장.
- `## 미리 알고 가면 좋은 것`: 자료가 당연하게 쓰는 선수 개념(수학·용어 등)을 짧게. 자료에 설명이 없으면 "(강의자료 외 보충)" 표시.
- 강의(파일) 순서대로 `## 강의 제목` 섹션. 각 섹션은 이 순서로:
  1. **이 강의의 질문**: 왜 이걸 배우는지, 앞 강의에서 무엇이 부족해서 나왔는지 1~2문장.
  2. **핵심 개념**: 3~6개, 개념마다 `### 개념 이름` 소제목. 각 개념은
     - 소제목 바로 아래 `> **한 줄 요약:** …` (그 개념을 한 문장으로. 이것만 읽어도 뜻이 잡히게)
     - ① 쉬운 말로 직관 → ② 강의자료의 정확한 정의·수식 → ③ 자료에 있는 예제로 한 번 따라가기. 순서를 지킨다.
  3. 새 용어는 처음 나올 때 **굵게** + 영어 병기 + 한 줄 뜻. 한 번 설명한 용어는 다시 풀지 않는다.
  4. 수식은 각 기호가 무엇인지 식 바로 아래에 풀어 쓰고, 식이 말하는 바를 한 문장으로.
  5. **처음 배울 때 흔한 오해** ⚠: 1~2개 (자료에서 강조·주의한 부분 위주).
  6. **스스로 확인**: 질문 2~3개 (답은 문서 맨 끝 `## 확인 질문 답`에 모은다). 공통 규칙의 "확인 질문 쓰지 않기"는 학생에게 되묻는 말을 뜻하니, 문서 내용인 이 질문은 넣는다.
- **그림을 적극적으로 쓴다** (공통 규칙의 "절당 1개" 제한보다 이 기준이 우선):
  - 강의 섹션마다 최소 1개, 많으면 2~3개. 단, 모든 개념에 넣지는 않는다. 구조·흐름·단계·비교·그래프·회로처럼 글만으로는 머릿속에 그려야 하는 개념에만 넣는다. 정의·용어 설명에는 넣지 않는다.
  - 우선순위: ① 강의자료에 그 그림이 있으면 원본 쪽 `[[원본: 파일.pdf p.N | 무엇을 볼지]]` (목차의 🖼 쪽을 먼저 살핀다) → ② 원본에 없거나 원본이 복잡하면 mermaid로 직접 그린다 (흐름도·상태도·계층도·순서도). 직접 그린 그림은 자료 내용만으로 그린다.
  - 그림 바로 아래에 "그림에서 볼 것" 한두 줄 (어디를 보고 무엇을 이해하면 되는지).
- 비유는 이해에 꼭 도움이 될 때만, 개념당 최대 하나, "(강의자료 외 보충)" 표시. 비유가 정의를 대신하지 않게 한다.
- 시험 팁·⭐ 외울 것·체크리스트는 넣지 않는다 (그건 시험대비 요약의 몫).
- 마지막 `## 전체 흐름 다시 보기`: 강의별로 한 줄씩, "A를 배워서 → B가 가능해지고 → C로 확장" 식으로 이어서."""),
}


def studio_prompt(subject: str, kind: str, scope: str, custom: str) -> str:
    if kind == "custom":
        task = f"아래 요청대로 정리 자료를 만든다.\n요청: {custom}"
    else:
        task = STUDIO_TYPES[kind][1]
    return f"""[스튜디오 — 정리 자료 만들기, 결과는 JSON(title, markdown)으로만]
과목: {subject} / 범위: {scope or "전체"}

{task}

공통 규칙:
- **토큰 절약 — 읽는 순서:** ① `cache/목차/{subject}.md`로 범위에 맞는 파일·쪽을 고른다 → ② 그 파일들의 요약본을 `python tools/digest.py "texts/{subject}/A.md" "texts/{subject}/B.md"`로 한 번에 받는다 (Bash timeout 600000. 처음엔 만드느라 걸리고 다음부턴 바로 나온다) → ③ 요약본으로 부족한 곳(수식 원문, 정확한 정의·표·예제)만 원문 texts를 **그 쪽만** Read(offset/limit)로 읽는다. 원문 파일을 통째로 Read하지 않는다. (digest.py 실행이 거부되면 요약본 없이 ①의 쪽만 골라 읽는다)
- 범위에 해당하는 파일은 빠짐없이 다루고, 자료 내용만으로 만든다. 자료에 없는 내용은 넣지 않는다 (꼭 필요하면 "(강의자료 외 보충)" 표시).
- 핵심 문장·표의 행마다 출처를 단다. 형식: `(Lecture05_TFIDF_Part1.pdf p.12)`, `(보고서.docx part 4)`, `(2강.pptx slide 3)`.
- 수식은 LaTeX(`$...$`, 따로 보일 식은 `$$...$$`).
- 그림: `.claude/skills/figure/SKILL.md`를 먼저 Read하고 그 기준을 따른다. 절마다 핵심 개념이 구조·흐름·그래프·회로처럼 글로는 머릿속에 그려야 하는 것일 때만 넣는다 (원본 쪽 `[[원본: 파일.pdf p.N | 설명]]` 우선, 없으면 mermaid). 문서 전체에 많아야 절당 1개.
- markdown은 바로 인쇄해도 될 완성본으로: 맨 위에 `#` 제목 없이 `##`부터 시작. 인사말, "확인할게요" 같은 진행 멘트, 확인 질문, 카드 저장 제안은 쓰지 않는다.
- title: 짧게, 예) "{subject} {STUDIO_TYPES[kind][0] if kind in STUDIO_TYPES else "정리"} ({scope or "전체"})"."""


def studio_list(subject: str | None) -> list[dict]:
    docs = []
    if not STUDIO.exists():
        return docs
    for f in STUDIO.glob("*/*.md"):
        if subject and f.parent.name != subject:
            continue
        meta = {"title": f.stem, "type": "custom", "scope": "", "created": ""}
        head = f.read_text(encoding="utf-8").split("\n---\n", 1)[0]
        for line in head.splitlines()[1:]:
            k, _, v = line.partition(": ")
            if k in meta:
                meta[k] = v
        docs.append({**meta, "subject": f.parent.name, "path": f.relative_to(STUDIO).as_posix()})
    return sorted(docs, key=lambda d: d["created"], reverse=True)


def studio_path(rel: str) -> Path:
    p = (STUDIO / rel).resolve()
    if not p.is_relative_to(STUDIO.resolve()) or p.suffix != ".md" or not p.is_file():
        raise ValueError(f"자료를 찾을 수 없어요: {rel}")
    return p


def studio_save(subject: str, kind: str, scope: str, title: str, markdown: str) -> str:
    folder = STUDIO / subject
    folder.mkdir(parents=True, exist_ok=True)
    now = time.localtime()
    path = folder / f"{time.strftime('%Y%m%d-%H%M%S', now)}_{kind}.md"
    one_line = lambda s: re.sub(r"\s+", " ", s).strip()  # noqa: E731
    path.write_text(f"---\ntitle: {one_line(title)}\ntype: {kind}\nscope: {one_line(scope)}\n"
                    f"created: {time.strftime('%Y-%m-%d %H:%M', now)}\n---\n{markdown.strip()}\n", encoding="utf-8")
    return path.relative_to(STUDIO).as_posix()


# ───────────────────────── 삭제 = Windows 휴지통으로 ─────────────────────────
def recycle(paths: list[Path]) -> None:
    """파일·폴더를 Windows 휴지통으로 보낸다 (휴지통에서 '복원'하면 원래 자리로 돌아감).
    Windows 셸 API(SHFileOperationW + FOF_ALLOWUNDO)를 써서 추가 설치가 필요 없다."""
    import ctypes
    from ctypes import wintypes

    paths = [p for p in paths if p.exists()]
    if not paths:
        return

    class SHFILEOPSTRUCTW(ctypes.Structure):
        _fields_ = [("hwnd", wintypes.HWND), ("wFunc", wintypes.UINT), ("pFrom", wintypes.LPCWSTR),
                    ("pTo", wintypes.LPCWSTR), ("fFlags", ctypes.c_ushort), ("fAnyOperationsAborted", wintypes.BOOL),
                    ("hNameMappings", ctypes.c_void_p), ("lpszProgressTitle", wintypes.LPCWSTR)]

    FO_DELETE, FOF_SILENT, FOF_NOCONFIRMATION, FOF_ALLOWUNDO, FOF_NOERRORUI = 3, 0x4, 0x10, 0x40, 0x400
    # 여러 경로는 \0 으로 잇고 끝은 \0\0 (API 규칙)
    buf = ctypes.create_unicode_buffer("\0".join(str(p.resolve()) for p in paths) + "\0")
    op = SHFILEOPSTRUCTW(None, FO_DELETE, ctypes.cast(buf, wintypes.LPCWSTR), None,
                         FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_SILENT | FOF_NOERRORUI, False, None, None)
    rc = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
    if rc != 0 or op.fAnyOperationsAborted or any(p.exists() for p in paths):
        raise ValueError(f"휴지통으로 옮기지 못했어요 (코드 {rc}). 탐색기나 다른 프로그램에서 파일이 열려 있으면 닫고 다시 해주세요.")


def subject_dirs(name: str) -> dict[str, Path]:
    return {"materials": MATERIALS / name, "texts": TEXTS / name, "studio": STUDIO / name,
            "cache_hd": CACHE_HD / name, "cache_pages": ROOT / "cache" / "pages" / name}


def subject_summary(name: str) -> dict:
    d = subject_dirs(name)
    files = [f for f in d["materials"].rglob("*") if f.is_file()] if d["materials"].exists() else []
    docs = list(d["studio"].glob("*.md")) if d["studio"].exists() else []
    with srs_lock:
        cards = sum(1 for c in srs.load() if c["subject"] == name)
    return {"name": name, "files": len(files), "cards": cards, "docs": len(docs)}


def subject_delete(name: str) -> dict:
    """과목을 Windows 휴지통으로 보낸다: 강의자료·추출 텍스트·스튜디오 자료·시험 스타일, 그리고 복습 카드 백업.
    쪽 이미지 캐시는 언제든 다시 만들 수 있어서 바로 지운다."""
    if not (MATERIALS / name).exists() and not any(c["subject"] == name for c in srs.load()):
        raise ValueError(f"없는 과목이에요: {name}")
    d = subject_dirs(name)
    with srs_lock:
        cards = srs.load()
        gone = [c for c in cards if c["subject"] == name]
        backup = None
        if gone:  # 카드 백업을 자료 폴더 안에 두면 폴더를 복원할 때 같이 돌아온다
            d["materials"].mkdir(parents=True, exist_ok=True)
            backup = d["materials"] / f"{name} 복습카드 백업.json"
            backup.write_text(json.dumps(gone, ensure_ascii=False, indent=1), encoding="utf-8")
        try:
            recycle([d["materials"], d["texts"], d["studio"], STYLES / f"{name}.json", STYLES / f"{name}.note.txt"])
        except ValueError:
            if backup and backup.exists():
                backup.unlink()
            raise
        if gone:
            srs.save([c for c in cards if c["subject"] != name])
    for key in ("cache_hd", "cache_pages"):
        shutil.rmtree(d[key], ignore_errors=True)
    return {"ok": True, "cards": len(gone)}


def with_context(message: str, context: list[dict]) -> str:
    """이어가기가 안 될 때, 화면에 저장돼 있던 최근 대화를 새 대화 앞에 붙인다 (메시지당 1,500자, 최근 12개)."""
    lines = []
    for m in context[-12:]:
        who = {"user": "학생", "bot": "튜터"}.get(m.get("role"))
        if who and m.get("text"):
            lines.append(f"[{who}] {str(m['text'])[:1500]}")
    if not lines:
        return message
    return ("[앞선 대화 일부 — 앱에 저장된 기록. 이 맥락을 이어서 답한다]\n" + "\n\n".join(lines)
            + "\n\n[이어지는 질문]\n" + message)


# ───────────────────────── 자료 폴더 ↔ 추출 텍스트 맞추기 ─────────────────────────
sync_lock = threading.Lock()


def materials_out_of_sync() -> bool:
    """탐색기로 직접 넣은 파일(추출 안 됨·원본이 더 새것)이나 지운 파일(추출본만 남음)이 있으면 True."""
    received = onedrive_sync.received_files()
    for src in MATERIALS.rglob("*") if MATERIALS.exists() else []:
        if src.suffix.lower() in ALLOWED_EXT:
            md = TEXTS / src.relative_to(MATERIALS).with_suffix(".md")
            # 탐색기로 복사한 파일은 수정 시각이 옛날 그대로라, 복사된 시각(ctime)도 함께 본다.
            # OneDrive에서 받아 온 자료는 예외: 받을 때 ctime이 지금으로 바뀌지만 추출본도 같이 받아 오므로 수정 시각만 본다
            st = src.stat()
            rel = "materials/" + src.relative_to(MATERIALS).as_posix()
            got = onedrive_sync.same(received.get(rel), (st.st_size, int(st.st_mtime)))
            if not md.exists() or md.stat().st_mtime < (st.st_mtime if got else max(st.st_mtime, st.st_ctime)):
                return True
    for md in TEXTS.rglob("*.md") if TEXTS.exists() else []:
        rel = md.relative_to(TEXTS)
        if not any((MATERIALS / rel).with_suffix(ext).exists() for ext in ALLOWED_EXT):
            return True
    return False


def sync_materials() -> bool:
    """자료와 추출 텍스트가 어긋나 있으면 extract.py로 맞춘다 (새 파일 추출 + 지운 파일의 추출본 정리). 바뀌었으면 True."""
    with sync_lock:
        if not materials_out_of_sync():
            return False
        run_tool("extract.py")
        return True


def stop_claude(slot: str | None = None) -> None:
    """slot을 주면 그 칸만, 없으면 전부 멈춘다."""
    with state_lock:
        targets = [p for k, p in procs.items() if slot in (None, k)]
    for proc in targets:
        if proc.poll() is None:
            # claude가 띄운 하위 프로세스까지 함께 종료
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True, creationflags=NO_WINDOW)


# ───────────────────────── iClass 자료 받기 (tools/iclass.py) ─────────────────────────
iclass_lock = threading.Lock()
iclass_proc: subprocess.Popen | None = None


def iclass_login(user: str, password: str) -> dict:
    """비밀번호는 표준입력으로만 넘기고 저장하지 않는다. 성공하면 iclass.py가 로그인 쿠키만 data/에 저장."""
    if not user.strip() or not password:
        raise ValueError("학번과 비밀번호를 모두 넣어주세요")
    r = subprocess.run([PYTHON, str(TOOLS / "iclass.py"), "login"], cwd=ROOT, input=f"{user.strip()}\n{password}\n",
                       capture_output=True, text=True, encoding="utf-8", errors="replace", creationflags=NO_WINDOW, timeout=90)
    out = (r.stdout + r.stderr).strip().splitlines()
    return {"ok": r.returncode == 0, "message": out[-1] if out else "응답이 없어요"}


def stop_iclass() -> None:
    proc = iclass_proc
    if proc and proc.poll() is None:
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True, creationflags=NO_WINDOW)


# ───────────────────────── HTTP ─────────────────────────
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # 콘솔 로그 끔
        pass

    def _trusted(self) -> bool:
        # 다른 웹사이트가 이 서버를 몰래 호출하지 못하게: Host 확인 + 사용자 정의 헤더 요구
        host_ok = self.headers.get("Host") in {f"127.0.0.1:{PORT}", f"localhost:{PORT}"}
        return host_ok and (self.command == "GET" or self.headers.get("X-Study-App") == "1")

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> bytes:
        return self.rfile.read(int(self.headers.get("Content-Length") or 0))

    def do_GET(self):
        global last_seen
        if not self._trusted():
            return self._json({"error": "forbidden"}, 403)
        last_seen = time.time()
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            body = (STATIC / "index.html").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
        elif path.startswith("/static/"):  # 화면 라이브러리·글꼴 (tools/static/vendor, 인터넷 없이도 화면이 뜨게)
            f = (STATIC / path[len("/static/"):]).resolve()
            if not f.is_file() or not f.is_relative_to(STATIC.resolve()):
                return self._json({"error": "not found"}, 404)
            body = f.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", STATIC_TYPES.get(f.suffix.lower(), "application/octet-stream"))
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "private, max-age=86400")
            self.end_headers()
            self.wfile.write(body)
        elif path == "/api/state":
            self._json(app_state())
        elif path == "/api/ping":
            self._json({"ok": True, "stale": code_version() > STARTED_VERSION})
        elif path == "/api/review":
            q = parse_qs(urlparse(self.path).query)
            with srs_lock:
                self._json({"cards": srs.due_cards(q["subject"][0] if q.get("subject") else None)})
        elif path == "/api/style":
            try:
                info = style_info(safe_name(parse_qs(urlparse(self.path).query)["subject"][0]))
                info.pop("sig", None)
                self._json(info)
            except (ValueError, KeyError) as exc:
                self._json({"error": str(exc)}, 400)
        elif path == "/api/ai":  # AI 설정 창: 엔진·요금제·기능별 모델·설치/로그인 상태
            if parse_qs(urlparse(self.path).query).get("refresh"):
                for e in engines.ENGINES:
                    engines.list_models(e, refresh=True)
            self._json(engines.summary())
        elif path in ("/api/chats", "/api/asks"):
            self._json({"chats": chat_list(TALKS[path.split("/")[2]])})
        elif path == "/api/weak/check":  # 약점 분석에 쓸 기록이 있는지 (복습 카드·질문하기·질문 창)
            import studylog
            subj = parse_qs(urlparse(self.path).query).get("subject", [""])[0]
            self._json(studylog.counts(subj))
        elif path == "/api/weak/log":  # /weak 요청에 붙여 보낼 공부 기록 (앱의 Claude가 스크립트를 따로 돌리지 않게)
            import studylog
            self._json({"log": studylog.render(parse_qs(urlparse(self.path).query).get("subject", [""])[0])})
        elif path in ("/api/chats/get", "/api/asks/get"):
            try:
                p = chat_path(parse_qs(urlparse(self.path).query).get("id", [""])[0], TALKS[path.split("/")[2]])
                self._json(json.loads(p.read_text(encoding="utf-8")))
            except (ValueError, OSError) as exc:
                self._json({"error": f"대화 기록을 열지 못했어요: {exc}"}, 404)
        elif path == "/api/quizzes":
            self._json({"quizzes": quiz_list()})
        elif path == "/api/reviews":
            self._json({"reviews": review_list()})
        elif path in ("/api/quizzes/get", "/api/reviews/get"):
            try:
                p = record_path(path.split("/")[2], parse_qs(urlparse(self.path).query).get("id", [""])[0])
                self._json(json.loads(p.read_text(encoding="utf-8")))
            except (ValueError, OSError) as exc:
                self._json({"error": f"기록을 열지 못했어요: {exc}"}, 404)
        elif path == "/api/studio":
            q = parse_qs(urlparse(self.path).query)
            self._json({"docs": studio_list(q["subject"][0] if q.get("subject") else None)})
        elif path == "/api/studio/doc":
            try:
                text = studio_path(parse_qs(urlparse(self.path).query)["path"][0]).read_text(encoding="utf-8")
                self._json({"markdown": text.split("\n---\n", 1)[-1]})
            except (ValueError, KeyError) as exc:
                self._json({"error": str(exc)}, 404)
        elif path in ("/api/source", "/api/page", "/api/section", "/api/pageimg"):
            q = {k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()}
            try:
                if path == "/api/pageimg":  # 답변 속 [[원본: 파일.pdf p.N]] 그림: 파일명으로 찾아서 그 쪽 이미지
                    src = find_source(q["name"], q.get("subject"))
                    if src is None or src.suffix.lower() != ".pdf":
                        raise ValueError(f"PDF 자료가 아니에요: {q['name']}")
                    q["path"] = src.relative_to(MATERIALS).as_posix()
                elif path == "/api/source":
                    return self._json(source_info(q["name"], q.get("subject")))
                if path == "/api/section":
                    secs = text_sections(material_path(q["path"]))
                    if q.get("n"):
                        n = int(q["n"])
                    else:  # 번호 없이 "3장 CDF 부분"처럼 적힌 출처 → 단어가 가장 많이 겹치는 구간
                        n = best_section(secs, q.get("q", ""))
                    if not 1 <= n <= len(secs):
                        raise ValueError(f"{n}번 구간이 없어요 (전체 {len(secs)}개)")
                    # Word는 제목마다 잘게 나뉘어 한 구간이 몇 줄뿐이라, 뒤 구간을 이어 붙여 맥락을 보여준다
                    more, size, i = [], len(secs[n - 1][1]), n
                    while i < len(secs) and size < 1800 and len(more) < 8:
                        more.append({"title": secs[i][0], "text": secs[i][1]})
                        size += len(secs[i][1])
                        i += 1
                    return self._json({"title": secs[n - 1][0], "text": secs[n - 1][1], "more": more, "count": len(secs)})
                body = render_page(q["path"], int(q["page"]))
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "private, max-age=3600")
                self.end_headers()
                self.wfile.write(body)
            except (ValueError, KeyError) as exc:
                self._json({"error": str(exc)}, 404)
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self):
        global last_seen
        if not self._trusted():
            return self._json({"error": "forbidden"}, 403)
        last_seen = time.time()
        url = urlparse(self.path)
        if url.path in AI_PATHS and not online():
            self._body()  # 보낸 내용은 읽고 버린다 (안 읽으면 연결이 끊긴 것처럼 보일 수 있음)
            return self._json({"error": OFFLINE_MSG, "offline": True}, 503)
        try:
            if url.path == "/api/chat":
                return self._chat()
            if url.path == "/api/shutdown":  # launch.pyw가 코드가 바뀐 예전 서버를 내릴 때
                stop_claude()
                self._json({"ok": True})
                threading.Thread(target=self.server.shutdown, daemon=True).start()
                return
            if url.path == "/api/stop":  # kind: chat(질문 답변 + iClass 받기) / ask(질문 창) / quiz / studio / 없으면 전부
                kind = (json.loads(self._body() or b"{}") or {}).get("kind")
                stop_claude(kind)
                if kind in (None, "chat"):
                    stop_iclass()
                return self._json({"ok": True})
            if url.path == "/api/iclass/login":
                data = json.loads(self._body())
                r = iclass_login(str(data.get("user", "")), str(data.get("password", "")))
                data.clear()  # 비밀번호를 메모리에 오래 두지 않는다
                return self._json(r, 200 if r["ok"] else 401)
            if url.path == "/api/iclass/sync":
                return self._iclass_sync()
            if url.path == "/api/ai":  # AI 설정 저장
                data = json.loads(self._body())
                engines.save(data)
                data.clear()  # API 키를 메모리에 오래 두지 않는다
                return self._json({"ok": True, "ai": engines.current_name()})
            if url.path == "/api/onedrive":  # OneDrive 동기화 켜기/끄기
                return self._json(set_onedrive(bool(json.loads(self._body() or b"{}").get("enabled"))))
            if url.path == "/api/ai/login":  # 로그인 창(새 콘솔) 띄우기
                engine = json.loads(self._body()).get("engine")
                if engine not in engines.ENGINES:
                    raise ValueError(f"알 수 없는 엔진: {engine}")
                engines.open_login(engine)
                return self._json({"ok": True})
            if url.path == "/api/grade":
                data = json.loads(self._body())
                if data["button"] not in srs.BUTTONS:
                    raise ValueError(f"알 수 없는 버튼: {data['button']}")
                with srs_lock:
                    card = srs.grade(int(data["id"]), srs.BUTTONS[data["button"]])
                return self._json({"ok": True, "due": card["due"], "interval": card["interval"]})
            if url.path == "/api/quiz/websolve":  # 근거 없는 문제를 학생이 고른 개수만큼 인터넷으로
                return self._quiz_websolve()
            if url.path == "/api/quiz/generate":
                return self._quiz_generate()
            if url.path == "/api/studio/generate":
                return self._studio_generate()
            if url.path == "/api/studio/delete":
                recycle([studio_path(json.loads(self._body())["path"])])
                return self._json({"ok": True})
            if url.path == "/api/quiz/grade":
                data = json.loads(self._body())
                try:
                    return self._json(grade_answer(data["question"], data["user_answer"]))
                except ValueError as exc:
                    return self._json({"error": f"채점 실패: {exc}"}, 502)
            if url.path == "/api/quiz/save":
                data = json.loads(self._body())
                items = [{**it, "subject": data["subject"], "lapses": 1} for it in data["items"]]
                with srs_lock:
                    ids = srs.add_cards(items)
                return self._json({"ok": True, "ids": ids})
            if url.path == "/api/card/delete":
                with srs_lock:
                    srs.delete(int(json.loads(self._body())["id"]))
                return self._json({"ok": True})
            if url.path == "/api/subject":
                name = safe_name(json.loads(self._body())["name"])
                (MATERIALS / name).mkdir(parents=True, exist_ok=True)
                return self._json({"ok": True})
            if url.path == "/api/style/note":  # 교수님 출제 스타일 메모 저장 (빈 글이면 삭제)
                data = json.loads(self._body())
                save_note(safe_name(data["subject"]), data.get("note", ""))
                return self._json({"ok": True})
            if url.path == "/api/style/reset":  # 다음 시험 스타일 퀴즈 때 다시 분석
                p = STYLES / f"{safe_name(json.loads(self._body())['subject'])}.json"
                if p.exists():
                    p.unlink()
                return self._json({"ok": True})
            talk = url.path.split("/")[2] if url.path.startswith(("/api/chats/", "/api/asks/")) else None
            if talk and url.path.endswith("/save"):
                return self._json(chat_save(json.loads(self._body()), TALKS[talk]))
            if talk and url.path.endswith("/rename"):
                data = json.loads(self._body())
                p = chat_path(data["id"], TALKS[talk])
                rec = json.loads(p.read_text(encoding="utf-8"))
                rec["title"] = re.sub(r"\s+", " ", str(data["title"])).strip()[:80] or rec["title"]
                p.write_text(json.dumps(rec, ensure_ascii=False, indent=1), encoding="utf-8")
                return self._json({"ok": True, "title": rec["title"]})
            if talk and url.path.endswith("/delete"):
                recycle([chat_path(json.loads(self._body())["id"], TALKS[talk])])
                return self._json({"ok": True})
            if url.path == "/api/quizzes/save":
                return self._json(quiz_save(json.loads(self._body())))
            if url.path == "/api/reviews/save":
                return self._json(review_save(json.loads(self._body())))
            if url.path in ("/api/quizzes/rename", "/api/reviews/rename"):
                data = json.loads(self._body())
                p = record_path(url.path.split("/")[2], data["id"])
                rec = json.loads(p.read_text(encoding="utf-8"))
                rec["title"] = re.sub(r"\s+", " ", str(data["title"])).strip()[:80] or rec["title"]
                write_record(p, rec)
                return self._json({"ok": True, "title": rec["title"]})
            if url.path in ("/api/quizzes/delete", "/api/reviews/delete"):
                recycle([record_path(url.path.split("/")[2], json.loads(self._body())["id"])])
                return self._json({"ok": True})
            if url.path == "/api/subject/order":
                save_order(json.loads(self._body())["order"])
                return self._json({"ok": True})
            if url.path == "/api/subject/summary":
                return self._json(subject_summary(safe_name(json.loads(self._body())["name"])))
            if url.path == "/api/subject/delete":
                if is_busy():
                    return self._json({"error": "다른 작업이 진행 중이에요. 끝난 뒤에 지워주세요."}, 409)
                return self._json(subject_delete(safe_name(json.loads(self._body())["name"])))
            if url.path == "/api/open-file":
                os.startfile(material_path(parse_qs(url.query)["path"][0]))
                return self._json({"ok": True})
            if url.path == "/api/open":
                q = parse_qs(url.query)
                target = MATERIALS / safe_name(q["subject"][0]) if q.get("subject") else MATERIALS
                target.mkdir(parents=True, exist_ok=True)
                os.startfile(target)
                return self._json({"ok": True})
            if url.path == "/api/upload":
                q = parse_qs(url.query)
                subject = safe_name(q["subject"][0])
                fname = safe_name(Path(unquote(q["name"][0])).name)
                if Path(fname).suffix.lower() not in ALLOWED_EXT:
                    return self._json({"error": f"{fname}: PDF·PPTX·DOCX만 넣을 수 있어요 (.ppt/.hwp는 PDF로 저장해서 넣어주세요)"}, 400)
                dest = MATERIALS / subject / fname
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(self._body())
                with sync_lock:  # 백그라운드 맞추기와 동시에 같은 파일을 추출하지 않게
                    log = run_tool("extract.py", subject)
                return self._json({"ok": True, "log": log})
        except (ValueError, KeyError, json.JSONDecodeError) as exc:
            return self._json({"error": str(exc)}, 400)
        self._json({"error": "not found"}, 404)

    def _stream_start(self, slot: str) -> bool:
        if is_busy(slot):
            what = "퀴즈를" if slot == "quiz" else "스튜디오 자료를"
            self._json({"error": f"{what} 만드는 중이에요. 끝난 뒤에 다시 해주세요."}, 409)
            return False
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        return True

    def _sender(self, slot: str):
        state = {"alive": True}

        def send(obj):
            global last_seen
            last_seen = time.time()
            if not state["alive"]:
                return
            try:
                self.wfile.write((json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8"))
                self.wfile.flush()
            except OSError:  # 화면이 닫힘 → 진행 중인 작업도 중단
                state["alive"] = False
                stop_claude(slot)
        return send

    def _iclass_sync(self):
        """iclass.py sync를 돌리며 한 줄씩 보낸다. 끝나면 받은 자료를 텍스트로 추출.
        이벤트: line(진행) / need_login(로그인 필요) / result(받은 결과) / error / done"""
        global iclass_proc
        subjects = [safe_name(s) for s in json.loads(self._body() or b"{}").get("subjects", [])]
        if not iclass_lock.acquire(blocking=False):
            return self._json({"error": "iClass에서 이미 받는 중이에요"}, 409)
        try:
            self.send_response(200)
            self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()
            send = self._sender("iclass")  # 화면이 닫혀도 Claude 작업은 건드리지 않음
            got, subj, blocked = {}, None, []
            try:
                iclass_proc = subprocess.Popen([PYTHON, str(TOOLS / "iclass.py"), "sync", *subjects], cwd=ROOT,
                                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                               encoding="utf-8", errors="replace", creationflags=NO_WINDOW)
                for raw in iclass_proc.stdout:
                    line = raw.rstrip()
                    if not line:
                        continue
                    if m := re.match(r"^\[(.+?)\] ", line):
                        subj = m.group(1)
                    elif line.startswith("  + ") and subj:
                        got.setdefault(subj, []).append(line[4:])
                    elif line.startswith("  ! ") and subj:
                        blocked.append(f"{subj}: {line[4:].split(' (')[0]}")
                    send({"type": "line", "text": line})
                code = iclass_proc.wait()
                if code == 3:
                    send({"type": "need_login"})
                elif code != 0:
                    send({"type": "error", "message": "iClass에서 받다가 문제가 생겼어요 (위 기록 참고)"})
                else:
                    if got:
                        send({"type": "line", "text": "받은 자료를 읽는 중 (텍스트 추출)…"})
                        with sync_lock:
                            run_tool("extract.py")
                    send({"type": "result", "got": got, "blocked": blocked})
            except Exception as exc:
                send({"type": "error", "message": f"iClass 받기 실패: {exc}"})
            finally:
                iclass_proc = None
            send({"type": "done"})
        finally:
            iclass_lock.release()
        self.close_connection = True

    def _quiz_generate(self):
        data = json.loads(self._body())
        subject = safe_name(data["subject"])
        n = max(1, min(int(data.get("n", 10)), 20))
        if not self._stream_start("quiz"):
            return
        sync_materials()
        send = self._sender("quiz")
        result = {}

        def forward(ev):  # 출제 중에는 진행 상황(도구 사용)만 보여주고 중간 글은 숨긴다
            if ev["type"] not in ("text", "done", "session"):
                send(ev)
        try:
            guide, note, pool = None, "", []
            if data.get("style", "exam") == "exam":
                info = style_info(subject)
                note = info["note"]
                guide = info["guide"] if info["has_exam"] else None  # 시험 자료를 지웠으면 예전 분석은 쓰지 않는다
                pool = info["pool"] if info["has_exam"] else []
                if info["has_exam"] and (info["stale"] or not guide):
                    # 처음이거나 시험 자료가 바뀌었으면 스타일부터 분석해서 저장 (강의 속 예제·연습문제도 찾아 문제 지도로)
                    send({"type": "tool", "label": f"시험 스타일·문제 지도 분석 중 (시험·문제 파일 {len(info['files'])}개 + 강의 속 예제, 처음 한 번)"})
                    sres = {}
                    stream_claude(style_prompt(subject, info["files"], note), None, forward,
                                  STYLE_SCHEMA, on_result=sres.update, slot="quiz", kind="style")
                    so = sres.get("structured_output") or {}
                    if so.get("guide"):
                        guide, pool = so["guide"], so.get("pool") or []
                        save_style(subject, guide, info["sig"], pool)
                        picked = sum(1 for p in pool if p.get("picked"))
                        send({"type": "tool", "label": f"시험 스타일 저장함 (문제 지도 {len(pool)}개, 시험에 뽑힌 문제 {picked}개) → 문제 만드는 중"})
                send({"type": "style", "used": bool(guide or note), "files": len(info["files"]), "note": bool(note)})
            prompt = quiz_prompt(subject, (data.get("scope") or "").strip(), n, data.get("level", "보통"),
                                 data.get("qtype", "mix"), guide, note, pool)
            stream_claude(prompt, None, forward, QUIZ_SCHEMA, on_result=result.update, slot="quiz")
            qs = clean_questions((result.get("structured_output") or {}).get("questions") or [])
            if qs:
                # 화면을 닫거나 다른 퀴즈를 보고 있어도 남도록 서버에서 바로 기록한다
                cfg = {k: data.get(k) for k in ("subject", "scope", "n", "level", "qtype", "style")}
                qs = [{**q, "user": None, "verdict": None} for q in qs]
                saved = quiz_save({"cfg": cfg, "qs": qs, "idx": 0, "phase": "answer", "styleUsed": bool(guide or note)})
                send({"type": "quiz", "questions": qs, "id": saved["id"], "title": saved["title"]})
            elif not result.get("is_error"):
                send({"type": "error", "message": "문제를 만들지 못했어요. 범위를 바꿔서 다시 해보세요."})
        except Exception as exc:
            send({"type": "error", "message": f"출제 실패: {exc}"})
            send({"type": "done"})
        self.close_connection = True

    def _quiz_websolve(self):
        """퀴즈에서 '근거 없음' 문제 중 학생이 고른 것만 인터넷 검색을 켜고 다시 푼다. 결과는 퀴즈 기록에도 반영"""
        data = json.loads(self._body())
        path = record_path("quizzes", str(data.get("id", "")))
        rec = json.loads(path.read_text(encoding="utf-8"))
        qs = rec.get("qs") or []
        idxs = sorted({int(i) for i in data.get("idxs", []) if 0 <= int(i) < len(qs)})[:10]
        if not idxs:
            return self._json({"error": "찾을 문제가 없어요"}, 400)
        subject = safe_name((rec.get("cfg") or {}).get("subject", ""))
        if not self._stream_start("quiz"):
            return
        send = self._sender("quiz")
        result = {}

        def forward(ev):
            if ev["type"] not in ("text", "done", "session"):
                send(ev)
        try:
            stream_claude(websolve_prompt(subject, [(i, qs[i]) for i in idxs]), None, forward, WEBSOLVE_SCHEMA,
                          on_result=result.update, slot="quiz", kind="websolve", web=True)
            got = (result.get("structured_output") or {}).get("items") or []
            rec = json.loads(path.read_text(encoding="utf-8"))  # 그동안 푼 진행 상황이 저장됐을 수 있어 다시 읽는다
            updated = {}
            for it in got:
                i = it.get("i")
                if i not in idxs or i >= len(rec.get("qs", [])):
                    continue
                q = rec["qs"][i]
                q["answer"], q["explanation"] = str(it.get("answer", q.get("answer", ""))), str(it.get("explanation", ""))
                q["basis"] = it.get("basis") if it.get("basis") in ("자료", "인터넷") else "인터넷"
                q["sources"] = [u for u in (it.get("sources") or []) if str(u).startswith(("http://", "https://"))][:5]
                q["unsure"] = not it.get("confident", True)
                if q.get("type") == "mcq" and 0 <= int(it.get("answer_index", -1)) <= 3:
                    q["answer_index"] = int(it["answer_index"])
                updated[i] = {k: q.get(k) for k in ("answer", "answer_index", "explanation", "basis", "sources", "unsure")}
            if updated:
                tmp = path.with_suffix(".tmp")
                tmp.write_text(json.dumps(rec, ensure_ascii=False, indent=1), encoding="utf-8")
                tmp.replace(path)
            send({"type": "websolve", "updated": updated})
            if not updated and not result.get("is_error"):
                send({"type": "error", "message": "답을 찾지 못했어요. 나중에 다시 해보세요."})
        except Exception as exc:
            send({"type": "error", "message": f"인터넷 찾기 실패: {exc}"})
        self.close_connection = True

    def _studio_generate(self):
        data = json.loads(self._body())
        subject = safe_name(data["subject"])
        kind = data.get("kind", "summary")
        if kind not in STUDIO_TYPES and kind != "custom":
            raise ValueError(f"알 수 없는 종류: {kind}")
        custom = (data.get("custom") or "").strip()
        if kind == "custom" and not custom:
            raise ValueError("만들 내용을 적어주세요")
        scope = (data.get("scope") or "").strip()
        if not self._stream_start("studio"):
            return
        sync_materials()
        send = self._sender("studio")
        result = {}

        def forward(ev):  # 진행 상황만 보여주고 중간 글은 숨긴다
            if ev["type"] != "text":
                send(ev)
        try:
            stream_claude(studio_prompt(subject, kind, scope, custom), None, forward,
                          DOC_SCHEMA, on_result=result.update, slot="studio")
            doc = result.get("structured_output") or {}
            if doc.get("markdown"):
                title = doc.get("title") or STUDIO_TYPES.get(kind, ("직접 요청",))[0]
                send({"type": "saved", "path": studio_save(subject, kind, scope or custom[:40], title, doc["markdown"]),
                      "title": title})
            elif not result.get("is_error"):
                send({"type": "error", "message": "자료를 만들지 못했어요. 범위를 좁혀서 다시 해보세요."})
        except Exception as exc:
            send({"type": "error", "message": f"만들기 실패: {exc}"})
            send({"type": "done"})
        self.close_connection = True

    def _chat(self):
        data = json.loads(self._body())
        message = (data.get("message") or "").strip()
        if not message:
            return self._json({"error": "빈 메시지"}, 400)
        # slot: chat(질문하기 화면) / ask(퀴즈·정리 자료에서 드래그해 여는 질문 창) — 서로 기다리지 않게 칸을 나눈다
        slot = "ask" if data.get("slot") == "ask" else "chat"
        if is_busy(slot):
            return self._json({"error": "이전 답변이 아직 진행 중이에요"}, 409)
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        alive = True

        def send(obj):
            nonlocal alive
            global last_seen
            last_seen = time.time()
            if not alive:
                return
            try:
                self.wfile.write((json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8"))
                self.wfile.flush()
            except OSError:  # 화면이 닫힘 → 진행 중인 작업도 중단
                alive = False
                stop_claude(slot)

        sync_materials()  # 탐색기로 넣거나 지운 자료를 먼저 반영
        session_id = data.get("session_id")
        engine = engines.load()["engine"]
        try:
            if session_id and engines.session_engine(session_id) != engine:
                # 다른 AI로 하던 대화: 이어갈 수 없으니 화면에 저장된 최근 대화를 붙여서 새로 시작
                send({"type": "tool", "label": f"{engines.current_name()}로 이어서 답해요 (최근 대화를 붙여 새로 시작)"})
                stream_claude(with_context(message, data.get("context") or []), None, send, slot=slot)
            elif not session_id:
                stream_claude(message, None, send, slot=slot)
            else:
                # 이어가기: 폴더를 옮겼거나 AI 쪽 대화가 사라졌으면 이어가기가 실패한다.
                # 실패를 바로 보여주지 않고, 화면에 저장된 최근 대화를 붙여서 새 대화로 다시 시작한다.
                held, got_text, result = [], {"v": False}, {}

                def guard(ev):
                    if ev["type"] == "text":
                        got_text["v"] = True
                    if not got_text["v"] and ev["type"] in ("error", "done"):
                        held.append(ev)
                        return
                    send(ev)
                stream_claude(message, session_id, guard, on_result=result.update, slot=slot)
                err = str(result.get("errors") or result.get("result") or "")
                # Claude는 "No conversation found"로 알 수 있고, 다른 엔진은 글 없이 실패하면 이어가기 실패로 본다
                # (로그인·한도 문제는 다시 해도 같은 오류가 나서 그대로 보인다)
                failed = not got_text["v"] and result.get("is_error") and (
                    "No conversation found" in err if engine == "claude"
                    else not re.search(r"login|auth|limit|quota|한도|로그인", err, re.I))
                if failed:
                    send({"type": "tool", "label": "이전 대화를 이어갈 수 없어서 최근 내용을 붙여 새로 시작해요"})
                    stream_claude(with_context(message, data.get("context") or []), None, send, slot=slot)
                else:
                    for ev in held:
                        send(ev)
        except Exception as exc:
            send({"type": "error", "message": f"AI 실행 실패: {exc}"})
            send({"type": "done", "session_id": session_id})
        self.close_connection = True


def idle_watchdog(server: ThreadingHTTPServer) -> None:
    while True:
        time.sleep(15)
        if not is_busy() and time.time() - last_seen > IDLE_EXIT_SECONDS:
            server.shutdown()
            return


class ExclusiveServer(ThreadingHTTPServer):
    # Windows에서 SO_REUSEADDR이 켜져 있으면 서버 두 개가 같은 포트를 동시에 잡아서
    # 요청이 예전 서버로 갈 수 있다 → 포트를 독점하고, 이미 떠 있으면 시작하지 않는다
    allow_reuse_address = False

    def server_bind(self):
        import socket
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def main() -> None:
    MATERIALS.mkdir(exist_ok=True)
    try:
        server = ExclusiveServer(("127.0.0.1", PORT), Handler)
    except OSError:
        if sys.stdout:
            print(f"이미 다른 서버가 {PORT} 포트를 쓰고 있어요")
        return
    threading.Thread(target=idle_watchdog, args=(server,), daemon=True).start()
    if sys.stdout:  # pythonw로 실행되면 콘솔이 없음
        sys.stdout.reconfigure(encoding="utf-8")
        print(f"학습 튜터: http://127.0.0.1:{PORT}")
    try:
        server.serve_forever()
    finally:
        server.server_close()  # 포트를 바로 놓아줘야 새 서버가 뜰 수 있다


if __name__ == "__main__":
    main()
