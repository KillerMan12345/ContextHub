"""
文档解析：markitdown + 本地 OCR 兜底
支持：PDF、Word、Excel、PPT、图片、HTML、CSV 等
"""

from pathlib import Path
from typing import Optional
import hashlib


class DocumentParser:
    def __init__(self):
        self._md = None

    def _get_markitdown(self):
        if self._md is None:
            from markitdown import MarkItDown
            self._md = MarkItDown()
        return self._md

    def parse(self, file_path: str) -> dict:
        """
        解析文档为 Markdown。
        返回: { markdown: str, title: str, file_hash: str, pages: int }
        """
        path = Path(file_path)
        if not path.exists():
            raise FileNotFoundError(f"文件不存在: {file_path}")

        # 计算哈希（去重用）
        file_hash = self._sha256(path)

        # markitdown 解析
        md = self._get_markitdown()
        result = md.convert(str(path))

        markdown = result.text_content or ""
        title = path.stem

        # 尝试从内容提取标题（第一个 # 标题）
        for line in markdown.split("\n"):
            line = line.strip()
            if line.startswith("# "):
                title = line[2:].strip()
                break

        return {
            "markdown": markdown,
            "title": title,
            "file_hash": file_hash,
            "file_name": path.name,
            "char_count": len(markdown),
        }

    def parse_bytes(self, data: bytes, file_name: str, work_dir: str = "/tmp") -> dict:
        """从字节流解析（API 上传场景）"""
        path = Path(work_dir) / file_name
        path.write_bytes(data)
        try:
            return self.parse(str(path))
        finally:
            path.unlink(missing_ok=True)

    def _sha256(self, path: Path) -> str:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(8192), b""):
                h.update(chunk)
        return h.hexdigest()

    def chunk_markdown(
        self,
        markdown: str,
        max_chars: int = 2000,
    ) -> list[dict]:
        """
        按标题切分 Markdown 为 chunks，每个带 section_path。
        这是"章节级引用"的基础。
        """
        chunks = []
        current_section = "全文"
        current_content: list[str] = []
        section_stack: list[str] = []

        for line in markdown.split("\n"):
            stripped = line.strip()
            if stripped.startswith("#"):
                # 保存上一个 section
                if current_content:
                    text = "\n".join(current_content).strip()
                    if text:
                        chunks.append({
                            "section_path": " / ".join(section_stack) if section_stack else "全文",
                            "content": text,
                        })
                    current_content = []

                # 解析标题层级
                level = len(stripped) - len(stripped.lstrip("#"))
                title = stripped.lstrip("#").strip()
                # 维护标题栈
                section_stack = section_stack[: level - 1] + [title]
                current_section = " / ".join(section_stack)
            else:
                current_content.append(line)

            # 超长截断
            if sum(len(c) for c in current_content) > max_chars:
                text = "\n".join(current_content).strip()
                if text:
                    chunks.append({
                        "section_path": current_section,
                        "content": text,
                    })
                current_content = []

        # 最后一个
        if current_content:
            text = "\n".join(current_content).strip()
            if text:
                chunks.append({
                    "section_path": current_section,
                    "content": text,
                })

        return chunks if chunks else [{"section_path": "全文", "content": markdown}]
