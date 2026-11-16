# 새 컴퓨터에 학습 튜터 설치. 설치 마법사(학습 튜터 설치.exe, setup/installer.py)가 -Wizard 로 부른다.
# 1) Python·Git이 없으면 winget으로 설치  2) GitHub에서 코드 받기(이미 있으면 최신으로)
# 3) 패키지 설치  4) 개인 자료를 OneDrive에서 받아오기  5) 바탕화면 바로가기  6) 실행
# -Wizard: 멈춰서 기다리지 않고, 마지막 실행은 마법사가 한다 (LAUNCH| 줄로 알려줌)
param([switch]$Wizard)
$ErrorActionPreference = "Stop"
try { [Console]::OutputEncoding = [Text.Encoding]::UTF8; $Host.UI.RawUI.WindowTitle = "학습 튜터 설치" } catch {}
$env:GIT_TERMINAL_PROMPT = "0"  # 보이지 않는 창에서 git이 아이디를 묻고 멈추지 않게 (로그인은 GitHub 로그인 창으로)
$repo = "https://github.com/blueskeleton32213-lab/study-agent.git"
$dir = Join-Path $env:USERPROFILE "study-agent"

function Step($msg) { Write-Host ""; Write-Host "▶ $msg" -ForegroundColor Cyan }
function Fail($msg) {
    Write-Host ""; Write-Host "✖ $msg" -ForegroundColor Red
    if (-not $Wizard) { Read-Host "Enter를 누르면 창이 닫혀요" }
    exit 1
}
function RefreshPath {
    $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [Environment]::GetEnvironmentVariable("Path", "User")
}
function HasPython {
    try { $v = & python --version 2>&1; return "$v" -match "Python 3\.(1[2-9]|[2-9]\d)" } catch { return $false }
}
function HasGit { return [bool](Get-Command git -ErrorAction SilentlyContinue) }
function WingetInstall($id, $name) {
    if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
        Fail "$name 이(가) 없어요. 직접 설치한 뒤 이 파일을 다시 실행해 주세요."
    }
    Write-Host "  $name 설치 중... (설치 창이 뜨면 '예'를 눌러 주세요)"
    winget install --id $id -e --silent --accept-package-agreements --accept-source-agreements | Out-Host
    RefreshPath
}

try {
    Step "Python·Git 확인"
    if (-not (HasPython)) { WingetInstall "Python.Python.3.12" "Python" }
    if (-not (HasPython)) { Fail "Python 설치를 확인하지 못했어요. 컴퓨터를 다시 시작한 뒤 한 번 더 실행해 주세요." }
    if (-not (HasGit)) { WingetInstall "Git.Git" "Git" }
    if (-not (HasGit)) { Fail "Git 설치를 확인하지 못했어요. 컴퓨터를 다시 시작한 뒤 한 번 더 실행해 주세요." }
    Write-Host "  OK"

    Step "코드 받기 → $dir"
    if (Test-Path (Join-Path $dir ".git")) {
        git -C $dir pull --ff-only | Out-Host
    } else {
        Write-Host "  처음이면 GitHub 로그인 창이 떠요. 로그인해 주세요 (비공개 저장소)."
        git clone $repo $dir | Out-Host
        if ($LASTEXITCODE) { Fail "코드를 받지 못했어요. GitHub 로그인을 확인해 주세요." }
    }

    Step "필요한 패키지 설치 (처음엔 1~2분)"
    python -m pip install --disable-pip-version-check -q -r (Join-Path $dir "requirements.txt") | Out-Host
    if ($LASTEXITCODE) { Fail "패키지 설치에 실패했어요." }

    Step "개인 자료를 OneDrive에서 받아오기"
    New-Item -ItemType Directory -Force (Join-Path $dir "local") | Out-Null
    python (Join-Path $dir "tools\onedrive_sync.py") | Out-Host
    if ($LASTEXITCODE) { Fail "OneDrive에서 자료를 받지 못했어요. OneDrive 동기화가 끝났는지 확인해 주세요." }

    Step "바탕화면 바로가기 만들기"
    $pythonw = Join-Path (Split-Path (& python -c "import sys; print(sys.executable)")) "pythonw.exe"
    $lnk = Join-Path ([Environment]::GetFolderPath("Desktop")) "학습 튜터.lnk"
    $s = (New-Object -ComObject WScript.Shell).CreateShortcut($lnk)
    $s.TargetPath = $pythonw
    $s.Arguments = "`"$dir\tools\launch.pyw`""
    $s.WorkingDirectory = $dir
    $s.IconLocation = "$dir\tools\static\app.ico"
    $s.Save()
    Write-Host "  $lnk"

    Step "끝! 학습 튜터를 실행할게요"
    Write-Host "  - iClass는 이 컴퓨터에서 한 번 다시 로그인해 주세요."
    Write-Host "  - AI(Claude 등)가 이 컴퓨터에 없으면 앱의 'AI 설정'에서 설치·로그인 안내를 볼 수 있어요."
    Write-Host "  - 다른 컴퓨터에서 학습 튜터를 켜 둔 채로 동시에 쓰지 마세요."
    Write-Host "LAUNCH|$pythonw|$dir"
    if (-not $Wizard) {
        Start-Process $pythonw -ArgumentList "`"$dir\tools\launch.pyw`"" -WorkingDirectory $dir
        Start-Sleep 3
    }
} catch {
    Fail "예상하지 못한 오류: $_"
}
