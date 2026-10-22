"""
ContextHub MCP Server
给 Claude Code / Cursor / 飞书 Bot 用的标准 MCP 接口。

6 个工具：
- search: 检索知识（带章节级引用）
- ingest: 入库文档
- commit: 提交版本（版本链）
- review: 标记待审核
- asset: 获取附件/原文件
- health: 健康指标查询（Phase 2 才有数据，接口先占位）
"""

import asyncio
import asyncpg
import hashlib
import os
import sys

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql://contexthub_app:change_me_in_env@localhost:5432/contexthub",
)

# 从 rls_context 复用逻辑（简化版：直接内联）
sys.path.insert(0, os.path.dirname(__file__))
from rls_context import profiled_connection

server = Server("contexthub")
_pool: asyncpg.Pool | None = None


async def get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5)
    return _pool


def _token() -> str:
    token = os.environ.get("CONTEXTHUB_TOKEN", "")
    if not token:
        raise ValueError("CONTEXTHUB_TOKEN 环境变量未设置")
    return token


@server.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="search",
            description="检索 ContextHub 知识库。返回带章节级引用的结果，每条注明来源文档、章节、页码。",
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "检索关键词"},
                    "limit": {"type": "integer", "default": 10},
                },
                "required": ["query"],
            },
        ),
        Tool(
            name="ingest",
            description="入库一篇文档。自动去重、规则路由、写台账。",
            inputSchema={
                "type": "object",
                "properties": {
                    "file_name": {"type": "string"},
                    "content": {"type": "string", "description": "文档文本内容"},
                    "user_instruction": {"type": "string"},
                },
                "required": ["file_name", "content"],
            },
        ),
        Tool(
            name="commit",
            description="提交文档新版本，形成版本链（旧版本标记 SUPERSEDED）。",
            inputSchema={
                "type": "object",
                "properties": {
                    "doc_id": {"type": "string", "description": "旧文档 ID"},
                    "file_name": {"type": "string"},
                    "content": {"type": "string"},
                    "change_note": {"type": "string", "description": "版本说明"},
                },
                "required": ["doc_id", "file_name", "content"],
            },
        ),
        Tool(
            name="review",
            description="标记文档待人工审核（隔离到 _review/）。",
            inputSchema={
                "type": "object",
                "properties": {
                    "doc_id": {"type": "string"},
                    "reason": {"type": "string"},
                },
                "required": ["doc_id", "reason"],
            },
        ),
        Tool(
            name="asset",
            description="获取文档的原文件路径和元信息。",
            inputSchema={
                "type": "object",
                "properties": {
                    "doc_id": {"type": "string"},
                },
                "required": ["doc_id"],
            },
        ),
        Tool(
            name="health",
            description="查询健康指标（Phase 2 功能，当前返回空）。",
            inputSchema={
                "type": "object",
                "properties": {
                    "person_name": {"type": "string"},
                    "metric_name": {"type": "string"},
                },
                "required": ["person_name"],
            },
        ),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    pool = await get_pool()
    token = _token()

    try:
        async with profiled_connection(pool, token) as (conn, user_id):
            if name == "search":
                return await _search(conn, arguments)
            elif name == "ingest":
                return await _ingest(conn, user_id, arguments)
            elif name == "commit":
                return await _commit(conn, user_id, arguments)
            elif name == "review":
                return await _review(conn, arguments)
            elif name == "asset":
                return await _asset(conn, arguments)
            elif name == "health":
                return await _health(conn, arguments)
            else:
                return [TextContent(type="text", text=f"未知工具: {name}")]
    except PermissionError as e:
        return [TextContent(type="text", text=f"权限拒绝: {e}")]
    except Exception as e:
        return [TextContent(type="text", text=f"错误: {e}")]


async def _search(conn, args) -> list[TextContent]:
    query = args["query"]
    limit = args.get("limit", 10)
    rows = await conn.fetch(
        """
        SELECT c.doc_title, c.section_path, c.page_number,
               left(c.content, 300) AS snippet,
               d.version_label
        FROM document_chunks c
        JOIN documents d ON c.doc_id = d.doc_id
        WHERE c.is_latest = TRUE
          AND c.content ILIKE '%' || $1 || '%'
        LIMIT $2
        """,
        query,
        limit,
    )
    if not rows:
        return [TextContent(type="text", text="未找到相关知识。")]
    lines = []
    for r in rows:
        citation = f"《{r['doc_title']}》{r['version_label']} - {r['section_path']}"
        if r["page_number"]:
            citation += f"，第{r['page_number']}页"
        lines.append(f"📄 {citation}\n   {r['snippet']}...")
    return [TextContent(type="text", text="\n\n".join(lines))]


