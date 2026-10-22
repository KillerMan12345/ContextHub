"""
Living Ledger - 只追加的台账
记录知识库里发生的所有事：入库、版本变更、路由决策
人/Agent 读这一个文件就知道库里有什么
"""

from datetime import datetime
from pathlib import Path
from typing import Optional


class LivingLedger:
    def __init__(self, ledger_path: str):
        self.path = Path(ledger_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self.path.write_text(
                "# ContextHub Living Ledger\n\n"
                "知识库变更台账（只追加）。每条记录带时间戳和引用。\n\n---\n\n",
                encoding="utf-8",
            )

    def _ts(self) -> str:
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def log_ingest(
        self,
        title: str,
        doc_id: str,
        category: str,
        space_id: str,
        file_hash: str,
        routing_rule: str = "",
        version_label: str = "v1.0",
        relation: str = "NEW",
    ):
        entry = (
            f"## {self._ts()} 入库\n"
            f"- doc: 《{title}》{version_label}（{relation}）\n"
            f"- doc_id: `{doc_id}`\n"
            f"- 位置: `{space_id}` / `{category}`\n"
            f"- SHA-256: `{file_hash[:16]}...`\n"
        )
        if routing_rule:
            entry += f"- 路由规则: {routing_rule}\n"
        entry += "\n"
        self._append(entry)

    def log_supersede(
        self,
        old_title: str,
        old_doc_id: str,
        new_title: str,
        new_doc_id: str,
        reason: str = "",
    ):
        entry = (
            f"## {self._ts()} 版本替代\n"
            f"- 旧: 《{old_title}》(`{old_doc_id}`) → SUPERSEDED\n"
            f"- 新: 《{new_title}》(`{new_doc_id}`) → ACTIVE\n"
        )
        if reason:
            entry += f"- 原因: {reason}\n"
        entry += "\n"
        self._append(entry)

    def log_review(self, doc_id: str, reason: str):
        entry = (
            f"## {self._ts()} 待审核\n"
            f"- doc_id: `{doc_id}`\n"
            f"- 原因: {reason}\n"
            f"- 状态: NEEDS_REVIEW（已隔离到 _review/）\n\n"
        )
        self._append(entry)

    def _append(self, text: str):
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(text)

    def recent(self, n: int = 20) -> str:
        """返回最近 n 条记录（给 Agent 快速了解库状态）"""
        lines = self.path.read_text(encoding="utf-8").split("\n")
        # 简单实现：返回最后 n*8 行
        return "\n".join(lines[-(n * 8):])

    def log_commit(self, topic: str, content: str,
                   event_id: str, user_id: str):
        """通用提交记录（对话沉淀等）。"""
        entry = (
            f"## {self._ts()} 笔记\n"
            f"- 主题: {topic}\n"
            f"- event_id: `{event_id}`\n"
            f"- 用户: {user_id}\n"
            f"- 内容: {content}\n\n"
        )
        self._append(entry)

    def _append(self, entry: str):
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(entry)
