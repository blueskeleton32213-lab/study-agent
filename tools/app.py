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
PORT = int(os.environ.get("STUDY_APP_PORT", "8765"))
IDLE_EXIT_SECONDS = 180  # 화면 heartbeat가 이만큼 없으면 종료
ALLOWED_EXT = {".pdf", ".pptx", ".docx"}
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

sys.path.insert(0, str(TOOLS))
import srs  # noqa: E402  복습 카드는 Claude 없이 직접 처리

srs_lock = threading.Lock()
CLAUDE = shutil.which("claude") or str(Path.home() / ".local" / "bin" / "claude.exe")
PYTHON = sys.executable.replace("pythonw.exe", "python.exe")

def code_version() -> float:
    """서버 코드(tools/*.py)의 최신 수정 시각. 실행 중에 코드가 바뀌면 launch.pyw가 서버를 교체한다."""
    return max(p.stat().st_mtime for p in TOOLS.glob("*.py"))


STARTED_VERSION = code_version()
state_lock = threading.Lock()
current_proc: subprocess.Popen | None = None
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
CHAT_ID = re.compile(r"^[0-9a-f]{32}$")


def chat_path(cid: str) -> Path:
    if not CHAT_ID.match(cid or ""):
        raise ValueError("잘못된 대화 기록이에요")
    return CHATS / f"{cid}.json"


def chat_list() -> list[dict]:
    out = []
    for f in CHATS.glob("*.json") if CHATS.exists() else []:
        try:
            c = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        # 검색용으로 대화 내용을 짧게 합쳐서 같이 보낸다 (전체 내용은 열 때 받음)
        text = " ".join(m.get("text", "") for m in c.get("messages", []))
        out.append({"id": c["id"], "title": c.get("title", ""), "subject": c.get("subject"), "created": c.get("created"),
                    "updated": c.get("updated"), "count": len(c.get("messages", [])), "search": text[:4000]})
    return sorted(out, key=lambda c: c.get("updated") or "", reverse=True)


def chat_save(data: dict) -> dict:
    import uuid
    msgs = [{"role": m["role"], "text": str(m.get("text", ""))} for m in data.get("messages", [])
            if m.get("role") in ("user", "bot", "notice")]
    if not msgs:
        raise ValueError("저장할 대화가 없어요")
    cid = data.get("id") or uuid.uuid4().hex
    path = chat_path(cid)
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    old = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    first = next((m["text"] for m in msgs if m["role"] == "user"), "새 대화")
    title = old.get("title") or re.sub(r"\s+", " ", first).strip()[:48]
    CHATS.mkdir(parents=True, exist_ok=True)
    rec = {"id": cid, "title": title, "subject": data.get("subject") or old.get("subject"),
           "session_id": data.get("session_id") or old.get("session_id"),
           "created": old.get("created", now), "updated": now, "messages": msgs}
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(rec, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)
    return {"ok": True, "id": cid, "title": title}


ORDER_FILE = ROOT / "data" / "subject_order.json"


