# llmwiki SessionStart 훅 — 세션 시작 때 두 가지를 주입한다:
# ① docs/pending.md에서 재검토 시점(ISO 날짜)이 도래한 보류 안건 (조건형은 lint가 점검)
# ② docs/status.md(작업 보드)가 있으면 착수 전 읽기 상기
# 한글 출력이 깨지지 않도록 stdout을 UTF-8로 고정 (파일 자체도 UTF-8 BOM으로 저장할 것)
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)

if (Test-Path "docs/pending.md") {
    $today = Get-Date -Format 'yyyy-MM-dd'
    $due = Get-Content "docs/pending.md" -Encoding UTF8 | Where-Object {
        $_ -match '\|' -and $_ -match '(\d{4}-\d{2}-\d{2})' -and $matches[1] -le $today
    }
    if ($due) {
        Write-Output "[llmwiki] 재검토 시점이 도래한 보류 안건이 있다:"
        $due | ForEach-Object { Write-Output $_ }
        Write-Output "사용자에게 이 안건의 재검토 미팅 시작을 제안하라. 재검토가 끝나면 pending.md에서 해당 행을 결정 페이지로 옮긴다."
    }
}

if (Test-Path "docs/status.md") {
    Write-Output "[llmwiki] 작업 착수 전 docs/status.md(작업 보드)를 먼저 읽어라 — 진행 중·미해결이 거기 있다."
}
exit 0
