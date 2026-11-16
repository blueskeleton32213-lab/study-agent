# 두 번째 컴퓨터(노트북 등)에서 한 번만 실행: 개인 자료 폴더를 OneDrive의 "학습 튜터 자료"와 연결한다.
#   powershell -ExecutionPolicy Bypass -File tools\link_onedrive.ps1
# - 이미 연결돼 있으면 건너뛴다.
# - 이 컴퓨터에 같은 이름의 진짜 폴더가 있으면 지우지 않고 "<이름>.이전" 으로 바꿔 둔다.
# - OneDrive 폴더는 "항상 이 장치에 유지"로 바꿔서 인터넷이 없을 때도 열리게 한다.
$ErrorActionPreference = "Stop"
$root = Split-Path $PSScriptRoot -Parent
$onedrive = if ($env:OneDrive) { $env:OneDrive } else { Join-Path $env:USERPROFILE "OneDrive" }
$dst = Join-Path $onedrive "학습 튜터 자료"
if (-not (Test-Path $dst)) {
    Write-Host "OneDrive에 '학습 튜터 자료' 폴더가 아직 없어요: $dst"
    Write-Host "OneDrive에 로그인하고 동기화가 끝난 뒤 다시 실행하세요."
    exit 1
}
attrib +P -U "$dst" /S /D | Out-Null   # 모든 파일을 이 컴퓨터에 내려받아 두기
foreach ($d in "materials", "texts", "studio", "data", "cache", "notes") {
    $here = Join-Path $root $d
    $there = Join-Path $dst $d
    if (-not (Test-Path $there)) { New-Item -ItemType Directory -Force $there | Out-Null }
    if (Test-Path $here) {
        $item = Get-Item $here -Force
        if ($item.LinkType -eq "Junction") { Write-Host "$d : 이미 연결됨"; continue }
        Rename-Item $here "$($d).이전"
        Write-Host "$d : 원래 폴더를 $($d).이전 으로 바꿔 둠 (필요 없으면 지워도 돼요)"
    }
    New-Item -ItemType Junction -Path $here -Target $there | Out-Null
    Write-Host "$d : 연결함 → $there"
}
New-Item -ItemType Directory -Force (Join-Path $root "local") | Out-Null
Write-Host "끝. 이제 학습 튜터를 실행하세요. (iClass는 이 컴퓨터에서 한 번 다시 로그인)"
