"""인하대 iClass(learn.inha.ac.kr)에서 강의자료를 받아 materials/<과목>/에 넣는다. (브라우저 없이 requests로)

로그인 (비밀번호는 저장하지 않음, 쿠키만 data/iclass_state.json에 저장):
  python tools/iclass.py login      → 아이디·비밀번호를 물어봄 (비밀번호는 화면에 안 보임)
                                       앱에서 부를 때는 표준입력으로 "아이디\\n비밀번호\\n"을 넘긴다
  python tools/iclass.py courses    → 이번 학기 수강 과목 목록
  python tools/iclass.py sync [--dry] [과목 이름 일부 ...]  → 새 자료(PDF·PPTX·DOCX)만 materials/<과목>/에 받기
                                       (--dry: 받지 않고 목록만. 과목을 안 적으면 전부)
  python tools/iclass.py explore <과목 이름 일부>   → (개발용) 과목 페이지의 링크 구조 출력
로그인이 풀려 있으면 종료 코드 3과 함께 '로그인 필요'를 출력한다.

로그인 흐름 (확인한 것):
  login.php → 학교 SSO(idp.inha.ac.kr) 확인 → 자동 POST 폼(redirectPostForm)으로 iClass에 되돌아옴
  → 아이디·비밀번호를 /login/index.php로 POST (암호화 없음). 틀리면 login.php?errorcode=3
"""
from __future__ import annotations

import getpass
import json
import re
import sys
from html import unescape
from pathlib import Path
from urllib.parse import unquote, urlparse

import requests

ROOT = Path(__file__).resolve().parent.parent
STATE_FILE = ROOT / "data" / "iclass_state.json"
MATERIALS = ROOT / "materials"
BASE = "https://learn.inha.ac.kr"
NEED_LOGIN = 3
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0 Safari/537.36"

sys.stdout.reconfigure(encoding="utf-8")


def new_session() -> requests.Session:
    s = requests.Session()
    s.headers["User-Agent"] = UA
    if STATE_FILE.exists():
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict):  # 예전 Playwright 형식 {"cookies": [...], "origins": [...]}
            data = data.get("cookies", [])
        for c in data:
            s.cookies.set(c["name"], c["value"], domain=c["domain"], path=c["path"])
    return s


def save_state(s: requests.Session) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    cookies = [{"name": c.name, "value": c.value, "domain": c.domain, "path": c.path} for c in s.cookies]
    STATE_FILE.write_text(json.dumps(cookies, ensure_ascii=False), encoding="utf-8")


def follow(s: requests.Session, r: requests.Response) -> requests.Response:
    """SSO가 주는 자동 POST 폼(브라우저라면 JS가 바로 제출)을 대신 제출한다."""
    for _ in range(10):
        html = r.text
        m = re.search(r"<form[^>]*id=['\"]redirectPostForm['\"][^>]*action=['\"]([^'\"]+)['\"][\s\S]*?</form>", html)
        if not m:
            return r
        data = {unescape(n): unescape(v) for n, v in
                re.findall(r"<input[^>]*name=['\"]([^'\"]+)['\"][^>]*value=['\"]([^'\"]*)['\"]", m.group(0))}
        r = s.post(unescape(m.group(1)), data=data, timeout=30)
    return r


def get(s: requests.Session, url: str) -> requests.Response:
    return follow(s, s.get(url, timeout=30))


def is_main(r: requests.Response) -> bool:
    return r.url.startswith(BASE) and "/login" not in r.url and "logout.php" in r.text


def cmd_login() -> None:
    if sys.stdin.isatty():
        user = input("iClass 아이디(학번): ").strip()
        pw = getpass.getpass("비밀번호 (화면에 안 보여요): ")
    else:  # 앱에서 호출
        user, pw = (sys.stdin.readline().strip(), sys.stdin.readline().rstrip("\r\n"))
    s = requests.Session()
    s.headers["User-Agent"] = UA
    get(s, BASE + "/login.php")  # SSO 확인을 한 바퀴 돌려 로그인 칸까지 온다
    r = follow(s, s.post(BASE + "/login/index.php", data={"username": user, "password": pw}, timeout=30))
    del pw
    if "errorcode=3" in r.url or "errorcode%3D3" in r.url:
        print("로그인 실패: 아이디나 비밀번호가 맞지 않아요."); sys.exit(NEED_LOGIN)
    r = get(s, BASE + "/")
    if not is_main(r):
        print(f"로그인 확인 실패 (마지막 주소: {r.url[:120]})"); sys.exit(NEED_LOGIN)
    save_state(s)
    print("로그인 완료.")


def course_links(html: str) -> list[dict]:
    """대시보드에 보이는 강좌 링크 (course/view.php?id=N)."""
    seen, out = set(), []
    for href, inner in re.findall(r"<a[^>]*href=['\"]([^'\"]*/course/view\.php\?id=\d+)[^'\"]*['\"][^>]*>([\s\S]*?)</a>", html):
        cid = re.search(r"id=(\d+)", href).group(1)
        name = re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]+>", " ", inner))).strip()
        if cid not in seen and name:
            seen.add(cid)
            out.append({"id": cid, "name": name, "url": f"{BASE}/course/view.php?id={cid}"})
    return out


def logged_in_session() -> tuple[requests.Session, requests.Response]:
    s = new_session()
    r = get(s, BASE + "/")
    if not is_main(r):
        print("로그인 필요"); sys.exit(NEED_LOGIN)
    save_state(s)
    return s, r


def cmd_courses() -> None:
    _, r = logged_in_session()
    print(json.dumps(course_links(r.text), ensure_ascii=False, indent=1))