def load_order() -> list[str]:
    try:
        return json.loads(ORDER_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []


def save_order(order: list[str]) -> None:
    ORDER_FILE.parent.mkdir(parents=True, exist_ok=True)
    ORDER_FILE.write_text(json.dumps([safe_name(n) for n in order], ensure_ascii=False), encoding="utf-8")


def app_state() -> dict:
    MATERIALS.mkdir(exist_ok=True)
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
    return {"subjects": subjects, "due": sum(s["due"] for s in subjects)}


# ───────────────────────── 출처 보기 ─────────────────────────
CACHE_HD = ROOT / "cache" / "hd"


def material_path(rel: str) -> Path:
    """materials/ 안의 파일만 허용."""
    p = (MATERIALS / rel).resolve()
    if not p.is_relative_to(MATERIALS.resolve()) or not p.is_file() or p.suffix.lower() not in ALLOWED_EXT:
        raise ValueError(f"자료를 찾을 수 없어요: {rel}")
    return p


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
    out = CACHE_HD / src.relative_to(MATERIALS).parent / f"{src.stem}_p{page}.png"
    if not out.exists() or out.stat().st_mtime < src.stat().st_mtime:
        with pymupdf.open(src) as doc:
            if not 1 <= page <= len(doc):
                raise ValueError(f"{page}쪽이 없어요 (전체 {len(doc)}쪽)")
            out.parent.mkdir(parents=True, exist_ok=True)
            doc[page - 1].get_pixmap(dpi=160).save(out)
    return out.read_bytes()


# ───────────────────────── Claude 호출 ─────────────────────────
def tool_label(name: str, inp: dict) -> str:
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
        return "명령 실행"
    if name == "Skill":
        return f"스킬 실행: {inp.get('skill', '')}"
    if name == "StructuredOutput":
        return "문제 정리 중"
    return name


def stream_claude(message: str, session_id: str | None, send, extra: list[str] | None = None,
                  on_result=None) -> None:
    """claude -p 를 실행하며 이벤트를 send로 흘려보낸다. on_result가 있으면 result 이벤트를 넘겨준다."""
    global current_proc
    cmd = [CLAUDE, "-p", "--output-format", "stream-json", "--verbose", "--include-partial-messages", *(extra or [])]
    if session_id:
        cmd += ["--resume", session_id]
    proc = subprocess.Popen(cmd, cwd=ROOT, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, creationflags=NO_WINDOW)
    with state_lock:
        current_proc = proc
    proc.stdin.write(message.encode("utf-8"))
    proc.stdin.close()

    had_text = False
    tool_inputs: dict[int, dict] = {}
    got_result = False
    try:
        for raw in proc.stdout:
            try:
                ev = json.loads(raw.decode("utf-8", errors="replace"))
            except json.JSONDecodeError:
                continue
            t = ev.get("type")
            if t == "system" and ev.get("subtype") == "init":
                send({"type": "session", "session_id": ev.get("session_id")})
            elif t == "stream_event" and ev.get("parent_tool_use_id") is None:
                e = ev["event"]
                et = e.get("type")
                if et == "content_block_start":
                    block = e["content_block"]
                    if block["type"] == "text" and had_text:
                        send({"type": "text", "delta": "\n\n"})
                    elif block["type"] == "tool_use":
                        tool_inputs[e["index"]] = {"name": block["name"], "json": ""}
                elif et == "content_block_delta":
                    d = e["delta"]
                    if d["type"] == "text_delta":
                        had_text = True
                        send({"type": "text", "delta": d["text"]})
                    elif d["type"] == "input_json_delta" and e["index"] in tool_inputs:
                        tool_inputs[e["index"]]["json"] += d["partial_json"]
                elif et == "content_block_stop" and e.get("index") in tool_inputs:
                    tu = tool_inputs.pop(e["index"])
                    try:
                        inp = json.loads(tu["json"] or "{}")
                    except json.JSONDecodeError:
                        inp = {}
                    send({"type": "tool", "label": tool_label(tu["name"], inp)})
            elif t == "result":
                got_result = True
                if on_result:
                    on_result(ev)
                if ev.get("is_error"):
                    send({"type": "error", "message": str(ev.get("result") or ev.get("subtype"))})
                send({"type": "done", "session_id": ev.get("session_id")})
        proc.wait()
        if not got_result:
            err = proc.stderr.read().decode("utf-8", errors="replace").strip()
            if proc.returncode not in (0, None) and err:
                send({"type": "error", "message": err[-800:]})
            send({"type": "done", "session_id": session_id})
    finally:
        with state_lock:
            if current_proc is proc:
                current_proc = None
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
        },
        "required": ["type", "question", "choices", "answer_index", "answer", "explanation", "source", "topic"],
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
# 파일명으로 시험 관련 자료를 찾는다 (중간고사·기출·모의고사·퀴즈·문제지·해설·형광펜 문항 등)
EXAM_RE = re.compile(r"중간|기말|기출|모의|퀴즈|quiz|문제지|해설|solution|답안|시험|exam|형광펜", re.I)
STYLES = ROOT / "data" / "styles"
STYLE_SCHEMA = {"type": "object", "properties": {"guide": {"type": "string"}}, "required": ["guide"]}


def exam_files(subject: str) -> list[Path]:
    folder = MATERIALS / subject
    if not folder.exists():
        return []
    return sorted(f for f in folder.rglob("*") if f.suffix.lower() in ALLOWED_EXT and EXAM_RE.search(f.stem))


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
            # 시험 자료가 추가·수정·삭제되면 다시 분석해야 한다
            "stale": bool(files) and (not rec or rec.get("files") != sig), "sig": sig}


