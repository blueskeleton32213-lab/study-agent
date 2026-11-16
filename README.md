# 학습 튜터

내 강의자료(PDF·PPTX·DOCX)를 근거로 질문에 답하고, 퀴즈를 내고, 틀린 문제를 간격반복으로 다시 물어보는 개인 공부용 앱이에요.
AI 엔진은 **Claude**([Claude Code](https://claude.com/claude-code)), **GPT**([Codex CLI](https://github.com/openai/codex)), **Gemini**([Antigravity CLI](https://antigravity.google/docs/cli/headless/)) 중에서 골라요. 각 서비스에 로그인한 구독으로 돌아가서 추가 요금이 없어요 (Claude는 API 키 요금제도 가능).

## 기능
| 메뉴 | 하는 일 |
|---|---|
| 질문하기 | 강의자료에서 찾아 답해요. 출처는 평소 숨기고(헤더 「출처 표시」로 켜기), 문장을 드래그하면 참조한 자료가 떠요. 누르면 원본 쪽이 가운데 큰 창으로 열려요(확대·폭 맞춤·옆에 두기). 구조·흐름처럼 그림이 필요한 설명은 원본 쪽 이미지나 다이어그램(mermaid)으로 보여줘요 |
| 질문 창 | 퀴즈·정리 자료·답변에서 드래그 → 「이 부분 물어보기」로 보던 화면 그대로 바로 물어봐요 |
| 퀴즈 | 범위·난이도·유형을 골라 출제해요. **시험 스타일**을 고르면 과목의 중간고사·기출·문제지 형식을 분석해서 그대로 따라 내요. 교수님 출제 스타일을 직접 적을 수도 있어요. 만든 퀴즈는 **퀴즈 기록**에 남아서 이어 풀거나 결과·해설을 다시 볼 수 있어요 |
| 복습 | 틀린 문제가 카드로 쌓이고, SM-2 간격반복으로 다시 물어봐요 (AI 호출 없이 바로 처리). 복습할 때마다 **복습 기록**에 남고, 헷갈린 카드만 다시 볼 수 있어요 |
| 스튜디오 | 시험대비 요약, 핵심 공식표, 기출 유형 분석, 용어집을 만들어 저장하고 인쇄·PDF로 내보내요 |
| 대화 기록 | 대화를 자동 저장하고, 과목별로 거르거나 검색해서 다시 열어 이어갈 수 있어요 |
| AI 설정 | 사이드바 **AI 설정**에서 엔진(Claude·GPT·Gemini)과 요금제(Claude Pro/Max/API 키, GPT Plus/Pro, Gemini 무료/AI Pro)를 골라요. 요금제마다 기능별 모델이 정해져 있고, 기능별로 직접 바꿀 수도 있어요. 로그인 창 열기·설치 안내도 여기서 해요 (설정 파일: `data/ai.json`) |
| iClass 받기 | 인하대 iClass에서 수강 과목의 새 강의자료(PDF·PPTX·DOCX)를 받아요. 사이드바 ↓ 버튼이나 대화창에 "최신으로 받아줘". 비밀번호는 저장하지 않고 로그인 쿠키만 `local/`에 남겨요 (터미널: `python tools/iclass.py login` / `sync`) |

**토큰 절약:** 앱이 부르는 AI에는 튜터에 필요한 도구만 싣고, 요금제에 맞춰 가벼운 일(채점·요약본)은 가벼운 모델로 처리해요 (예: Claude Pro는 질문 Sonnet·채점 Haiku). 과목 목차(`cache/목차/`)와 강의별 요약본(`tools/digest.py`, 한 번 만들고 재사용)을 먼저 보고 필요한 쪽만 원문으로 읽어요. 과목이 바뀌거나 대화가 길어지면 새 대화를 권해요. 사용량은 `python tools/usage.py`로 확인해요.

그 밖에: 낮·밤 모드, 사이드바 접기(Ctrl+B), 과목 순서 끌어서 바꾸기, 과목 클릭으로 자료 목록 보기, 과목 삭제(Windows 휴지통으로 이동, 복원 가능).

## 필요한 것
- Windows 10/11 (WebView2 런타임, 기본 설치돼 있음)
- Python 3.12 이상
- 쓸 AI의 CLI 설치 및 로그인 (하나 이상, 앱의 **AI 설정**에서 상태 확인·로그인 창 열기)
  - Claude: [Claude Code](https://claude.com/claude-code) — `npm install -g @anthropic-ai/claude-code`
  - GPT: Codex CLI — `npm install -g @openai/codex` 후 `codex login` (ChatGPT Plus/Pro 계정)
  - Gemini: Antigravity CLI — PowerShell에서 `irm https://antigravity.google/cli/install.ps1 | iex` 후 `agy`를 한 번 실행해 로그인 (Gemini CLI는 2026년 6월부터 개인 구독 로그인을 지원하지 않아요)
  - Claude가 가장 잘 맞춰져 있어요. GPT·Gemini는 같은 규칙(`CLAUDE.md`)과 스킬 파일을 읽게 해 두었지만 출처 표기·형식이 조금 다를 수 있어요.

## 설치
```powershell
git clone <이 저장소 주소> study-agent
cd study-agent
python -m pip install -r requirements.txt
```

## 실행
```powershell
pythonw tools\launch.pyw
```
서버(127.0.0.1:8765)를 켜고 앱 창(pywebview, WebView2)을 열어요. 창을 닫으면 서버도 꺼지고, 다시 실행하면 열려 있는 창이 앞으로 와요. 바탕화면 바로가기를 만들려면 대상은 `pythonw.exe`, 인수는 `tools\launch.pyw`, 아이콘은 `tools\static\app.ico`로 지정하세요.
코드를 고친 뒤 앱을 다시 열면 실행 중인 예전 서버를 알아서 새 서버로 바꿔요.

### exe로 실행하기
```powershell
python -m pip install pyinstaller
python -m PyInstaller --noconfirm --onefile --noconsole --icon "$PWD\tools\static\app.ico" --name "학습 튜터" --distpath . tools\launch.pyw
```
프로젝트 폴더 맨 위에 `학습 튜터.exe`가 생겨요 (다른 곳으로 옮기지 말고 바로가기를 만드세요). exe에는 앱 창만 들어 있고 서버·도구는 설치된 Python(PATH의 `pythonw`)으로 돌아서, `tools/` 코드를 고쳐도 다시 만들 필요가 없어요. `launch.pyw`를 고쳤을 때만 다시 빌드하세요.

## 두 컴퓨터에서 이어 쓰기 (데스크탑 ↔ 노트북)
- **공부 기록·자료**(`materials/` `texts/` `studio/` `data/` `cache/` `notes/`)는 OneDrive의 `학습 튜터 자료` 폴더에 있고, 프로젝트 폴더에는 연결(정션)만 있어요. OneDrive가 알아서 동기화해요.
- **코드**는 GitHub으로 주고받아요. 앱을 켤 때 `git pull`(고치다 만 파일이 없을 때만), 창을 닫을 때 커밋해 둔 것만 `git push`를 자동으로 해요.
- **컴퓨터마다 따로**: `local/`(iClass 로그인 쿠키, 창 크기·위치). iClass는 컴퓨터마다 한 번씩 로그인하세요.
- 두 컴퓨터에서 **동시에 켜 두지 마세요.** 복습 카드 같은 파일이 서로 덮어써질 수 있어요. 한쪽을 닫고, OneDrive 동기화가 끝난 뒤(작업표시줄 구름 아이콘) 다른 쪽을 켜세요.

두 번째 컴퓨터 처음 설정:
```powershell
git clone <이 저장소 주소> study-agent
cd study-agent
python -m pip install -r requirements.txt
powershell -ExecutionPolicy Bypass -File tools\link_onedrive.ps1
```
(OneDrive에 같은 계정으로 로그인해서 `학습 튜터 자료`가 내려받아진 뒤에 실행하세요.)

## 자료 넣기
1. 왼쪽 **+** 버튼으로 과목을 만들어요.
2. PDF·PPTX·DOCX 파일을 과목 이름 위로 끌어다 놓으면 `materials/<과목>/`에 복사되고 텍스트가 추출돼요.
   (직접 넣었다면 `python tools/extract.py <과목>`)

## 폴더 구조
```
CLAUDE.md              튜터 규칙 (Claude Code가 읽고, Codex·Antigravity에도 넘겨줌)
.claude/skills/        터미널용 스킬: /ingest /quiz /review /weak, 그림 규칙(figure)
.claude/settings.json  앱에서 쓰는 도구 권한
tools/
  app.py               앱 서버 (Python 표준 라이브러리만 사용)
  launch.pyw           실행기 (서버 + pywebview 앱 창)
  extract.py           PDF·PPTX·DOCX → texts/ (쪽 번호, Word 수식은 LaTeX로)
  render.py            PDF 쪽 → 이미지 (수식·그림 확인용)
  digest.py            강의자료 요약본 (파일마다 한 번 만들어 재사용)
  engines.py           AI 엔진(Claude·GPT·Gemini) 실행·요금제별 모델 (설정: data/ai.json)
  usage.py             AI 사용량 기록·요약
  srs.py               복습 카드·간격반복(SM-2)
  link_onedrive.ps1    두 번째 컴퓨터에서 개인 자료 폴더를 OneDrive와 연결
  static/index.html    화면
```
아래 폴더는 개인 자료라 저장소에 올라가지 않아요 (`.gitignore`):
`materials/`(강의자료), `texts/`(추출본), `studio/`(정리 자료), `data/`(복습 카드·대화·퀴즈·복습 기록·시험 스타일), `cache/`, `notes/`(작업 기록) — OneDrive로 동기화
`local/`(iClass 로그인 쿠키, 창 위치) — 컴퓨터마다 따로

## 주의
- 강의자료는 저작권이 있으니 저장소에 올리지 마세요.
- 서버는 내 PC(127.0.0.1)에서만 접속되고, 다른 웹사이트가 몰래 호출하지 못하도록 막혀 있어요.
