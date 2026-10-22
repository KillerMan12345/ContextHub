#!/usr/bin/env python3
"""ContextHub Inbox 自动入库

两种运行方式：
1. cron: */5 * * * * python3 inbox_watcher.py
2. 常驻: python3 inbox_watcher.py --daemon  (每 60s 扫一次)

扫描 /data/inbox/ 下的新文件 → 调网关 /ingest/file 入库
→ 成功移入 done/，失败移入 failed/
"""

import os
import sys
import time
import shutil
import urllib.request
import mimetypes
import json
import uuid
from pathlib import Path

INBOX_DIR = Path("/data/inbox")
DONE_DIR = INBOX_DIR / "done"
FAILED_DIR = INBOX_DIR / "failed"
GATEWAY_URL = os.environ.get("GATEWAY_URL", "http://192.168.31.63:8000")
ENV_FILE = Path("/volume1/docker/contexthub/deploy/nas/.env")

SKIP_NAMES = {".DS_Store", "Thumbs.db", ".gitkeep"}
SKIP_SUFFIXES = {".tmp", ".part", ".crdownload", ".reason.txt"}


def get_token() -> str:
    for src in (ENV_FILE, Path("/app/.env")):
        if src.exists():
            for line in src.read_text().splitlines():
                if line.startswith("INBOX_TOKEN="):
                    return line.split("=", 1)[1].strip()
    return os.environ.get("INBOX_TOKEN", "test-token-001")


def ingest_file(fpath: Path, token: str) -> dict:
    boundary = f"----InboxBoundary{uuid.uuid4().hex}"
    fname = fpath.name
    ctype = mimetypes.guess_type(fname)[0] or "application/octet-stream"
    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{fname}"\r\n'
        f"Content-Type: {ctype}\r\n\r\n"
    ).encode() + fpath.read_bytes() + f"\r\n--{boundary}--\r\n".encode()
    req = urllib.request.Request(
        f"{GATEWAY_URL}/ingest/file",
        data=body,
        headers={
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Authorization": f"Bearer {token}",
        },
    )
    with urllib.request.urlopen(req, timeout=180) as r:
        return json.loads(r.read().decode())


def scan_once():
    for d in (INBOX_DIR, DONE_DIR, FAILED_DIR):
        d.mkdir(parents=True, exist_ok=True)
    token = get_token()
    files = [f for f in INBOX_DIR.iterdir()
             if f.is_file()
             and f.name not in SKIP_NAMES
             and f.suffix.lower() not in SKIP_SUFFIXES]
    if not files:
        return
    print(f"[inbox] 发现 {len(files)} 个文件", flush=True)
    for fpath in sorted(files):
        try:
            s1 = fpath.stat().st_size
            time.sleep(3)
            if fpath.stat().st_size != s1:
                print(f"[inbox] 跳过（正在写入）: {fpath.name}", flush=True)
                continue
            result = ingest_file(fpath, token)
            status = result.get("status", "UNKNOWN")
            print(f"[inbox] {fpath.name} → {status}", flush=True)
            dest = (DONE_DIR if status in
                    ("COMMITTED", "DUPLICATE", "NEEDS_REVIEW")
                    else FAILED_DIR)
            shutil.move(str(fpath), str(dest / fpath.name))
        except Exception as e:
            print(f"[inbox] {fpath.name} 失败: {e}", flush=True)
            try:
                shutil.move(str(fpath), str(FAILED_DIR / fpath.name))
            except Exception:
                pass


if __name__ == "__main__":
    if "--daemon" in sys.argv:
        print("[inbox] 常驻模式，每 60s 扫描", flush=True)
        while True:
            try:
                scan_once()
            except Exception as e:
                print(f"[inbox] 错误: {e}", flush=True)
            time.sleep(60)
    else:
        scan_once()
