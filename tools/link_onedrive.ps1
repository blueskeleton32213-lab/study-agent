# 예전 설치 마법사(학습 튜터 설치.exe)가 부르는 이름이라 남겨 둔 파일.
# 이제는 정션 대신 로컬 폴더 + 동기화 방식이라, OneDrive 내용을 로컬로 받아오는 동기화만 한 번 돌린다.
#   (앱을 켤 때·끌 때 launch.pyw가 tools\onedrive_sync.py를 자동으로 부른다)
$root = Split-Path $PSScriptRoot -Parent
New-Item -ItemType Directory -Force (Join-Path $root "local") | Out-Null
& python (Join-Path $PSScriptRoot "onedrive_sync.py")
exit $LASTEXITCODE
