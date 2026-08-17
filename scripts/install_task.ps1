# 미니PC 상주 등록 — 로그온 시 파이프라인 스케줄러를 자동 시작한다.
# 실행(관리자 불필요): powershell -ExecutionPolicy Bypass -File scripts\install_task.ps1
# 해제: schtasks /Delete /TN "naverblog_ssalmuk" /F

$proj = Split-Path -Parent $PSScriptRoot
$action = "cmd /c cd /d `"$proj`" && uv run python -m src.scheduler >> data\scheduler.log 2>&1"

schtasks /Create /TN "naverblog_ssalmuk" /TR $action /SC ONLOGON /F
if ($LASTEXITCODE -eq 0) {
    Write-Host "등록 완료 — 다음 로그온부터 자동 시작됩니다."
    Write-Host "지금 바로 시작하려면: schtasks /Run /TN naverblog_ssalmuk"
} else {
    Write-Host "등록 실패 — 관리자 권한으로 다시 시도해 보세요."
}