def cmd_explore(args: list[str]) -> None:
    s, r = logged_in_session()
    courses = course_links(r.text)
    targets = [c for c in courses if any(a in c["name"] for a in args)] if args else courses[:1]
    for c in targets:
        page = get(s, c["url"])
        print(f"\n=== {c['name']} {page.url}")
        for href, inner in re.findall(r"<a[^>]*href=['\"]([^'\"]*/mod/[^'\"]*)['\"][^>]*>([\s\S]*?)</a>", page.text):
            print(f"{unescape(href)}\t{re.sub(r'\s+', ' ', unescape(re.sub(r'<[^>]+>', ' ', inner))).strip()[:80]}")


SYNCED_FILE = ROOT / "data" / "iclass_synced.json"  # 이미 받은 iClass 파일 id (지운 파일을 다시 받지 않게)
KEEP = {".pdf", ".pptx", ".docx"}  # extract.py가 읽을 수 있는 것만 받는다


def subject_name(course: str) -> str | None:
    """'오프라인 학부 자연어처리[202602-AIE3004-001] 김도국' → '자연어처리'. 정규 강좌가 아니면 None."""
    m = re.match(r"^(?:오프라인|이러닝|온라인|블렌디드)?\s*(?:학부|대학원)?\s*(.+?)\s*\[\d{6}-[A-Z0-9]+-\d+\]", course)
    if not m:
        return None
    return re.sub(r"\s+", " ", re.sub(r'[\\/:*?"<>|]', " ", m.group(1))).strip()


def file_links(html: str) -> list[dict]:
    out, seen = [], set()
    for mid, inner in re.findall(r"<a[^>]*href=['\"][^'\"]*/mod/ubfile/view\.php\?id=(\d+)[^'\"]*['\"][^>]*>([\s\S]*?)</a>", html):
        if mid in seen:
            continue
        seen.add(mid)
        title = re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]+>", " ", inner))).strip()
        out.append({"id": mid, "title": re.sub(r"\s*파일$", "", title)})
    return out


def filename_of(r: requests.Response) -> str:
    cd = r.headers.get("content-disposition", "")
    m = re.search(r"filename\*=UTF-8''([^;]+)", cd, re.I)
    name = unquote(m.group(1)) if m else unquote(urlparse(r.url).path.rsplit("/", 1)[-1])
    return re.sub(r'[\\/:*?"<>|]', "_", name).strip() or "file"


def cmd_sync(args: list[str]) -> None:
    """새 자료만 받는다. --dry 면 받지 않고 목록만. 과목 이름 일부를 주면 그 과목만."""
    dry = "--dry" in args
    names = [a for a in args if a != "--dry"]
    s, r = logged_in_session()
    synced = set(json.loads(SYNCED_FILE.read_text(encoding="utf-8"))) if SYNCED_FILE.exists() else set()
    total, skipped_types, viewer_only = 0, {}, []
    for c in course_links(r.text):
        subj = subject_name(c["name"])
        if not subj or (names and not any(n in subj for n in names)):
            continue
        folder = MATERIALS / subj
        page = get(s, c["url"])
        new = [f for f in file_links(page.text) if f["id"] not in synced]
        print(f"\n[{subj}] iClass 파일 {len(file_links(page.text))}개, 새로 확인할 것 {len(new)}개", flush=True)
        for f in new:
            fr = s.get(f"{BASE}/mod/ubfile/view.php?id={f['id']}", timeout=60, stream=True)
            if "text/html" in fr.headers.get("content-type", ""):
                # 교수님이 다운로드를 막고 온라인 뷰어(열기)만 허용한 자료. 우회하지 않고 알려만 준다
                fr.close(); viewer_only.append(f"{subj}: {f['title']}")
                print(f"  ! {f['title']} (다운로드 막힘, iClass 뷰어로만 볼 수 있음)"); continue
            name = filename_of(fr)
            ext = Path(name).suffix.lower()
            if ext not in KEEP:
                fr.close(); skipped_types[ext or "(없음)"] = skipped_types.get(ext or "(없음)", 0) + 1
                print(f"  - {name} (형식 {ext or '없음'}은 안 받음)")
                if not dry:
                    synced.add(f["id"])
                continue
            dest = folder / name
            if dest.exists():
                fr.close(); print(f"  = {name} (이미 있음)")
                if not dry:
                    synced.add(f["id"])
                continue
            if dry:
                fr.close(); print(f"  + {name}"); total += 1; continue
            folder.mkdir(parents=True, exist_ok=True)
            tmp = dest.with_suffix(dest.suffix + ".part")
            with open(tmp, "wb") as out:
                for chunk in fr.iter_content(1 << 16):
                    out.write(chunk)
            tmp.replace(dest)
            synced.add(f["id"])
            total += 1
            print(f"  + {name}", flush=True)
            SYNCED_FILE.write_text(json.dumps(sorted(synced)), encoding="utf-8")
    if not dry:
        SYNCED_FILE.parent.mkdir(parents=True, exist_ok=True)
        SYNCED_FILE.write_text(json.dumps(sorted(synced)), encoding="utf-8")
        save_state(s)
    print(f"\n{'받을' if dry else '새로 받은'} 자료 {total}개"
          + (f" / 안 받은 형식: {', '.join(f'{k} {v}개' for k, v in skipped_types.items())}" if skipped_types else "")
          + (f" / 다운로드 막힌 자료 {len(viewer_only)}개 (직접 넣어야 함)" if viewer_only else ""))


def main() -> None:
    cmd, *args = sys.argv[1:] or ["courses"]
    if cmd == "login":
        cmd_login()
    elif cmd == "courses":
        cmd_courses()
    elif cmd == "sync":
        cmd_sync(args)
    elif cmd == "explore":
        cmd_explore(args)
    else:
        print(f"알 수 없는 명령: {cmd}"); sys.exit(2)


if __name__ == "__main__":
    main()
