"""PDF의 특정 쪽을 PNG 이미지로 바꿔서, 수식·그래프·회로도를 직접 볼 수 있게 한다.

사용법:
    python tools/render.py "materials/과목/파일.pdf" 2        # 2쪽
    python tools/render.py "materials/과목/파일.pdf" 2-4      # 2~4쪽 (최대 6쪽)
출력: 만들어진 PNG 경로 (cache/pages/ 아래, 이미 있으면 재사용)
"""
import sys
from pathlib import Path

import pymupdf

sys.stdout.reconfigure(encoding="utf-8")
ROOT = Path(__file__).resolve().parent.parent
CACHE = ROOT / "cache" / "pages"
MAX_PAGES = 6


def main() -> None:
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    pdf = (ROOT / sys.argv[1]).resolve()
    if not pdf.is_relative_to(ROOT / "materials") or pdf.suffix.lower() != ".pdf" or not pdf.exists():
        sys.exit(f"materials/ 안의 PDF가 아닙니다: {sys.argv[1]}")
    a, _, b = sys.argv[2].partition("-")
    first, last = int(a), int(b or a)
    if last - first + 1 > MAX_PAGES:
        sys.exit(f"한 번에 최대 {MAX_PAGES}쪽까지")
    out_dir = CACHE / pdf.parent.name
    out_dir.mkdir(parents=True, exist_ok=True)
    with pymupdf.open(pdf) as doc:
        for n in range(first, last + 1):
            if not 1 <= n <= len(doc):
                print(f"{n}쪽 없음 (전체 {len(doc)}쪽)")
                continue
            out = out_dir / f"{pdf.stem}_p{n}.png"
            if not out.exists() or out.stat().st_mtime < pdf.stat().st_mtime:
                doc[n - 1].get_pixmap(dpi=110).save(out)
            print(out.relative_to(ROOT))


if __name__ == "__main__":
    main()