async def _ingest(conn, user_id, args) -> list[TextContent]:
    import uuid

    file_name = args["file_name"]
    content = args["content"]
    file_hash = hashlib.sha256(content.encode()).hexdigest()

    existing = await conn.fetchrow(
        "SELECT doc_id, title FROM documents WHERE file_hash = $1 LIMIT 1",
        file_hash,
    )
    if existing:
        return [TextContent(
            type="text",
            text=f"文档已存在（去重）：《{existing['title']}》({existing['doc_id']})",
        )]

    # 简化路由
    text = (file_name + " " + args.get("user_instruction", "")).lower()
    if any(k in text for k in ["体检", "血压", "血糖", "医院", "健康"]):
        category = "04_Health_Archive"
    elif any(k in text for k in ["家庭", "孩子", "女儿", "父母", "生日"]):
        category = "03_Family"
    else:
        category = "01_Work"

    doc_id = uuid.uuid4()
    await conn.execute(
        """INSERT INTO documents
           (doc_id, space_id, category, title, file_hash, wiki_file_path, created_by)
           VALUES ($1, 'space_personal_u1', $2, $3, $4, $5, $6)""",
        doc_id, category, file_name, file_hash,
        f"wiki/{category}/{file_name}", user_id,
    )
    await conn.execute(
        """INSERT INTO document_chunks
           (doc_id, space_id, category, doc_title, section_path, content)
           VALUES ($1, 'space_personal_u1', $2, $3, '全文', $4)""",
        doc_id, category, file_name, content[:10000],
    )
    return [TextContent(
        type="text",
        text=f"✅ 已入库：《{file_name}》\n- doc_id: {doc_id}\n- 分类: {category}",
    )]


async def _commit(conn, user_id, args) -> list[TextContent]:
    import uuid

    old_doc_id = args["doc_id"]
    old = await conn.fetchrow(
        "SELECT * FROM documents WHERE doc_id = $1::uuid", old_doc_id
    )
    if not old:
        return [TextContent(type="text", text=f"文档不存在: {old_doc_id}")]

    new_doc_id = uuid.uuid4()
    file_hash = hashlib.sha256(args["content"].encode()).hexdigest()

    # 旧版本标记 SUPERSEDED
    await conn.execute(
        """UPDATE documents SET is_latest = FALSE, doc_status = 'SUPERSEDED',
           superseded_by = $1 WHERE doc_id = $2::uuid""",
        new_doc_id, old_doc_id,
    )
    # 新版本
    await conn.execute(
        """INSERT INTO documents
           (doc_id, space_id, category, title, file_hash, wiki_file_path,
            version_key, version_label, relation_type, supersedes_doc_id,
            parent_doc_id, created_by)
           VALUES ($1, $2, $3, $4, $5, $6, $7, 'v2.0', 'REPLACEMENT', $8::uuid, $8::uuid, $9)""",
        new_doc_id, old["space_id"], old["category"], args["file_name"],
        file_hash, old["wiki_file_path"], old["version_key"],
        old_doc_id, user_id,
    )
    return [TextContent(
        type="text",
        text=f"✅ 版本已提交：{old['title']} → v2.0\n"
             f"- 旧: {old_doc_id} (SUPERSEDED)\n"
             f"- 新: {new_doc_id} (ACTIVE)\n"
             f"- 说明: {args.get('change_note', '无')}",
    )]


async def _review(conn, args) -> list[TextContent]:
    await conn.execute(
        "UPDATE documents SET doc_status = 'NEEDS_REVIEW' WHERE doc_id = $1::uuid",
        args["doc_id"],
    )
    return [TextContent(
        type="text",
        text=f"⚠️ 已标记待审核: {args['doc_id']}\n原因: {args['reason']}",
    )]


async def _asset(conn, args) -> list[TextContent]:
    row = await conn.fetchrow(
        "SELECT title, wiki_file_path, file_hash, version_label, created_at"
        " FROM documents WHERE doc_id = $1::uuid",
        args["doc_id"],
    )
    if not row:
        return [TextContent(type="text", text="文档不存在")]
    return [TextContent(
        type="text",
        text=f"📎 《{row['title']}》{row['version_label']}\n"
             f"- 路径: {row['wiki_file_path']}\n"
             f"- SHA: {row['file_hash'][:16]}...\n"
             f"- 创建: {row['created_at']}",
    )]


async def _health(conn, args) -> list[TextContent]:
    return [TextContent(
        type="text",
        text="健康指标功能在 Phase 2 上线，当前无数据。",
    )]


async def main():
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream,
                         server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
