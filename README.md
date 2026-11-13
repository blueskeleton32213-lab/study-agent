# 학습 튜터

내 강의자료(PDF·PPTX·DOCX)를 근거로 질문에 답하고, 퀴즈를 내고, 틀린 문제를 간격반복으로 다시 물어보는 개인 공부용 앱이에요.
[Claude Code](https://claude.com/claude-code)를 엔진으로 쓰기 때문에 Claude 구독 안에서 추가 요금 없이 돌아가요.

## 기능
| 메뉴 | 하는 일 |
|---|---|
| 질문하기 | 강의자료에서 찾아 출처(파일·쪽)를 달아 답해요. 출처를 누르면 원본 쪽이 오른쪽에 열려요 |
| 퀴즈 | 범위·난이도·유형을 골라 출제해요. **시험 스타일**을 고르면 과목의 중간고사·기출·문제지 형식을 분석해서 그대로 따라 내요. 교수님 출제 스타일을 직접 적을 수도 있어요 |
| 복습 | 틀린 문제가 카드로 쌓이고, SM-2 간격반복으로 다시 물어봐요 (Claude 호출 없이 바로 처리) |
| 스튜디오 | 시험대비 요약, 핵심 공식표, 기출 유형 분석, 용어집을 만들어 저장하고 인쇄·PDF로 내보내요 |
| 대화 기록 | 대화를 자동 저장하고, 과목별로 거르거나 검색해서 다시 열어 이어갈 수 있어요 |

그 밖에: 낮·밤 모드, 사이드바 접기(Ctrl+B), 과목 순서 끌어서 바꾸기, 과목 클릭으로 자료 목록 보기, 과목 삭제(휴지통으로 이동).

## 필요한 것
- Windows 10/11, Microsoft Edge
- Python 3.12 이상
- [Claude Code](https://claude.com/claude-code) 설치 및 로그인 (`claude` 명령이 동작해야 해요)

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
서버(127.0.0.1:8765)를 켜고 Edge 앱 창을 열어요. 바탕화면 바로가기를 만들려면 대상은 `pythonw.exe`, 인수는 `tools\launch.pyw`, 아이콘은 `tools\static\app.ico`로 지정하세요.
코드를 고친 뒤 앱을 다시 열면 실행 중인 예전 서버를 알아서 새 서버로 바꿔요.

## 자료 넣기
1. 왼쪽 **+** 버튼으로 과목을 만들어요.
2. PDF·PPTX·DOCX 파일을 과목 이름 위로 끌어다 놓으면 `materials/<과목>/`에 복사되고 텍스트가 추출돼요.
   (직접 넣었다면 `python tools/extract.py <과목>`)

## 폴더 구조
```
CLAUDE.md              튜터 규칙 (Claude Code가 읽음)
.claude/skills/        터미널용 스킬: /ingest /quiz /review /weak
.claude/settings.json  앱에서 쓰는 도구 권한
tools/
  app.py               앱 서버 (Python 표준 라이브러리만 사용)
  launch.pyw           실행기 (서버 + Edge 앱 창)
  extract.py           PDF·PPTX·DOCX → texts/ (쪽 번호, Word 수식은 LaTeX로)
  render.py            PDF 쪽 → 이미지 (수식·그림 확인용)
  srs.py               복습 카드·간격반복(SM-2)
  static/index.html    화면
```
아래 폴더는 개인 자료라 저장소에 올라가지 않아요 (`.gitignore`):
`materials/`(강의자료), `texts/`(추출본), `studio/`(정리 자료), `data/`(복습 카드·대화 기록·시험 스타일), `cache/`, `휴지통/`

## 주의
- 강의자료는 저작권이 있으니 저장소에 올리지 마세요.
- 서버는 내 PC(127.0.0.1)에서만 접속되고, 다른 웹사이트가 몰래 호출하지 못하도록 막혀 있어요.
