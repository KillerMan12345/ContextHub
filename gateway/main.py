"""ContextHub Gateway - Phase 0 skeleton."""

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel
import asyncpg
import os

from rls_context import profiled_connection, DATABASE_URL

app = FastAPI(title="ContextHub Gateway", version="0.1.0-phase0")
pool: asyncpg.Pool | None = None


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
    return {"status": "ok", "phase": "0"}


@app.post("/search")
async def search_knowledge(
    req: SearchRequest, authorization: str = Header("")
):
    """Phase 0: 带 RLS 过滤的全文检索。向量检索 Phase 1+。"""
    token = authorization.removeprefix("Bearer ").strip()
    if not token:
        raise HTTPException(401, "Missing token")
    assert pool is not None
    try:
        async with profiled_connection(pool, token) as (conn, user_id):
            # 关键词检索（FTS trigram）；space/category 过滤由 RLS 自动执行
            rows = await conn.fetch(
                """
                SELECT c.doc_id, c.doc_title, c.section_path, c.page_number,
                       left(c.content, 300) AS snippet
                FROM document_chunks c
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
                        "section": r["section_path"],
                        "page": r["page_number"],
                        "snippet": r["snippet"],
                        "citation": f"《{r['doc_title']}》- {r['section_path']}"
                        + (f"，第{r['page_number']}页" if r["page_number"] else ""),
                    }
                    for r in rows
                ]
            }
    except PermissionError as e:
        raise HTTPException(403, str(e))


class IngestRequest(BaseModel):
    file_name: str
    target_space_id: str = "AUTO"
    user_instruction: str | None = None


@app.post("/ingest")
async def ingest(req: IngestRequest, authorization: str = Header("")):
    """Phase 0: 只登记任务，实际流水线 Phase 1。"""
    token = authorization.removeprefix("Bearer ").strip()
    if not token:
        raise HTTPException(401, "Missing token")
    assert pool is not None
    try:
        async with profiled_connection(pool, token) as (conn, user_id):
            # 幂等键：user + 文件名（Phase 0 简化；Phase 1 用 SHA-256）
            import uuid, hashlib

            task_id = f"ing_{uuid.uuid4().hex[:12]}"
            idem = hashlib.sha256(
                f"{user_id}:{req.file_name}:{req.target_space_id}".encode()
            ).hexdigest()
            try:
                await conn.execute(
                    """
                    INSERT INTO ingestion_tasks
                        (task_id, idempotency_key, user_id, target_space_id, task_status)
                    VALUES ($1, $2, $3, $4, 'RECEIVED')
                    """,
                    task_id,
                    idem,
                    user_id,
                    None if req.target_space_id == "AUTO" else req.target_space_id,
                )
            except asyncpg.UniqueViolationError:
                row = await conn.fetchrow(
                    "SELECT task_id, task_status FROM ingestion_tasks "
                    "WHERE idempotency_key = $1",
                    idem,
                )
                return {
                    "task_id": row["task_id"],
                    "status": row["task_status"],
                    "deduped": True,
                }
            return {"task_id": task_id, "status": "RECEIVED", "deduped": False}
    except PermissionError as e:
        raise HTTPException(403, str(e))
