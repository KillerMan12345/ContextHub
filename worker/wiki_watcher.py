#!/usr/bin/env python3
"""
WF-04: Obsidian 双向同步 - 文件监听器
监听 wiki/ 目录的 .md 文件变更，同步回数据库。

规则（v2.3 §10.2）：
- 只监听 wiki/ 下的 .md 文件
- 忽略 _dashboard_控制台.md（系统生成）
- 系统字段（frontmatter 中的 doc_id/version）变化 → 拒绝并生成审核任务
- 正文变化 → 更新 chunks
- 用 content_revision 防循环
"""

import os
import sys
import time
import hashlib
import re
from pathlib import Path

WIKI_DIR = Path("/volume1/docker/contexthub/data/wiki")
GATEWAY_URL = os.environ.get("GATEWAY_URL", "http://gateway:8000")
TOKEN = os.environ.get("INBOX_TOKEN", "test-token-001")

# 防循环：记录我们自己写回的文件
_our_writes = set()


def file_hash(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        h.update(f.read())
    return h.hexdigest()[:16]


def parse_frontmatter(content: str) -> dict:
    """解析 YAML frontmatter。"""
    m = re.match(r"^---\n(.*?)\n---\n", content, re.DOTALL)
    if not m:
        return {}
    fm = {}
    for line in m.group(1).split("\n"):
        if ":" in line:
            k, v = line.split(":", 1)
            fm[k.strip()] = v.strip()
    return fm


def sync_file_to_db(path: Path):
    """单个文件同步到 DB。"""
    if path.name == "_dashboard_控制台.md":
        return  # 系统生成，跳过

    if str(path) in _our_writes:
        _our_writes.discard(str(path))
        return  # 自己写回的，跳过防循环

    try:
        content = path.read_text(encoding="utf-8")
    except Exception as e:
        print(f"读文件失败 {path}: {e}", flush=True)
        return

    fm = parse_frontmatter(content)
    doc_id = fm.get("doc_id")
    if not doc_id:
        print(f"跳过（无 doc_id）: {path.name}", flush=True)
        return

    # TODO: 调用网关 API 更新
    # 1. 检查系统字段是否被篡改
    # 2. 更新 document_chunks
    # 3. 更新 content_revision
    print(f"检测到变更: {path.name} (doc_id={doc_id[:8]}...)", flush=True)


def watch():
    """主循环：轮询检查文件 mtime（简单可靠，不依赖 inotify）。"""
    print("WF-04 文件监听启动", flush=True)
    print(f"监听目录: {WIKI_DIR}", flush=True)

    # 记录初始状态
    mtimes = {}
    for md in WIKI_DIR.rglob("*.md"):
        try:
            mtimes[str(md)] = md.stat().st_mtime
        except Exception:
            pass

    while True:
        time.sleep(10)  # 每 10 秒检查
        try:
            current = {}
            for md in WIKI_DIR.rglob("*.md"):
                try:
                    current[str(md)] = md.stat().st_mtime
                except Exception:
                    pass

            # 检查变更
            for path_str, mtime in current.items():
                if path_str not in mtimes or mtimes[path_str] != mtime:
                    sync_file_to_db(Path(path_str))

            # 检查删除
            for path_str in mtimes:
                if path_str not in current:
                    print(f"文件被删除: {Path(path_str).name}", flush=True)

            mtimes = current
        except Exception as e:
            print(f"监听错误: {e}", flush=True)


if __name__ == "__main__":
    watch()
