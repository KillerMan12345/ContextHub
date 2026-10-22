"""
ContextHub MCP Server (mcp<2, v1 API)
6 个工具：search / ingest / commit / review / asset / health
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

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from rls_context import profiled_connection

server = Server("contexthub")
_pool = None


async def get_pool():
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5)
    return _pool


def _token():
    token = os.environ.get("CONTEXTHUB_TOKEN", "")
    if not token:
        raise ValueError("CONTEXTHUB_TOKEN 未设置")
    return token


@server.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(name="search", description="检索 ContextHub 知识库，返回章节级引用。",
             inputSchema={"type": "object",
                          "properties": {"query": {"type": "string"},
                                         "limit": {"type": "integer", "default": 10}},
                          "required": ["query"]}),
        Tool(name="ingest", description="入库文档（去重→路由→写库）。",
             inputSchema={"type": "object",
                          "properties": {"file_name": {"type": "string"},
                                         "content": {"type": "string"},
                                         "user_instruction": {"type": "string"}},
                          "required": ["file_name", "content"]}),
        Tool(name="commit", description="提交新版本，形成版本链。",
             inputSchema={"type": "object",
                          "properties": {"doc_id": {"type": "string"},
                                         "file_name": {"type": "string"},
                                         "content": {"type": "string"},
                                         "change_note": {"type": "string"}},
                          "required": ["doc_id", "file_name", "content"]}),
        Tool(name="review", description="标记文档待审核。",
             inputSchema={"type": "object",
                          "properties": {"doc_id": {"type": "string"},
                                         "reason": {"type": "string"}},
                          "required": ["doc_id", "reason"]}),
        Tool(name="asset", description="获取文档原文件信息。",
             inputSchema={"type": "object",
                          "properties": {"doc_id": {"type": "string"}},
                          "required": ["doc_id"]}),
        Tool(name="health", description="健康指标查询（Phase 2）。",
             inputSchema={"type": "object",
                          "properties": {"person_name": {"type": "string"},
                                         "metric_name": {"type": "string"}},
                          "required": ["person_name"]}),
        Tool(name="code_search", description="代码符号检索（IDE 编码感知）。",
             inputSchema={"type": "object",
                          "properties": {"query": {"type": "string"},
                                         "repo_path": {"type": "string"},
                                         "limit": {"type": "integer", "default": 10}},
                          "required": ["query"]}),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    pool = await get_pool()
    try:
        async with profiled_connection(pool, _token()) as (conn, user_id):
            from code_search import _code_search as _cs
            fn = {"search": _search, "ingest": _ingest, "commit": _commit,
                  "review": _review, "asset": _asset, "health": _health,
                  "code_search": _cs}.get(name)
            if not fn:
                text = f"未知工具: {name}"
            elif name in ("ingest", "commit"):
                text = await fn(conn, user_id, arguments)
            else:
                text = await fn(conn, arguments)
    except PermissionError as e:
        text = f"权限拒绝: {e}"
    except Exception as e:
        text = f"错误: {e}"
    return [TextContent(type="text", text=text)]


async def _search(conn, args) -> str:
    rows = await conn.fetch(
        """SELECT c.doc_title, c.section_path, c.page_number,
                  left(c.content, 300) AS snippet, d.version_label
           FROM document_chunks c JOIN documents d ON c.doc_id = d.doc_id
           WHERE c.is_latest = TRUE AND c.content ILIKE '%' || $1 || '%'
           LIMIT $2""",
        args["query"], args.get("limit", 10))
    if not rows:
        return "未找到相关知识。"
    out = []
    for r in rows:
        cit = f"《{r['doc_title']}》{r['version_label']} - {r['section_path']}"
        if r["page_number"]:
            cit += f"，第{r['page_number']}页"
        out.append(f"📄 {cit}\n   {r['snippet']}...")
    return "\n\n".join(out)


async def _ingest(conn, user_id, args) -> str:
    import uuid
    fh = hashlib.sha256(args["content"].encode()).hexdigest()
    ex = await conn.fetchrow(
        "SELECT doc_id, title FROM documents WHERE file_hash = $1 LIMIT 1", fh)
    if ex:
        return f"文档已存在（去重）：《{ex['title']}》"
    t = (args["file_name"] + " " + args.get("user_instruction", "")).lower()
    cat = ("04_Health_Archive" if any(k in t for k in ["体检", "血压", "血糖", "医院", "健康"])
           else "03_Family" if any(k in t for k in ["家庭", "孩子", "女儿", "父母", "生日"])
           else "01_Work")
    did = uuid.uuid4()
    await conn.execute(
        "INSERT INTO documents (doc_id, space_id, category, title, file_hash,"
        " wiki_file_path, created_by) VALUES ($1,'space_personal_u1',$2,$3,$4,$5,$6)",
        did, cat, args["file_name"], fh,
        f"wiki/{cat}/{args['file_name']}", user_id)
    await conn.execute(
        "INSERT INTO document_chunks (doc_id, space_id, category, doc_title,"
        " section_path, content) VALUES ($1,'space_personal_u1',$2,$3,'全文',$4)",
        did, cat, args["file_name"], args["content"][:10000])
    return f"✅ 已入库：《{args['file_name']}》\n- doc_id: {did}\n- 分类: {cat}"


async def _commit(conn, user_id, args) -> str:
    import uuid
    old = await conn.fetchrow(
        "SELECT * FROM documents WHERE doc_id = $1::uuid", args["doc_id"])
    if not old:
        return f"文档不存在: {args['doc_id']}"
    nid = uuid.uuid4()
    fh = hashlib.sha256(args["content"].encode()).hexdigest()
    await conn.execute(
        "UPDATE documents SET is_latest=FALSE, doc_status='SUPERSEDED',"
        " superseded_by=$1 WHERE doc_id=$2::uuid", nid, args["doc_id"])
    await conn.execute(
        "INSERT INTO documents (doc_id, space_id, category, title, file_hash,"
        " wiki_file_path, version_key, version_label, relation_type,"
        " supersedes_doc_id, parent_doc_id, created_by)"
        " VALUES ($1,$2,$3,$4,$5,$6,$7,'v2.0','REPLACEMENT',$8::uuid,$8::uuid,$9)",
        nid, old["space_id"], old["category"], args["file_name"], fh,
        old["wiki_file_path"], old["version_key"], args["doc_id"], user_id)
    return (f"✅ 版本已提交\n- 旧: {args['doc_id']} (SUPERSEDED)\n"
            f"- 新: {nid} (ACTIVE)")


async def _review(conn, args) -> str:
    await conn.execute(
        "UPDATE documents SET doc_status='NEEDS_REVIEW' WHERE doc_id=$1::uuid",
        args["doc_id"])
    return f"⚠️ 已标记待审核: {args['doc_id']}\n原因: {args['reason']}"


async def _asset(conn, args) -> str:
    r = await conn.fetchrow(
        "SELECT title, wiki_file_path, file_hash, version_label FROM documents"
        " WHERE doc_id=$1::uuid", args["doc_id"])
    if not r:
        return "文档不存在"
    return (f"📎 《{r['title']}》{r['version_label']}\n"
            f"- 路径: {r['wiki_file_path']}\n- SHA: {r['file_hash'][:16]}...")


async def _health(conn, args) -> str:
    return "健康指标功能在 Phase 2 上线，当前无数据。"


async def main():
    async with stdio_server() as (rs, ws):
        await server.run(rs, ws, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