def style_prompt(subject: str, files: list[str]) -> str:
    names = "\n".join(f"- {n} → texts/{subject}/{Path(n).with_suffix('.md').name}" for n in files)
    return f"""[시험 스타일 분석 — 결과는 JSON(guide)으로만]
과목: {subject}
아래 시험 관련 자료를 **모두** 읽고, 이 과목 시험이 어떤 형식으로 나오는지 출제자가 그대로 따라 할 수 있는 스타일 가이드를 만든다.
{names}

guide(마크다운, 1500~3000자)에 담을 것:
1. 문항 구성: 한 시험(또는 한 세트)의 문항 수, 유형 비율(계산·서술·단답·객관식·OX·빈칸), 배점 표시 방식
2. 문장 형식: 실제 문제의 말투와 지시문 템플릿 3~6개 (원문 표현을 짧게 인용, 예: "~를 구하시오", "(10점)")
3. 구조: 소문항 (1)(2)/(a)(b) 사용 여부, 조건·표·그림·수식을 주는 방식
4. 자주 나오는 주제와 강의 범위 (어느 강의·주차에서 많이 나오는지)
5. 난이도와 함정: 자주 유도하는 실수, 헷갈리게 만드는 포인트
6. 답안 요구 형식: 풀이 과정 요구 여부, 단위·최종 답 표기
7. 출제 체크리스트: 이 과목 스타일로 새 문제를 낼 때 지킬 것 5~8개

규칙: 수식은 LaTeX. `(🖼 ...)` 표시가 있는 쪽의 수식·그림이 필요하면 render.py로 원본을 본다. 문제를 통째로 옮기지 말고 형식을 요약한다. 인사말·진행 멘트 없이."""


def save_style(subject: str, guide: str, sig: dict) -> None:
    STYLES.mkdir(parents=True, exist_ok=True)
    (STYLES / f"{subject}.json").write_text(json.dumps(
        {"subject": subject, "created": time.strftime("%Y-%m-%d %H:%M"), "files": sig, "guide": guide},
        ensure_ascii=False, indent=1), encoding="utf-8")


def save_note(subject: str, note: str) -> None:
    STYLES.mkdir(parents=True, exist_ok=True)
    p = STYLES / f"{subject}.note.txt"
    note = (note or "").strip()[:4000]
    if note:
        p.write_text(note, encoding="utf-8")
    elif p.exists():
        p.unlink()


