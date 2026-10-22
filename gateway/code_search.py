"""MCP 扩展：代码符号检索（IDE 编码感知）

让 Cursor/Claude Code 写代码时，能拉取关联的接口规范和 ADR。
"""

import os
import re
from pathlib import Path


async def _code_search(conn, args) -> str:
    """
    代码符号检索。
    args: { "query": "关键词", "repo_path": "/path/to/repo" (可选),
            "limit": 10 }
    返回匹配的函数/类/接口定义及其上下文。
    """
    query = args.get("query", "")
    repo = args.get("repo_path", "/volume1/docker/contexthub")
    limit = min(args.get("limit", 10), 30)

    if not query:
        return "需要 query 参数"

    results = []
    # 搜索 Python/JS/TS/Java 文件中的定义
    patterns = [
        (r"^(class|def|function|interface)\s+(\w+)", "py/js"),
        (r"^(public|private|protected)?\s*(class|interface)\s+(\w+)", "java"),
    ]

    for ext in ["*.py", "*.js", "*.ts", "*.java"]:
        for fpath in Path(repo).rglob(ext):
            # 跳过 node_modules/.git
            if any(p in str(fpath) for p in
                   ["node_modules", ".git", "__pycache__"]):
                continue
            try:
                text = fpath.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                continue
            for i, line in enumerate(text.split("\n"), 1):
                if query.lower() in line.lower():
                    # 找最近的定义行
                    for j in range(max(0, i-5), i):
                        l = text.split("\n")[j]
                        for pat, _ in patterns:
                            m = re.match(pat, l.strip())
                            if m:
                                results.append({
                                    "file": str(fpath.relative_to(repo)),
                                    "line": j+1,
                                    "definition": l.strip()[:120],
                                    "match_line": i,
                                    "match": line.strip()[:120],
                                })
                                break
                    if len(results) >= limit:
                        break
            if len(results) >= limit:
                break
        if len(results) >= limit:
            break

    if not results:
        return f"未找到与 '{query}' 相关的代码符号"

    out = [f"找到 {len(results)} 个相关代码位置：\n"]
    for r in results[:limit]:
        out.append(f"- `{r['file']}:{r['line']}` {r['definition']}")
        out.append(f"  匹配行 {r['match_line']}: {r['match']}")
    return "\n".join(out)


# 注册到 MCP 工具表（加到 mcp_server.py 的 TOOLS 字典）
CODE_TOOL_DEF = {
    "code_search": {
        "func": _code_search,
        "description": "代码符号检索：按关键词找函数/类/接口定义，"
                       "供 IDE 写代码时拉取关联规范",
        "schema": {
            "query": "搜索关键词（函数名/类名/接口名）",
            "repo_path": "代码仓库路径（可选，默认 ContextHub）",
            "limit": "返回条数（默认10，最多30）",
        },
    },
}
