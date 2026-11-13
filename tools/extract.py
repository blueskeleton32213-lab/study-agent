"""materials/<과목>/ 의 PDF·PPTX를 texts/<과목>/<파일명>.md 로 추출한다.

각 페이지/슬라이드 앞에 `## [p.N]` / `## [slide N]` 머리말을 붙여서
검색 결과에서 바로 출처(파일명 + 페이지)를 알 수 있게 한다.
이미 추출됐고 원본이 바뀌지 않은 파일은 건너뛴다.

사용법:
    python tools/extract.py            # 전체
    python tools/extract.py 운영체제    # 특정 과목만
    python tools/extract.py --force    # 전부 다시 추출
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parent.parent
MATERIALS = ROOT / "materials"
TEXTS = ROOT / "texts"
# 페이지당 글자 수가 이보다 적으면 스캔본(이미지)일 가능성이 높다
SCAN_THRESHOLD = 30


def extract_pdf(path: Path) -> tuple[list[str], int]:
    import pymupdf

    parts, empty = [], 0
    with pymupdf.open(path) as doc:
        for i, page in enumerate(doc, 1):
            text = page.get_text().strip()
            if len(text) < SCAN_THRESHOLD:
                empty += 1
            # 수식·그래프·회로도는 대부분 이미지라 텍스트로 안 나온다 → 원본을 봐야 한다는 표시
            n_img = len(page.get_images())
            mark = f"\n(🖼 그림/수식 이미지 {n_img}개 — 수식·그림이 필요하면 원본 PDF p.{i}를 직접 볼 것)" if n_img else ""
            parts.append(f"## [p.{i}]\n{text}{mark}\n")
    return parts, empty


def extract_pptx(path: Path) -> tuple[list[str], int]:
    from pptx import Presentation

    parts, empty = [], 0
    for i, slide in enumerate(Presentation(path).slides, 1):
        lines = []
        for shape in slide.shapes:
            if shape.has_text_frame:
                lines += [p.text for p in shape.text_frame.paragraphs if p.text.strip()]
            if getattr(shape, "has_table", False) and shape.has_table:
                for row in shape.table.rows:
                    lines.append(" | ".join(c.text.strip() for c in row.cells))
        if slide.has_notes_slide:
            notes = slide.notes_slide.notes_text_frame.text.strip()
            if notes:
                lines.append(f"(발표자 노트) {notes}")
        text = "\n".join(lines).strip()
        if len(text) < SCAN_THRESHOLD:
            empty += 1
        parts.append(f"## [slide {i}]\n{text}\n")
    return parts, empty


W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
M = "{http://schemas.openxmlformats.org/officeDocument/2006/math}"


FUNCS = {"lim", "log", "ln", "exp", "sin", "cos", "tan", "max", "min", "sup", "inf", "arg", "det", "Pr"}
NARY = {"∑": r"\sum", "∏": r"\prod", "∫": r"\int", "∬": r"\iint", "∮": r"\oint", "⋃": r"\bigcup", "⋂": r"\bigcap"}
ACCENT = {"̂": r"\hat", "̄": r"\bar", "̇": r"\dot", "̃": r"\tilde", "⃗": r"\vec", "→": r"\vec"}
HANGUL = re.compile(r"[가-힣ㄱ-ㅎ]")


def omml(el) -> str:
    """Word 수식(OMML)을 LaTeX로. 예: \\frac{a}{b}, x^{2}, \\sum_{k=0}^{n} a_{k}
    → 텍스트 검색도 되고, 앱 화면에서는 KaTeX로 수식 그대로 그려진다."""
    tag = el.tag.replace(M, "")

    def ch(name):
        c = el.find(M + name)
        return omml(c) if c is not None else ""

    def prop(pr, name, default):
        p = el.find(f"{M}{pr}/{M}{name}")
        return p.get(M + "val", default) if p is not None else default

    def delim(c, side):
        return {"{": r"\{", "}": r"\}", "": "."}.get(c, c) if side else c

    if tag == "r":
        text = "".join(t.text or "" for t in el.iter(M + "t"))
        if text.strip() in FUNCS:
            return "\\" + text.strip() + " "
        if HANGUL.search(text):
            return r"\text{" + text + "}"
        return text.replace("%", r"\%").replace("&", r"\&").replace("#", r"\#")
    if tag == "f":
        if prop("fPr", "type", "bar") == "lin":
            return f"{ch('num')}/{ch('den')}"
        return rf"\frac{{{ch('num')}}}{{{ch('den')}}}"
    if tag == "sSup":
        return f"{{{ch('e')}}}^{{{ch('sup')}}}"
    if tag == "sSub":
        return f"{{{ch('e')}}}_{{{ch('sub')}}}"
    if tag == "sSubSup":
        return f"{{{ch('e')}}}_{{{ch('sub')}}}^{{{ch('sup')}}}"
    if tag == "sPre":
        return f"{{}}_{{{ch('sub')}}}^{{{ch('sup')}}}{ch('e')}"
    if tag == "rad":
        deg = ch("deg")
        return rf"\sqrt{{{ch('e')}}}" if not deg else rf"\sqrt[{deg}]{{{ch('e')}}}"
    if tag == "nary":
        sym = prop("naryPr", "chr", "∫")
        sub, sup = ch("sub"), ch("sup")
        return f"{NARY.get(sym, sym)}{'_{' + sub + '}' if sub else ''}{'^{' + sup + '}' if sup else ''} {ch('e')}"
    if tag == "d":
        beg, end, sep = prop("dPr", "begChr", "("), prop("dPr", "endChr", ")"), prop("dPr", "sepChr", "|")
        inner = (r" \mid " if sep == "|" else sep).join(omml(e) for e in el.findall(M + "e"))
        return rf"\left{delim(beg, 1)}{inner}\right{delim(end, 1)}"
    if tag == "func":
        return f"{ch('fName')}{{{ch('e')}}}"
    if tag == "limLow":
        return f"{ch('e')}_{{{ch('lim')}}}"
    if tag == "limUpp":
        return f"{ch('e')}^{{{ch('lim')}}}"
    if tag == "acc":
        return f"{ACCENT.get(prop('accPr', 'chr', '̂'), r'\hat')}{{{ch('e')}}}"
    if tag == "bar":
        return rf"\overline{{{ch('e')}}}"
    if tag == "m":
        rows = [" & ".join(omml(e) for e in r.findall(M + "e")) for r in el.findall(M + "mr")]
        return r"\begin{matrix}" + r" \\ ".join(rows) + r"\end{matrix}"
    if tag == "eqArr":
        return r"\begin{aligned}" + r" \\ ".join(omml(e) for e in el.findall(M + "e")) + r"\end{aligned}"
    # oMath, e, num, box 등: 속성(*Pr)을 빼고 자식을 이어 붙임
    return "".join(omml(c) for c in el if not c.tag.endswith("Pr"))


def para_text(p) -> str:
    out = []
    for node in p.iter():
        if node.tag == W + "t" and not any(a.tag.startswith(M) for a in node.iterancestors()):
            out.append(node.text or "")
        elif node.tag == W + "tab":
            out.append(" ")
        elif node.tag in (M + "oMath",) and not any(a.tag == M + "oMath" for a in node.iterancestors()):
            out.append(f" ${omml(node).strip()}$ ")
        elif node.tag.endswith("}drawing") or node.tag.endswith("}pict"):
            out.append(" [그림] ")
    return re.sub(r"[ \t]+", " ", "".join(out)).strip()


def extract_docx(path: Path) -> tuple[list[str], int]:
    """Word 문서는 쪽 정보가 없어서 제목(Heading) 단위로, 제목이 없으면 약 1500자 단위로 나눈다.
    수식(OMML)은 $...$ 안에 한 줄 텍스트로 변환한다."""
    from docx import Document

    doc = Document(path)
    styles = {s.style_id: (s.name or "") for s in doc.styles}
    lines: list[tuple[bool, str]] = []
    for block in doc.element.body:
        if block.tag == W + "p":
            text = para_text(block)
            if text:
                sid = block.find(f"{W}pPr/{W}pStyle")
                name = styles.get(sid.get(W + "val"), "") if sid is not None else ""
                lines.append((name.lower().startswith(("heading", "title", "제목")), text))
        elif block.tag == W + "tbl":
            for row in block.iter(W + "tr"):
                cells = [" ".join(para_text(p) for p in tc.iter(W + "p")).strip() for tc in row.iter(W + "tc")]
                if any(cells):
                    lines.append((False, " | ".join(cells)))

    parts, buf, title, n = [], [], "", 0

    def flush():
        nonlocal buf, n
        if buf:
            n += 1
            parts.append(f"## [part {n}{' · ' + title if title else ''}]\n" + "\n".join(buf) + "\n")
            buf = []

    for is_heading, text in lines:
        if is_heading:
            flush()
            title = text[:40]
        buf.append(text)
        if sum(len(b) for b in buf) > 1500:
            flush()
    flush()
    return parts, 0


EXTRACTORS = {".pdf": extract_pdf, ".pptx": extract_pptx, ".docx": extract_docx}


def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    force = "--force" in sys.argv
    subjects = [MATERIALS / a for a in args] if args else sorted(p for p in MATERIALS.iterdir() if p.is_dir())

    done = skipped = 0
    for subject in subjects:
        if not subject.is_dir():
            print(f"[없음] 과목 폴더가 없습니다: {subject}")
            continue
        for src in sorted(subject.rglob("*")):
            ext = src.suffix.lower()
            if ext not in EXTRACTORS:
                if src.is_file() and ext in {".ppt", ".hwp", ".hwpx", ".doc"}:
                    print(f"[미지원] {src.relative_to(MATERIALS)} → PDF로 변환해서 넣어주세요")
                continue
            out = TEXTS / src.relative_to(MATERIALS).with_suffix(".md")
            if not force and out.exists() and out.stat().st_mtime >= src.stat().st_mtime:
                skipped += 1
                continue
            try:
                parts, empty = EXTRACTORS[ext](src)
            except Exception as exc:  # 깨진 파일 하나 때문에 전체가 멈추지 않게
                print(f"[실패] {src.relative_to(MATERIALS)}: {exc}")
                continue
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(f"# {src.name}\n\n" + "\n".join(parts), encoding="utf-8")
            done += 1
            warn = ""
            if parts and empty / len(parts) > 0.5:
                warn = f"  ⚠ {empty}/{len(parts)}쪽에 글자가 거의 없음 (스캔본일 수 있음 → 원본 PDF를 직접 읽어야 함)"
            print(f"[추출] {src.relative_to(MATERIALS)} ({len(parts)}쪽){warn}")
    print(f"완료: 새로 추출 {done}개, 변경 없음 {skipped}개")


if __name__ == "__main__":
    main()