def quiz_prompt(subject: str, scope: str, n: int, level: str, qtype: str, guide: str | None = None,
                note: str = "") -> str:
    style_block = ""
    if guide or note:
        qtype_text = "이 과목 시험 스타일의 유형 비율대로" if qtype == "mix" else QTYPES.get(qtype, QTYPES["mix"])
        parts = []
        if note:
            parts.append(f"## 교수님 출제 스타일 (학생이 직접 적은 것 — 가장 우선한다)\n{note}")
        if guide:
            parts.append(f"## 시험 자료에서 분석한 스타일{' (위 메모와 다르면 메모를 따른다)' if note else ''}\n{guide}")
        style_block = "\n\n" + "\n\n".join(parts) + f"""

시험 스타일 출제 규칙 (반드시 따른다):
- 위 스타일의 유형 비율, 말투, 지시문, 소문항 구조, 배점 표시를 그대로 따라 **실제 시험지처럼** 만든다. 배점을 표시하는 과목이면 문제 앞에 "(10점)"처럼 붙인다.
- 유형: {qtype_text}. 시험에 객관식이 없는 과목이면 서술·계산형 위주로 낸다.
- 내용은 강의자료 범위에서 새로 만든다. 기출·모의고사 문항을 숫자만 바꿔 그대로 옮기지 않는다 (구조와 형식은 같아도 된다).
- 소문항이 있는 문제는 하나의 서술형(short)으로 만들고, answer에 소문항별 모범답안을 모두 쓴다."""
    return f"""[퀴즈 출제 — 앱 화면용, 결과는 JSON으로만]
과목: {subject} / 범위: {scope or "전체"} / 문제 수: {n} / 난이도: {level} ({LEVELS.get(level, LEVELS["보통"])}) / 유형: {"시험 스타일을 따름" if (guide or note) and qtype == "mix" else QTYPES.get(qtype, QTYPES["mix"])}{style_block}

출제 규칙:
1. `texts/{subject}/`에서 범위에 해당하는 파일을 읽고 **그 내용에서만** 출제한다. 범위가 "전체"면 강의 전반에 고르게.
2. `python tools/srs.py stats {subject}`의 자주틀린주제가 범위 안에 있으면 1~2문제는 그 주제로.
3. 파일명에 solution·해설·답안·모의고사·문제지가 있는 자료는 출제 스타일과 자주 나오는 유형을 참고만 하고, 문제를 그대로 베끼지 않는다.
4. 수식은 LaTeX(`$...$`). `(🖼 ...)` 표시가 있는 쪽의 수식이 필요하면 render.py로 원본을 확인한다. 추측으로 수식을 만들지 않는다.
5. 객관식(mcq): choices에 보기 정확히 4개, answer_index는 정답 위치(0~3). 오답 보기는 학생이 실제로 헷갈릴 만한 오개념으로. "모두 정답/정답 없음" 보기는 쓰지 않는다. 정답 위치는 문제마다 골고루 섞는다. answer에는 정답 보기의 내용을 쓴다.
6. 서술형(short): 2~4문장이나 계산 결과로 답할 수 있게. choices는 [], answer_index는 -1. answer는 채점 기준이 되는 모범답안(꼭 들어가야 할 핵심 키워드 포함).
7. explanation: 왜 그게 정답인지 2~4문장. 흔한 실수가 있으면 함께.
8. source: 원본 파일명 + 위치, 예) `Lecture05_TFIDF_Part1.pdf p.12`, `보고서.docx part 4`, `2강.pptx slide 3`.
9. topic: 짧고 일관된 개념명 (예: "TF-IDF-정의").
10. 복습 카드 저장, 확인 질문, 인사말은 하지 않는다. 문제는 정확히 {n}개."""


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
    r = subprocess.run([CLAUDE, "-p", "--tools", "", "--output-format", "json", "--json-schema", json.dumps(GRADE_SCHEMA)],
                       cwd=ROOT, input=prompt.encode("utf-8"), capture_output=True, creationflags=NO_WINDOW, timeout=180)
    try:
        data = json.loads(r.stdout.decode("utf-8", errors="replace"))
        res = data.get("structured_output")
        if not res:
            raise ValueError(data.get("result") or "채점 결과가 비어 있어요")
        return res
    except json.JSONDecodeError:
        raise ValueError((r.stderr or r.stdout).decode("utf-8", errors="replace")[-500:] or "채점 실패")


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
- 근거 자료: 파일명에 solution·해설·답안·모의고사·문제지·기출·퀴즈가 있는 파일. 모두 읽는다.
- `## 유형별 정리` 표: `| 유형 | 나온 횟수 | 대표 문제(요약) | 관련 강의 | 출처 |`
- `## 자주 나오는 함정`: 오답을 유도하는 포인트와 피하는 법.
- `## 배점·난이도 경향` (자료에 있을 때만).
- `## 대비 전략`: 유형별로 무엇을 연습해야 하는지, 우선순위 순서로.
- 이런 파일이 없으면 그 사실을 맨 위에 밝히고, 강의에서 강조된 부분(반복, '중요', 예제) 기준으로 예상 포인트를 정리한다."""),
    "glossary": ("용어집", """**용어집**을 만든다.
- 강의 순서대로 `## 강의 제목` 섹션, 각 섹션에 표: `| 용어 | 영어 | 한 줄 정의 | 출처 |`
- 영어 강의면 한국어 번역 용어를, 한국어 강의면 영어 원어를 함께.
- 마지막에 `## 헷갈리는 용어 비교`: 비슷한 용어 쌍을 표로 (차이점 한 줄)."""),
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
- `texts/{subject}/`에서 범위에 해당하는 파일을 **빠짐없이** 읽고, 그 내용만으로 만든다. 자료에 없는 내용은 넣지 않는다 (꼭 필요하면 "(강의자료 외 보충)" 표시).
- 핵심 문장·표의 행마다 출처를 단다. 형식: `(Lecture05_TFIDF_Part1.pdf p.12)`, `(보고서.docx part 4)`, `(2강.pptx slide 3)`.
- 수식은 LaTeX(`$...$`, 따로 보일 식은 `$$...$$`).
- markdown은 바로 인쇄해도 될 완성본으로: 맨 위에 `#` 제목 없이 `##`부터 시작. 인사말, "확인할게요" 같은 진행 멘트, 확인 질문, 카드 저장 제안은 쓰지 않는다.
- title: 짧게, 예) "{subject} 시험대비 요약 ({scope or "전체"})"."""


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


