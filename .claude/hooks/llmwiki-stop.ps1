# llmwiki Stop 훅 — 턴 종료 전 위키 기록 점검
# 현재 a 모드: 매 턴 블록해서 Claude가 기록 여부를 스스로 판단하게 한다.
# b 모드로 전환하려면: 블록 전에 기록 신호(예: $in.last_assistant_message에 결정·수정·버그 관련
# 내용이 있는지)를 검사해, 신호가 없으면 exit 0으로 조용히 통과시키는 조건을 추가한다.
# 한글 출력이 깨지지 않도록 stdout을 UTF-8로 고정 (파일 자체도 UTF-8 BOM으로 저장할 것)
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$in = [Console]::In.ReadToEnd() | ConvertFrom-Json

# 이 훅이 이미 한 번 잡은 턴이면 그대로 통과 — 무한 루프 방지
if ($in.stop_hook_active) { exit 0 }

Write-Output '{"decision":"block","reason":"[llmwiki] 턴 종료 전 점검: 이번 턴에 결정·버그·아이디어·방향 전환이 있었다면 docs/ 위키의 관련 페이지와 log.md에 기록한 뒤 종료하라. 기록할 것이 없거나 이미 기록했다면 아무것도 하지 말고 그대로 종료하라."}'
exit 0
