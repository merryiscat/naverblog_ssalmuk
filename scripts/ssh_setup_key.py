"""배포 준비 — 서버에 이 PC의 공개키를 등록해 이후 키 인증으로 전환한다.

실행: uv run python scripts/ssh_setup_key.py
자격은 .env(SSH_HOST/USER/PASSWORD)에서 읽는다 (일회성 — 이후엔 키로 접속).
"""

import os
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import paramiko

from src import config  # noqa: F401 — .env 로드를 위해

HOST = os.getenv("SSH_HOST")
USER = os.getenv("SSH_USER")
PW = os.getenv("SSH_PASSWORD")

pubkey = (Path.home() / ".ssh" / "id_ed25519.pub").read_text(encoding="utf-8").strip()

client = paramiko.SSHClient()
client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
client.connect(HOST, username=USER, password=PW, timeout=10)

cmd = (f"mkdir -p ~/.ssh && chmod 700 ~/.ssh && "
       f"grep -qF '{pubkey}' ~/.ssh/authorized_keys 2>/dev/null || "
       f"echo '{pubkey}' >> ~/.ssh/authorized_keys; chmod 600 ~/.ssh/authorized_keys; "
       f"echo done; uname -a; free -m | sed -n 2p")
stdin, stdout, stderr = client.exec_command(cmd)
print(stdout.read().decode())
err = stderr.read().decode()
if err:
    print("stderr:", err)
client.close()