# ───────────────────────── 과목 삭제 (휴지통으로 옮기기) ─────────────────────────
TRASH = ROOT / "휴지통"


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
    """과목을 통째로 휴지통/<시각>_<과목>/ 으로 옮긴다. 영구 삭제하지 않는다."""
    if not (MATERIALS / name).exists() and not any(c["subject"] == name for c in srs.load()):
        raise ValueError(f"없는 과목이에요: {name}")
    summary = subject_dirs(name)
    dest = TRASH / f"{time.strftime('%Y%m%d-%H%M%S')}_{name}"
    dest.mkdir(parents=True, exist_ok=True)
    moved = []
    try:
        for key, src in summary.items():  # 자료 폴더부터 옮긴다 (탐색기에서 열려 있으면 여기서 실패 → 아무것도 안 바뀜)
            if src.exists():
                shutil.move(str(src), str(dest / key))
                moved.append(key)
    except OSError as exc:
        for key in reversed(moved):  # 일부만 옮겨졌으면 되돌린다
            shutil.move(str(dest / key), str(summary[key]))
        shutil.rmtree(dest, ignore_errors=True)
        raise ValueError(f"옮기지 못했어요. 탐색기에서 이 과목 폴더나 파일이 열려 있으면 닫고 다시 해주세요. ({exc})")
    with srs_lock:
        cards = srs.load()
        gone = [c for c in cards if c["subject"] == name]
        if gone:
            (dest / "cards.json").write_text(json.dumps(gone, ensure_ascii=False, indent=1), encoding="utf-8")
            srs.save([c for c in cards if c["subject"] != name])
    return {"ok": True, "trash": dest.relative_to(ROOT).as_posix(), "cards": len(gone)}


