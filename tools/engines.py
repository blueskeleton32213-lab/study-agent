"""AI 엔진: Claude Code · Codex(GPT) · Antigravity(Gemini) CLI를 같은 방식으로 부른다.

세 엔진 모두 구독 로그인을 그대로 쓴다 (Claude는 API 키 요금제도 고를 수 있음).
설정은 data/ai.json — 앱의 'AI 설정' 창에서 바꾼다.

    {"engine": "claude",
     "plans": {"claude": "pro", "gpt": "plus", "gemini": "free"},
     "models": {"claude": {"chat": "opus"}},      # 요금제 기본값 대신 직접 고른 모델 (기능별)
     "claude_api_key": ""}                        # Claude 'API 키' 요금제일 때만

start()가 돌려주는 Run.events()는 엔진과 상관없이 같은 모양의 이벤트를 내보낸다.
    {"type": "session", "session_id"}   이어가기용 id (Claude는 그대로, 나머지는 "codex:…", "gemini:…")
    {"type": "text", "delta"}
    {"type": "tool", "name", "input"}   name: Read/Grep/Glob/Bash/Skill 또는 엔진 고유 이름
    {"type": "result", ...}             Claude의 result 이벤트 모양 (is_error, result, structured_output,
                                        session_id, usage, modelUsage, total_cost_usd)
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SETTINGS = ROOT / "data" / "ai.json"
MODEL_CACHE = ROOT / "cache" / "ai_models.json"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
NEW_CONSOLE = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)

# 기능 이름 (usage.py의 KIND_NAMES와 같음)
KINDS = {"chat": "질문하기", "ask": "질문 창", "quiz": "퀴즈 출제", "style": "시험 스타일 분석",
         "studio": "스튜디오", "grade": "서술형 채점", "digest": "요약본 만들기"}
# websolve(인터넷 답 찾기)는 퀴즈 출제 모델을 그대로 써서 AI 설정에 따로 두지 않는다 (사용량 기록에만 이름이 있음)

ENGINES = {
    "claude": {"name": "Claude", "cli": "Claude Code", "plans": {"pro": "Pro", "max": "Max", "api": "API 키"}},
    "gpt": {"name": "GPT", "cli": "Codex", "plans": {"plus": "Plus", "pro": "Pro"}},
    "gemini": {"name": "Gemini", "cli": "Antigravity", "plans": {"free": "무료", "aipro": "AI Pro"}},
}

# 요금제별 기본 모델. 값은 "모델" 또는 "모델:생각 깊이"(GPT만). "" = CLI 기본 모델.
# GPT·Gemini는 모델 이름 대신 역할(@best·@main·@fast·@light)로 적고, 설치된 CLI의 모델 목록에서 찾는다
# (모델이 새로 나와도 프리셋을 고칠 필요가 없게).
PRESETS = {
    "claude": {
        # Pro: 사용 한도가 작으니 질문은 Sonnet, 출제·정리는 기본 모델, 채점·요약은 Haiku (기존 동작)
        "pro": {"chat": "sonnet", "ask": "sonnet", "quiz": "", "style": "", "studio": "", "grade": "haiku", "digest": "haiku"},
        # Max: 한도가 넉넉하니 품질이 중요한 곳은 Opus
        "max": {"chat": "opus", "ask": "sonnet", "quiz": "opus", "style": "opus", "studio": "opus", "grade": "haiku", "digest": "haiku"},
        # API 키: 쓴 만큼 요금이 나가니 Opus는 빼고 Sonnet 중심
        "api": {"chat": "sonnet", "ask": "sonnet", "quiz": "sonnet", "style": "sonnet", "studio": "sonnet", "grade": "haiku", "digest": "haiku"},
    },
    "gpt": {
        "plus": {"chat": "@main:medium", "ask": "@light:low", "quiz": "@main:medium", "style": "@main:medium",
                 "studio": "@main:medium", "grade": "@light:low", "digest": "@light:low"},
        "pro": {"chat": "@main:high", "ask": "@main:medium", "quiz": "@best:high", "style": "@best:medium",
                "studio": "@best:high", "grade": "@light:low", "digest": "@light:low"},
    },
    "gemini": {
        "free": {"chat": "@fast", "ask": "@fast", "quiz": "@fast", "style": "@fast", "studio": "@fast",
                 "grade": "@light", "digest": "@light"},
        "aipro": {"chat": "@best", "ask": "@fast", "quiz": "@best", "style": "@best", "studio": "@best",
                  "grade": "@light", "digest": "@light"},
    },
}
EFFORTS = {"low": "낮게", "medium": "보통", "high": "깊게", "xhigh": "아주 깊게"}

CLAUDE_MODELS = [{"id": "opus", "name": "Opus"}, {"id": "sonnet", "name": "Sonnet"}, {"id": "haiku", "name": "Haiku"}]
# 튜터에 필요한 도구만 (개인 MCP·스킬은 싣지 않음 → 입력 토큰 절약)
CLAUDE_TOOLS = "Read,Grep,Glob,Bash,Skill"

# Claude 외 엔진은 Skill 도구가 없고 CLAUDE.md를 자동으로 읽지 않을 수 있어서, 새 대화 맨 앞에 붙인다
SKILL_NOTE = """[이 앱에서 지킬 것]
- 아래(또는 프로젝트의) CLAUDE.md가 튜터 규칙이다. 그대로 따른다.
- 스킬(quiz 퀴즈, review 복습, weak 약점, ingest 자료 정리, figure 그림)이 필요하면 `.claude/skills/<이름>/SKILL.md`를 읽고 그 절차를 따른다.
- 명령은 CLAUDE.md에 적힌 `python tools/...` 형식 그대로 실행한다. materials/ 원본과 data/cards.json은 직접 고치지 않는다.
- 파일은 필요한 줄 주변만 읽는다 (통째로 읽지 않기)."""


# ───────────────────────── 설정 ─────────────────────────
_lock = threading.Lock()


def load() -> dict:
    try:
        s = json.loads(SETTINGS.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        s = {}
    s.setdefault("engine", "claude")
    if s["engine"] not in ENGINES:
        s["engine"] = "claude"
    plans = {e: next(iter(v["plans"])) for e, v in ENGINES.items()}
    plans.update({e: p for e, p in (s.get("plans") or {}).items() if e in ENGINES and p in ENGINES[e]["plans"]})
    s["plans"] = plans
    s.setdefault("models", {})
    s.setdefault("claude_api_key", "")
    return s


def save(new: dict) -> dict:
    """화면에서 받은 설정을 검사해서 저장한다. API 키는 빈 값이면 기존 값을 유지한다."""
    with _lock:
        s = load()
        if new.get("engine") in ENGINES:
            s["engine"] = new["engine"]
        for e, p in (new.get("plans") or {}).items():
            if e in ENGINES and p in ENGINES[e]["plans"]:
                s["plans"][e] = p
        for e, kinds in (new.get("models") or {}).items():
            if e not in ENGINES or not isinstance(kinds, dict):
                continue
            cur = s["models"].setdefault(e, {})
            for k, v in kinds.items():
                if k not in KINDS:
                    continue
                v = str(v or "").strip()
                if not v or v == "@preset":
                    cur.pop(k, None)
                elif re.fullmatch(r"@?[\w.\-]*(:\w+)?", v):
                    cur[k] = v
        if "claude_api_key" in new:
            key = str(new["claude_api_key"] or "").strip()
            if key == "-":  # 지우기
                s["claude_api_key"] = ""
            elif key:
                s["claude_api_key"] = key
        if s["engine"] == "claude" and s["plans"]["claude"] == "api" and not s["claude_api_key"]:
            raise ValueError("Claude API 키 요금제는 API 키가 필요해요")
        SETTINGS.parent.mkdir(parents=True, exist_ok=True)
        tmp = SETTINGS.with_suffix(".tmp")
        tmp.write_text(json.dumps(s, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(SETTINGS)
        return s


# ───────────────────────── CLI 찾기 ─────────────────────────
def exe(engine: str) -> str | None:
    home = Path.home()
    if engine == "claude":
        cands = [shutil.which("claude"), home / ".local" / "bin" / "claude.exe"]
    elif engine == "gpt":
        # npm의 codex.cmd를 거치면 cmd.exe가 인자의 따옴표를 망가뜨릴 수 있어서, 들어 있는 codex.exe를 바로 쓴다
        npm = Path(os.environ.get("APPDATA", home / "AppData" / "Roaming")) / "npm" / "node_modules" / "@openai" / "codex"
        cands = sorted(npm.glob("node_modules/@openai/codex-win32-*/vendor/*/bin/codex.exe")) + [shutil.which("codex")]
    else:
        local = Path(os.environ.get("LOCALAPPDATA", home / "AppData" / "Local"))
        cands = [shutil.which("agy"), local / "agy" / "bin" / "agy.exe"]
    for c in cands:
        if c and Path(c).exists():
            return str(c)
    return None


INSTALL = {
    "claude": "npm install -g @anthropic-ai/claude-code",
    "gpt": "npm install -g @openai/codex",
    "gemini": "irm https://antigravity.google/cli/install.ps1 | iex   (PowerShell)",
}


def login_status(engine: str) -> dict:
    """설치·로그인 상태. 확인할 방법이 없으면 logged_in=None."""
    path = exe(engine)
    if not path:
        return {"installed": False, "logged_in": False, "install": INSTALL[engine]}
    st = {"installed": True, "logged_in": None, "install": INSTALL[engine]}
    try:
        if engine == "gpt":
            r = subprocess.run([path, "login", "status"], capture_output=True, text=True, encoding="utf-8",
                               errors="replace", timeout=15, creationflags=NO_WINDOW)
            out = (r.stdout + r.stderr).strip()
            st["logged_in"] = r.returncode == 0 and "not logged in" not in out.lower()
            st["detail"] = out.splitlines()[-1] if out else ""
        elif engine == "claude":
            r = subprocess.run([path, "auth", "status"], capture_output=True, text=True, encoding="utf-8",
                               errors="replace", timeout=15, creationflags=NO_WINDOW)
            out = (r.stdout + r.stderr).strip()
            if r.returncode == 0 and out.startswith("{"):
                info = json.loads(out)
                st["logged_in"] = bool(info.get("loggedIn"))
                st["plan"] = info.get("subscriptionType") or ""  # pro / max — 요금제 고를 때 참고로 보여준다
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
        pass
    return st


def open_login(engine: str) -> None:
    """로그인 창(새 콘솔)을 띄운다. 비밀번호는 앱·대화를 거치지 않고 CLI가 브라우저로 받는다."""
    path = exe(engine)
    if not path:
        raise ValueError(f"{ENGINES[engine]['cli']}가 설치돼 있지 않아요: {INSTALL[engine]}")
    args = {"claude": [path, "auth", "login"], "gpt": [path, "login"], "gemini": [path]}[engine]
    subprocess.Popen(args, cwd=tempfile.gettempdir(), creationflags=NEW_CONSOLE)


# ───────────────────────── 모델 목록·선택 ─────────────────────────
def _version_key(slug: str) -> tuple:
    return tuple(float(x) for x in re.findall(r"\d+(?:\.\d+)?", slug)) or (0,)


def list_models(engine: str, refresh: bool = False) -> list[dict]:
    """CLI에서 고를 수 있는 모델 목록 (하루 동안 cache/ai_models.json에 보관)."""
    if engine == "claude":
        return CLAUDE_MODELS
    try:
        cache = json.loads(MODEL_CACHE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        cache = {}
    hit = cache.get(engine)
    if hit and not refresh and time.time() - hit.get("t", 0) < 86400 and hit.get("models"):
        return hit["models"]
    path = exe(engine)
    models: list[dict] = []
    try:
        if path and engine == "gpt":
            r = subprocess.run([path, "debug", "models"], capture_output=True, timeout=30, creationflags=NO_WINDOW)
            data = json.loads(r.stdout.decode("utf-8", errors="replace"))
            ms = [m for m in data.get("models", []) if m.get("visibility") == "list"]
            ms.sort(key=lambda m: m.get("priority", 99))
            models = [{"id": m["slug"], "name": m.get("display_name") or m["slug"], "desc": m.get("description", ""),
                       "efforts": [e["effort"] for e in m.get("supported_reasoning_levels", [])]} for m in ms]
        elif path and engine == "gemini":
            r = subprocess.run([path, "models"], capture_output=True, timeout=30, creationflags=NO_WINDOW)
            out = r.stdout.decode("utf-8", errors="replace")
            seen = []
            for slug in re.findall(r"\b(gemini-[\w.\-]+)", out):
                if slug not in seen:
                    seen.append(slug)
            models = [{"id": s, "name": s} for s in seen]
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError, KeyError):
        models = []
    if models:
        cache[engine] = {"t": time.time(), "models": models}
        try:
            MODEL_CACHE.parent.mkdir(parents=True, exist_ok=True)
            MODEL_CACHE.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass
    return models or (hit or {}).get("models") or []


def _resolve_role(engine: str, role: str, models: list[dict]) -> str:
    ids = [m["id"] for m in models]
    if engine == "gpt":
        # 카탈로그 설명으로 역할을 고른다. 못 찾으면 "" (Codex 기본 모델)
        def by(words):
            return [m["id"] for m in models if any(w in m.get("desc", "").lower() for w in words)]
        if role == "best":
            hit = by(["frontier"])
        elif role == "light":
            hit = by(["fast", "affordable", "efficient"])
        else:  # main: Codex가 가장 앞에 두는(우선순위가 높은) 모델
            hit = by(["latest workhorse"]) or ids[:1]
        return hit[0] if hit else ""
    if engine == "gemini":
        pro = [i for i in ids if "pro" in i]
        flash = [i for i in ids if "flash" in i and "lite" not in i]
        lite = [i for i in ids if "lite" in i]
        pick = {"best": pro or flash, "fast": flash, "light": lite or flash}.get(role, [])
        return max(pick, key=_version_key) if pick else ""
    return ""


def choice(kind: str, s: dict | None = None, engine: str | None = None) -> dict:
    """기능(kind)에 쓸 모델과 생각 깊이. {"model", "effort", "spec", "custom"}"""
    s = s or load()
    engine = engine or s["engine"]
    plan = s["plans"][engine]
    custom = (s["models"].get(engine) or {}).get(kind)
    spec = custom or PRESETS[engine][plan].get(kind, "")
    model, _, effort = spec.partition(":")
    if model.startswith("@"):
        model = _resolve_role(engine, model[1:], list_models(engine))
    return {"model": model, "effort": effort, "spec": spec, "custom": bool(custom)}


def label(engine: str, c: dict) -> str:
    if not c["model"]:
        return f"{ENGINES[engine]['cli']} 기본 모델"
    name = next((m["name"] for m in list_models(engine) if m["id"] == c["model"]), c["model"])
    return name + (f" · 생각 {EFFORTS.get(c['effort'], c['effort'])}" if c["effort"] else "")


def summary() -> dict:
    """설정 창에 보낼 정보 (API 키 값은 보내지 않음)."""
    s = load()
    out = {"engine": s["engine"], "plans": s["plans"], "has_api_key": bool(s["claude_api_key"]),
           "kinds": KINDS, "efforts": EFFORTS, "engines": {}}
    for e, info in ENGINES.items():
        models = list_models(e)
        out["engines"][e] = {
            "name": info["name"], "cli": info["cli"], "plans": info["plans"],
            "status": login_status(e), "models": models,
            "presets": {p: {k: label(e, choice(k, {**s, "plans": {**s["plans"], e: p}, "models": {}}, e))
                            for k in KINDS} for p in info["plans"]},
            "custom": s["models"].get(e) or {},
        }
    return out


def current_name() -> str:
    s = load()
    e = s["engine"]
    return f"{ENGINES[e]['name']} {ENGINES[e]['plans'][s['plans'][e]]}"


# ───────────────────────── 실행 ─────────────────────────
def session_engine(sid: str | None) -> str | None:
    if not sid:
        return None
    if sid.startswith("codex:"):
        return "gpt"
    if sid.startswith("gemini:"):
        return "gemini"
    return "claude"


def _env(engine: str, s: dict) -> dict:
    env = {**os.environ, "STUDY_APP": "1"}
    # 구독 로그인을 고른 경우 다른 곳에서 설정된 API 키가 끼어들지 않게 뺀다
    for k in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "CODEX_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY"):
        env.pop(k, None)
    if engine == "claude" and s["plans"]["claude"] == "api":
        env["ANTHROPIC_API_KEY"] = s["claude_api_key"]
    return env


def strict_schema(schema: dict) -> dict:
    """OpenAI 구조화 출력 규칙에 맞춘다: 모든 속성 required, additionalProperties false.
    원래 선택이던 속성은 null도 허용."""
    if not isinstance(schema, dict):
        return schema
    out = dict(schema)
    if out.get("type") == "object" and "properties" in out:
        req = set(out.get("required") or [])
        props = {}
        for k, v in out["properties"].items():
            v = strict_schema(v)
            if k not in req and isinstance(v.get("type"), str):
                v = {**v, "type": [v["type"], "null"]}
            props[k] = v
        out["properties"] = props
        out["required"] = list(props)
        out["additionalProperties"] = False
    if out.get("type") == "array" and "items" in out:
        out["items"] = strict_schema(out["items"])
    return out


def _rules_text() -> str:
    try:
        return (ROOT / "CLAUDE.md").read_text(encoding="utf-8")
    except OSError:
        return ""


def _ensure_agy_permissions() -> None:
    """Antigravity는 headless에서 승인이 필요한 명령을 조용히 거절한다 → 튜터 도구 명령만 미리 허용."""
    path = Path.home() / ".gemini" / "antigravity-cli" / "settings.json"
    want = [f"command(python tools/{t}.py)" for t in ("srs", "render", "digest", "extract")]
    try:
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        allow = data.setdefault("permissions", {}).setdefault("allow", [])
        missing = [w for w in want if w not in allow]
        if not missing:
            return
        allow.extend(missing)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except (OSError, json.JSONDecodeError, AttributeError):
        pass  # 설정을 못 고쳐도 질문·설명은 된다 (카드 저장 같은 명령만 막힘)


class Run:
    """AI CLI 한 번 실행. proc을 바로 쓸 수 있고(멈추기), events()로 결과를 받는다."""

    def __init__(self, kind: str, prompt: str, session_id: str | None = None, schema: dict | None = None,
                 bare: bool = False, web: bool = False):
        # web: 인터넷 검색(WebSearch·WebFetch)을 이번 한 번만 켠다 — 사용자가 "몇 문제를 인터넷에서 찾을지" 고른 뒤에만
        self.kind, self.schema, self.bare, self.web = kind, schema, bare, web
        s = load()
        self.engine = s["engine"]
        if session_engine(session_id) not in (None, self.engine):
            session_id = None  # 다른 엔진의 대화는 이어갈 수 없다 (호출한 쪽이 맥락을 붙여 새로 시작)
        self.session_id = session_id
        self.choice = choice("quiz" if kind == "websolve" else kind, s)  # 인터넷 답 찾기는 퀴즈 출제 모델로
        if web and self.engine != "claude":
            raise RuntimeError("인터넷에서 답 찾기는 지금 Claude 엔진에서만 돼요. 'AI 설정'에서 Claude로 바꿔 주세요.")
        path = exe(self.engine)
        if not path:
            raise RuntimeError(f"{ENGINES[self.engine]['cli']}가 설치돼 있지 않아요. 'AI 설정'에서 설치 방법을 확인하세요.")
        self._tmp: list[Path] = []
        cwd = tempfile.gettempdir() if bare else str(ROOT)
        stdin_text = prompt
        if self.engine == "claude":
            cmd = self._claude(path)
        elif self.engine == "gpt":
            if not bare and not session_id:
                stdin_text = f"{SKILL_NOTE}\n\n---\n\n{prompt}"
            cmd = self._codex(path)
        else:
            if not bare and not session_id:
                prompt = f"{SKILL_NOTE}\n\n# CLAUDE.md\n\n{_rules_text()}\n\n---\n\n{prompt}"
                _ensure_agy_permissions()
            cmd = self._agy(path, prompt)
            stdin_text = None
        self.proc = subprocess.Popen(cmd, cwd=cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, creationflags=NO_WINDOW, env=_env(self.engine, s))
        try:
            if stdin_text is not None:
                self.proc.stdin.write(stdin_text.encode("utf-8"))
            self.proc.stdin.close()
        except OSError:
            pass
        # stderr가 가득 차서 멈추지 않게 따로 읽어 둔다
        self._err: list[bytes] = []
        threading.Thread(target=lambda: self._err.extend(self.proc.stderr), daemon=True).start()

    # ── 명령 만들기 ──
    def _schema_file(self, schema: dict) -> str:
        f = Path(tempfile.mkstemp(prefix="study_schema_", suffix=".json")[1])
        f.write_text(json.dumps(schema, ensure_ascii=False), encoding="utf-8")
        self._tmp.append(f)
        return str(f)

    def _claude(self, path: str) -> list[str]:
        cmd = [path, "-p", "--strict-mcp-config", "--setting-sources", "project,local",
               "--tools", "" if self.bare else CLAUDE_TOOLS + (",WebSearch,WebFetch" if self.web else ""),
               "--output-format", "stream-json", "--verbose", "--include-partial-messages"]
        if self.web:  # 인터넷 도구는 이 실행에서만 허락 (설정 파일의 허용 목록은 그대로)
            cmd += ["--allowedTools", "WebSearch,WebFetch"]
        if self.choice["model"]:
            cmd += ["--model", self.choice["model"]]
        if self.schema:
            cmd += ["--json-schema", json.dumps(self.schema, ensure_ascii=False)]
        if self.session_id:
            cmd += ["--resume", self.session_id]
        return cmd

    def _codex(self, path: str) -> list[str]:
        cmd = [path, "exec"] + (["resume"] if self.session_id else [])
        cmd += ["--json", "--skip-git-repo-check",
                "-c", 'project_doc_fallback_filenames=["CLAUDE.md"]',
                "-c", 'approval_policy="never"',
                # 카드 저장(data/cards.json)·요약본(cache/) 때문에 작업 폴더 쓰기는 허용, 요약본은 AI를 다시 부르니 네트워크도
                "-c", f'sandbox_mode="{"read-only" if self.bare else "workspace-write"}"',
                "-c", "sandbox_workspace_write.network_access=true"]
        if self.bare:
            cmd += ["--ephemeral"]
        if self.choice["model"]:
            cmd += ["-m", self.choice["model"]]
        if self.choice["effort"]:
            cmd += ["-c", f'model_reasoning_effort="{self.choice["effort"]}"']
        if self.schema:
            cmd += ["--output-schema", self._schema_file(strict_schema(self.schema))]
        if self.session_id:
            cmd += [self.session_id.split(":", 1)[1]]
        return cmd + ["-"]

    def _agy(self, path: str, prompt: str) -> list[str]:
        if len(prompt) > 20000:  # 명령줄 길이 제한(약 32K자) → 긴 지시는 파일로 넘긴다
            f = ROOT / "cache" / "prompts" / f"{int(time.time() * 1000)}.md"
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(prompt, encoding="utf-8")
            self._tmp.append(f)
            prompt = f"`{f.relative_to(ROOT).as_posix()}` 파일을 읽고 그 안의 지시를 그대로 따르라. (파일 내용이 이번 요청 전체다)"
        cmd = [path, "-p", prompt, "--output-format", "stream-json"]
        if self.choice["model"]:
            cmd += ["--model", self.choice["model"]]
        if self.schema:
            cmd += ["--json-schema", self._schema_file(self.schema)]
        if self.session_id:
            cmd += ["--conversation", self.session_id.split(":", 1)[1]]
        return cmd

    # ── 결과 읽기 ──
    def events(self):
        import usage
        parse = {"claude": self._parse_claude, "gpt": self._parse_codex, "gemini": self._parse_agy}[self.engine]
        result = None
        try:
            for ev in parse():
                if ev["type"] == "result":
                    result = ev
                    msg = str(ev.get("result") or "")
                    if ev.get("is_error") and re.search(r"\b401\b|unauthori[sz]ed|not logged in|authentication required|bearer", msg, re.I):
                        ev["result"] = f"{ENGINES[self.engine]['cli']} 로그인이 필요해요. 'AI 설정'에서 로그인한 뒤 다시 해보세요.\n\n{msg[:300]}"
                    usage.log(self.kind, ev, self.engine)
                yield ev
            self.proc.wait()
            if result is None:
                err = b"".join(self._err).decode("utf-8", errors="replace").strip()
                msg = err[-800:] if err else f"{ENGINES[self.engine]['cli']}가 결과 없이 끝났어요 (종료 코드 {self.proc.returncode})"
                if re.search(r"auth|login|log in|credential|unauthori", err, re.I):
                    msg = f"{ENGINES[self.engine]['cli']} 로그인이 필요해요. 'AI 설정'에서 로그인하세요.\n\n{msg}"
                yield {"type": "result", "is_error": True, "result": msg, "errors": [msg], "session_id": self.session_id}
        finally:
            for f in self._tmp:
                try:
                    f.unlink()
                except OSError:
                    pass

    def _lines(self):
        for raw in self.proc.stdout:
            try:
                ev = json.loads(raw.decode("utf-8", errors="replace"))
            except json.JSONDecodeError:
                continue
            if isinstance(ev, dict):
                yield ev

    def _parse_claude(self):
        had_text = False
        tools: dict[int, dict] = {}
        for ev in self._lines():
            t = ev.get("type")
            if t == "system" and ev.get("subtype") == "init":
                yield {"type": "session", "session_id": ev.get("session_id")}
            elif t == "stream_event" and ev.get("parent_tool_use_id") is None:
                e = ev["event"]
                et = e.get("type")
                if et == "content_block_start":
                    block = e["content_block"]
                    if block["type"] == "text" and had_text:
                        yield {"type": "text", "delta": "\n\n"}
                    elif block["type"] == "tool_use":
                        tools[e["index"]] = {"name": block["name"], "json": ""}
                elif et == "content_block_delta":
                    d = e["delta"]
                    if d["type"] == "text_delta":
                        had_text = True
                        yield {"type": "text", "delta": d["text"]}
                    elif d["type"] == "input_json_delta" and e["index"] in tools:
                        tools[e["index"]]["json"] += d["partial_json"]
                elif et == "content_block_stop" and e.get("index") in tools:
                    tu = tools.pop(e["index"])
                    try:
                        inp = json.loads(tu["json"] or "{}")
                    except json.JSONDecodeError:
                        inp = {}
                    yield {"type": "tool", "name": tu["name"], "input": inp}
            elif t == "result":
                yield ev

    def _structured(self, text: str):
        if not self.schema or not text:
            return None
        m = re.search(r"\{.*\}", text, re.S)  # 앞뒤에 설명이나 ```json 이 붙어도 읽는다
        try:
            return json.loads(m.group(0)) if m else None
        except json.JSONDecodeError:
            return None

    def _parse_codex(self):
        sid, last, errors, had_text, done = self.session_id, "", [], False, False
        use = {"input": 0, "cached": 0, "output": 0}
        for ev in self._lines():
            t = ev.get("type")
            item = ev.get("item") or {}
            itype = item.get("type") or item.get("item_type")
            if t == "thread.started":
                sid = "codex:" + str(ev.get("thread_id"))
                yield {"type": "session", "session_id": sid}
            elif t == "item.started" and itype == "command_execution":
                yield {"type": "tool", "name": "Bash", "input": {"command": item.get("command", "")}}
            elif t == "item.started" and itype in ("mcp_tool_call", "web_search", "file_change"):
                yield {"type": "tool", "name": itype, "input": item}
            elif t == "item.completed" and itype in ("agent_message", "assistant_message"):
                text = item.get("text") or ""
                if text:
                    if had_text:
                        yield {"type": "text", "delta": "\n\n"}
                    had_text, last = True, text
                    yield {"type": "text", "delta": text}
            elif t == "turn.completed":
                done = True
                u = ev.get("usage") or {}
                use["input"] += u.get("input_tokens", 0)
                use["cached"] += u.get("cached_input_tokens", 0)
                use["output"] += u.get("output_tokens", 0)
            elif t in ("turn.failed", "error"):
                e = ev.get("error") if isinstance(ev.get("error"), dict) else ev
                msg = str(e.get("message") or ev)
                if t == "turn.failed" or not re.search(r"reconnect|retry", msg, re.I):
                    errors.append(msg)
        self.proc.wait()
        if not done and not errors and not last:
            return  # 아무것도 못 받음 → events()가 stderr로 알려준다
        failed = bool(errors) and not done
        yield {"type": "result", "is_error": failed, "result": (errors[-1] if failed else last), "errors": errors,
               "session_id": sid, "structured_output": None if failed else self._structured(last),
               "usage": {"input_tokens": use["input"] - use["cached"], "cache_read_input_tokens": use["cached"],
                         "output_tokens": use["output"]},
               "modelUsage": {self.choice["model"] or "codex-default": {}}, "total_cost_usd": 0}

    def _parse_agy(self):
        sid, had_text, last_step, seen_tools = self.session_id, False, None, set()
        for ev in self._lines():
            t = ev.get("type")
            p = ev.get(t) if isinstance(ev.get(t), dict) else ev
            cid = p.get("conversation_id")
            if cid and sid != f"gemini:{cid}":
                sid = f"gemini:{cid}"
                yield {"type": "session", "session_id": sid}
            if t == "step_update":
                step, stype = p.get("step_index"), p.get("step_type")
                if stype == "agent_response" and p.get("text_delta"):
                    if had_text and step != last_step:
                        yield {"type": "text", "delta": "\n\n"}
                    had_text, last_step = True, step
                    yield {"type": "text", "delta": p["text_delta"]}
                elif stype == "tool" and step not in seen_tools:
                    seen_tools.add(step)
                    yield {"type": "tool", "name": p.get("tool_name") or "tool", "input": p.get("tool_info") or {}}
            elif t == "result":
                status = str(p.get("status") or "")
                err = p.get("error")
                failed = status.upper() != "SUCCESS"
                text = p.get("response") or ""
                u = p.get("usage") or {}
                msg = (err.get("message") if isinstance(err, dict) else err) or status
                yield {"type": "result", "is_error": failed, "result": (str(msg) if failed else text),
                       "errors": [str(msg)] if failed else [], "session_id": sid,
                       "structured_output": p.get("structured_output") or self._structured(text),
                       "usage": {"input_tokens": u.get("input_tokens", 0) - u.get("cache_read_tokens", 0),
                                 "cache_read_input_tokens": u.get("cache_read_tokens", 0),
                                 "output_tokens": u.get("output_tokens", 0) + u.get("thinking_tokens", 0)},
                       "modelUsage": {self.choice["model"] or "antigravity-default": {}}, "total_cost_usd": 0}


def start(kind: str, prompt: str, session_id: str | None = None, schema: dict | None = None, bare: bool = False,
          web: bool = False) -> Run:
    return Run(kind, prompt, session_id, schema, bare, web)


def run_once(kind: str, prompt: str, schema: dict | None = None, timeout: float = 600) -> dict:
    """도구 없이 한 번 묻고 결과(result 이벤트)를 돌려준다. 채점·요약본용 (프로젝트 밖 임시 폴더에서 실행)."""
    r = start(kind, prompt, schema=schema, bare=True)
    killed = threading.Event()

    def kill():
        if r.proc.poll() is None:
            killed.set()
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(r.proc.pid)], capture_output=True, creationflags=NO_WINDOW)
    timer = threading.Timer(timeout, kill)
    timer.start()
    try:
        result = {}
        for ev in r.events():
            if ev["type"] == "result":
                result = ev
        if killed.is_set():
            raise TimeoutError(f"{timeout:.0f}초 안에 끝나지 않았어요")
        return result
    finally:
        timer.cancel()
