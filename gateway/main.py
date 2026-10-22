"""ContextHub Gateway - Phase 1."""

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel
import asyncpg
import hashlib
import os
import uuid

from rls_context import profiled_connection, DATABASE_URL
from living_ledger import LivingLedger

app = FastAPI(title="ContextHub Gateway", version="0.2.0-phase1")
pool: asyncpg.Pool | None = None

LEDGER_PATH = os.environ.get("LEDGER_PATH", "/data/living-ledger.md")
ledger = LivingLedger(LEDGER_PATH)


@app.on_event("startup")
async def startup():
    global pool
    pool = await asyncpg.create_pool(DATABASE_URL, min_size=2, max_size=10)


@app.on_event("shutdown")
async def shutdown():
    if pool:
        await pool.close()


class SearchRequest(BaseModel):
    query: str
    space_filter: str | None = None
    category_filter: str | None = None
    limit: int = 10


@app.get("/health")
async def health():
    return {"status": "ok", "phase": "1"}


@app.post("/search")
async def search_knowledge(req: SearchRequest, authorization: str = Header("")):
    """带 RLS 过滤的全文检索，返回章节级引用。"""
    token = authorization.removeprefix("Bearer ").strip()
    if not token:
        raise HTTPException(401, "Missing token")
    assert pool is not None
    try:
        async with profiled_connection(pool, token) as (conn, user_id):
            rows = await conn.fetch(
                """
                SELECT c.doc_id, c.doc_title, c.section_path, c.page_number,
                       left(c.content, 300) AS snippet,
                       d.version_label, d.category
                FROM document_chunks c
                JOIN documents d ON c.doc_id = d.doc_id
                WHERE c.is_latest = TRUE
                  AND c.content ILIKE '%' || $1 || '%'
                LIMIT $2
                """,
                req.query,
                req.limit,
            )
            return {
                "results": [
                    {
                        "doc_id": str(r["doc_id"]),
                        "title": r["doc_title"],
                        "version": r["version_label"],
                        "category": r["category"],
                        "section": r["section_path"],
                        "page": r["page_number"],
                        "snippet": r["snippet"],
                        "citation": (
                            f"《{r['doc_title']}》{r['version_label']} - "
                            f"{r['section_path']}"
                            + (f"，第{r['page_number']}页" if r["page_number"] else "")
                        ),
                    }
                    for r in rows
                ]
            }
    except PermissionError as e:
        raise HTTPException(403, str(e))


def route_category(file_name: str, instruction: str | None) -> tuple[str, str]:
    """规则优先路由。返回 (category, rule_id)。"""
    text = (file_name + " " + (instruction or "")).lower()
    if any(k in text for k in ["体检", "血压", "血糖", "医院", "医生", "处方", "化验", "健康"]):
        return "04_Health_Archive", "R-01"
    if any(k in text for k in ["家庭", "孩子", "女儿", "父母", "生日", "派对"]):
        return "03_Family", "R-02"
    if any(k in text for k in ["生活", "购物", "旅游", "美食", "电影"]):
        return "02_Life", "R-03"
    return "01_Work", "R-00-default"


class IngestRequest(BaseModel):
    file_name: str
    content: str = ""  # Phase 1: 直接传文本内容
    target_space_id: str = "AUTO"
    user_instruction: str | None = None


@app.post("/ingest")
async def ingest(req: IngestRequest, authorization: str = Header("")):
    """真实入库：去重→路由→写库→写台账。"""
    token = authorization.removeprefix("Bearer ").strip()
    if not token:
        raise HTTPException(401, "Missing token")
    assert pool is not None
    try:
        async with profiled_connection(pool, token) as (conn, user_id):
            # 1. 计算哈希，去重
            file_hash = hashlib.sha256(req.content.encode()).hexdigest()
            existing = await conn.fetchrow(
                "SELECT doc_id, title FROM documents WHERE file_hash = $1 LIMIT 1",
                file_hash,
            )
            if existing:
                return {
                    "doc_id": str(existing["doc_id"]),
                    "status": "DUPLICATE",
                    "message": f"已存在：《{existing['title']}》",
                }

            # 2. 路由
            category, rule_id = route_category(req.file_name, req.user_instruction)
            space_id = (
                req.target_space_id
                if req.target_space_id != "AUTO"
                else "space_personal_u1"  # Phase 1 简化
            )

            # 3. 写库
            doc_id = uuid.uuid4()
            wiki_path = f"wiki/{category}/{req.file_name}"
            await conn.execute(
                """
                INSERT INTO documents
                    (doc_id, space_id, category, title, file_hash,
                     wiki_file_path, created_by)
                VALUES ($1, $2, $3, $4, $5, $6, $7)
                """,
                doc_id,
                space_id,
                category,
                req.file_name,
                file_hash,
                wiki_path,
                user_id,
            )

            # 4. 切片（Phase 1 简化：整篇一个 chunk）
            await conn.execute(
                """
                INSERT INTO document_chunks
                    (doc_id, space_id, category, doc_title,
                     section_path, content)
                VALUES ($1, $2, $3, $4, '全文', $5)
                """,
                doc_id,
                space_id,
                category,
                req.file_name,
                req.content[:10000],  # 限制长度
            )

            # 5. 写台账
            ledger.log_ingest(
                title=req.file_name,
                doc_id=str(doc_id),
                category=category,
                space_id=space_id,
                file_hash=file_hash,
                routing_rule=rule_id,
            )

            return {
                "doc_id": str(doc_id),
                "status": "COMMITTED",
                "category": category,
                "rule": rule_id,
            }
    except PermissionError as e:
        raise HTTPException(403, str(e))


@app.get("/ledger")
async def get_ledger(authorization: str = Header(""), n: int = 20):
    """查看台账最近记录。"""
    token = authorization.removeprefix("Bearer ").strip()
    if not token:
        raise HTTPException(401, "Missing token")
    return {"recent": ledger.recent(n)}
