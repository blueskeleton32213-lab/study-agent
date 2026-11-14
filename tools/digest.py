"""강의자료 요약본: 파일마다 한 번만 만들어 두고 다시 쓴다 (스튜디오·퀴즈가 원문 전체를 매번 읽지 않게).

사용법:
    python tools/digest.py "texts/과목/파일.md" ["texts/과목/파일2.md" ...]
출력: 파일마다 요약본 (쪽 번호 머리말 유지 → 출처를 그대로 달 수 있음)
캐시: cache/digests/<과목>/<파일>.md — 원문(texts)이 바뀌면 다시 만든다.
처음 만들 때는 파일당 30초~2분 걸린다 (가벼운 모델 haiku, 여러 파일은 동시에).
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
ROOT = Path(__file__).resolve().parent.parent
TEXTS = ROOT / "texts"
MATERIALS = ROOT / "materials"
CACHE = ROOT / "cache" / "digests"
CLAUDE = shutil.which("claude") or str(Path.home() / ".local" / "bin" / "claude.exe")
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
sys.path.insert(0, str(ROOT / "tools"))
import usage  # noqa: E402

PROMPT = """아래는 강의자료 「{name}」에서 추출한 텍스트다. 시험공부·정리 자료의 재료로 쓸 **요약본**을 만든다.

규칙:
- 원문 순서대로, 내용 덩어리마다 `### p.A–B 제목` 머리말. 쪽 표기는 원문 `## [..]` 머리말 그대로 (p.N / slide N / part N).
- 덩어리마다 핵심 개념·정의·공식·예시·비교를 bullet 2~6개. 원문 용어를 그대로 쓰고 영어 용어는 남긴다. 수식은 LaTeX `$...$`.
- 시험에 나올 만한 강조(중요, 반복, 예제, 교수 코멘트)는 ⭐로 표시.
- 원문에 `(🖼 ...)`가 있는 쪽은 머리말 끝에 🖼 (텍스트에 수식·그림이 빠졌다는 뜻).
- 강의 소개·출석·연락처·목차·인사 쪽은 한 줄로 줄이거나 뺀다.
- 원문에 없는 내용은 절대 넣지 않는다. 전체 길이는 원문의 1/5 이하.
- 출력은 요약본 markdown만 (앞뒤 설명 없이).

---
{text}"""


def source_name(md: Path) -> str:
    rel = md.relative_to(TEXTS)
    for ext in (".pdf", ".pptx", ".docx"):
        if (MATERIALS / rel).with_suffix(ext).exists():
            return rel.with_suffix(ext).name
    return md.name


def make(md: Path, out: Path) -> str:
    name = source_name(md)
    prompt = PROMPT.format(name=name, text=md.read_text(encoding="utf-8"))
    # 도구 없이, 프로젝트 밖(임시 폴더)에서 → CLAUDE.md·스킬 목록이 안 실려서 입력이 원문뿐
    r = subprocess.run([CLAUDE, "-p", "--model", "haiku", "--tools", "", "--strict-mcp-config", "--setting-sources", "local",
                        "--output-format", "json"], cwd=tempfile.gettempdir(), input=prompt.encode("utf-8"),
                       capture_output=True, creationflags=NO_WINDOW, timeout=600, env={**os.environ, "STUDY_APP": "1"})
    data = json.loads(r.stdout.decode("utf-8", errors="replace") or "{}")
    usage.log("digest", data)
    body = (data.get("result") or "").strip()
    if data.get("is_error") or not body:
        raise RuntimeError(body or r.stderr.decode("utf-8", errors="replace")[-300:] or "요약 실패")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(f"# {name} 요약본 (원문: texts/{md.relative_to(TEXTS).as_posix()})\n\n{body}\n", encoding="utf-8")
    return out.read_text(encoding="utf-8")


def get(arg: str) -> str:
    md = (ROOT / arg).resolve()
    if not md.is_relative_to(TEXTS.resolve()) or md.suffix != ".md" or not md.exists():
        return f"[없음] texts/ 안의 추출본이 아닙니다: {arg}\n"
    out = CACHE / md.relative_to(TEXTS.resolve())
    if out.exists() and out.stat().st_mtime >= md.stat().st_mtime:
        return out.read_text(encoding="utf-8")
    try:
        return make(md, out)
    except Exception as exc:  # 요약이 실패하면 원문을 직접 읽으라고 알린다
        return f"[요약 실패] {arg}: {exc} → 이 파일은 원문을 필요한 쪽만 Read로 읽을 것\n"


def main() -> None:
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    with ThreadPoolExecutor(max_workers=4) as pool:
        for text in pool.map(get, sys.argv[1:]):
            print(text, end="\n\n")


if __name__ == "__main__":
    main()