def stop_claude() -> None:
    with state_lock:
        proc = current_proc
    if proc and proc.poll() is None:
        # claude가 띄운 하위 프로세스까지 함께 종료
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
        elif path == "/api/chats":
            self._json({"chats": chat_list()})
        elif path == "/api/chats/get":
            try:
                p = chat_path(parse_qs(urlparse(self.path).query).get("id", [""])[0])
                self._json(json.loads(p.read_text(encoding="utf-8")))
            except (ValueError, OSError) as exc:
                self._json({"error": f"대화 기록을 열지 못했어요: {exc}"}, 404)
        elif path == "/api/studio":
            q = parse_qs(urlparse(self.path).query)
            self._json({"docs": studio_list(q["subject"][0] if q.get("subject") else None)})
        elif path == "/api/studio/doc":
            try:
                text = studio_path(parse_qs(urlparse(self.path).query)["path"][0]).read_text(encoding="utf-8")
                self._json({"markdown": text.split("\n---\n", 1)[-1]})
            except (ValueError, KeyError) as exc:
                self._json({"error": str(exc)}, 404)
        elif path in ("/api/source", "/api/page", "/api/section"):
            q = {k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()}
            try:
                if path == "/api/source":
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
        try:
            if url.path == "/api/chat":
                return self._chat()
            if url.path == "/api/shutdown":  # launch.pyw가 코드가 바뀐 예전 서버를 내릴 때
                stop_claude()
                self._json({"ok": True})
                threading.Thread(target=self.server.shutdown, daemon=True).start()
                return
            if url.path == "/api/stop":
                stop_claude()
                return self._json({"ok": True})
            if url.path == "/api/grade":
                data = json.loads(self._body())
                if data["button"] not in srs.BUTTONS:
                    raise ValueError(f"알 수 없는 버튼: {data['button']}")
                with srs_lock:
                    card = srs.grade(int(data["id"]), srs.BUTTONS[data["button"]])
                return self._json({"ok": True, "due": card["due"], "interval": card["interval"]})
            if url.path == "/api/quiz/generate":
                return self._quiz_generate()
            if url.path == "/api/studio/generate":
                return self._studio_generate()
            if url.path == "/api/studio/delete":
                studio_path(json.loads(self._body())["path"]).unlink()
                return self._json({"ok": True})
            if url.path == "/api/quiz/grade":
                data = json.loads(self._body())
                try:
                    return self._json(grade_answer(data["question"], data["user_answer"]))
                except (ValueError, subprocess.TimeoutExpired) as exc:
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
            if url.path == "/api/chats/save":
                return self._json(chat_save(json.loads(self._body())))
            if url.path == "/api/chats/rename":
                data = json.loads(self._body())
                p = chat_path(data["id"])
                rec = json.loads(p.read_text(encoding="utf-8"))
                rec["title"] = re.sub(r"\s+", " ", str(data["title"])).strip()[:80] or rec["title"]
                p.write_text(json.dumps(rec, ensure_ascii=False, indent=1), encoding="utf-8")
                return self._json({"ok": True, "title": rec["title"]})
            if url.path == "/api/chats/delete":
                p = chat_path(json.loads(self._body())["id"])
                if p.exists():
                    p.unlink()
                return self._json({"ok": True})
            if url.path == "/api/subject/order":
                save_order(json.loads(self._body())["order"])
                return self._json({"ok": True})
            if url.path == "/api/subject/summary":
                return self._json(subject_summary(safe_name(json.loads(self._body())["name"])))
            if url.path == "/api/subject/delete":
                with state_lock:
                    busy = current_proc is not None and current_proc.poll() is None
                if busy:
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
                return self._json({"ok": True, "log": run_tool("extract.py", subject)})
        except (ValueError, KeyError, json.JSONDecodeError) as exc:
            return self._json({"error": str(exc)}, 400)
        self._json({"error": "not found"}, 404)

    def _stream_start(self) -> bool:
        with state_lock:
            busy = current_proc is not None and current_proc.poll() is None
        if busy:
            self._json({"error": "이전 작업이 아직 진행 중이에요"}, 409)
            return False
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        return True

    def _sender(self):
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
                stop_claude()
        return send

    def _quiz_generate(self):
        data = json.loads(self._body())
        subject = safe_name(data["subject"])
        n = max(1, min(int(data.get("n", 10)), 20))
        if not self._stream_start():
            return
        send = self._sender()
        result = {}
        extra = ["--json-schema", json.dumps(QUIZ_SCHEMA)]

        def forward(ev):  # 출제 중에는 진행 상황(도구 사용)만 보여주고 중간 글은 숨긴다
            if ev["type"] not in ("text", "done", "session"):
                send(ev)
        try:
            guide, note = None, ""
            if data.get("style", "exam") == "exam":
                info = style_info(subject)
                note = info["note"]
                guide = info["guide"] if info["has_exam"] else None  # 시험 자료를 지웠으면 예전 분석은 쓰지 않는다
                if info["has_exam"] and (info["stale"] or not guide):
                    # 처음이거나 시험 자료가 바뀌었으면 스타일부터 분석해서 저장
                    send({"type": "tool", "label": f"시험 스타일 분석 중 (자료 {len(info['files'])}개, 처음 한 번)"})
                    sres = {}
                    stream_claude(style_prompt(subject, info["files"]), None, forward,
                                  ["--json-schema", json.dumps(STYLE_SCHEMA)], on_result=sres.update)
                    guide = (sres.get("structured_output") or {}).get("guide") or guide
                    if guide and (sres.get("structured_output") or {}).get("guide"):
                        save_style(subject, guide, info["sig"])
                        send({"type": "tool", "label": "시험 스타일 저장함 → 문제 만드는 중"})
                send({"type": "style", "used": bool(guide or note), "files": len(info["files"]), "note": bool(note)})
            prompt = quiz_prompt(subject, (data.get("scope") or "").strip(), n, data.get("level", "보통"),
                                 data.get("qtype", "mix"), guide, note)
            stream_claude(prompt, None, forward, extra, on_result=result.update)
            qs = clean_questions((result.get("structured_output") or {}).get("questions") or [])
            if qs:
                send({"type": "quiz", "questions": qs})
            elif not result.get("is_error"):
                send({"type": "error", "message": "문제를 만들지 못했어요. 범위를 바꿔서 다시 해보세요."})
        except Exception as exc:
            send({"type": "error", "message": f"출제 실패: {exc}"})
            send({"type": "done"})
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
        if not self._stream_start():
            return
        send = self._sender()
        result = {}

        def forward(ev):  # 진행 상황만 보여주고 중간 글은 숨긴다
            if ev["type"] != "text":
                send(ev)
        try:
            stream_claude(studio_prompt(subject, kind, scope, custom), None, forward,
                          ["--json-schema", json.dumps(DOC_SCHEMA)], on_result=result.update)
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
        with state_lock:
            busy = current_proc is not None and current_proc.poll() is None
        if busy:
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
                stop_claude()

        try:
            stream_claude(message, data.get("session_id"), send)
        except Exception as exc:
            send({"type": "error", "message": f"Claude 실행 실패: {exc}"})
            send({"type": "done", "session_id": data.get("session_id")})
        self.close_connection = True


def idle_watchdog(server: ThreadingHTTPServer) -> None:
    while True:
        time.sleep(15)
        with state_lock:
            busy = current_proc is not None and current_proc.poll() is None
        if not busy and time.time() - last_seen > IDLE_EXIT_SECONDS:
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
