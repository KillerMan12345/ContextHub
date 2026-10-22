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


"""网关 /ask 端点（修订版）：隐私查询不调模型，直接返回检索原文"""

from pydantic import BaseModel
from fastapi import Header, HTTPException


class AskRequest(BaseModel):
    question: str
    limit: int = 5


@app.post("/ask")
async def ask_question(req: AskRequest, authorization: str = Header("")):
    token = authorization.removeprefix("Bearer ").strip()
    if not token:
        raise HTTPException(401, "Missing token")
    assert pool is not None

    try:
        async with profiled_connection(pool, token) as (conn, user_id):
            rows = await conn.fetch(
                """
                SELECT c.doc_title, c.section_path, c.page_number,
                       left(c.content, 500) AS snippet,
                       d.version_label, d.category
                FROM document_chunks c
                JOIN documents d ON c.doc_id = d.doc_id
                WHERE c.is_latest = TRUE
                  AND c.content ILIKE '%' || $1 || '%'
                LIMIT $2
                """,
                req.question,
                req.limit,
            )

            if not rows:
                return {"answer": "知识库中没有找到相关内容。",
                        "citations": []}

            citations = []
            context_parts = []
            for i, r in enumerate(rows, 1):
                cit = (f"《{r['doc_title']}》{r['version_label']}"
                       f" - {r['section_path']}")
                if r["page_number"]:
                    cit += f"，第{r['page_number']}页"
                citations.append(cit)
                context_parts.append(f"[{i}] {cit}\n{r['snippet']}")

            context = "\n\n".join(context_parts)

            # 隐私判断：健康类别不调模型
            privacy = any(r["category"] == "04_Health_Archive" for r in rows)

            if privacy:
                # 隐私红线：直接返回检索原文，不调任何模型
                return {
                    "answer": f"以下为知识库原文（隐私内容，未经模型处理）：\n\n{context}",
                    "citations": citations,
                    "provider": "rule (privacy-safe)",
                    "privacy_routed_local": True,
                }

            # 通用：LLM 组织答案
            from model_adapter import get_adapter
            adapter = get_adapter()
            prompt = (
                f"基于以下知识库内容回答问题。回答后必须列出引用来源。\n\n"
                f"问题：{req.question}\n\n"
                f"知识库内容：\n{context}\n\n"
                "要求：\n"
                "1. 只基于上述内容回答，不要编造\n"
                "2. 每条关键信息标注来源编号如 [1]\n"
                "3. 最后列出引用列表"
            )
            try:
                resp = await adapter.chat(prompt, caller="ask")
            except RuntimeError as e:
                return {
                    "answer": f"模型未配置，返回原始检索结果：\n\n{context}",
                    "citations": citations,
                    "provider": "none (fallback)",
                    "warning": str(e),
                }

            return {
                "answer": resp.text,
                "citations": citations,
                "provider": resp.provider,
                "privacy_routed_local": False,
            }
    except PermissionError as e:
        raise HTTPException(403, str(e))
"""健康档案入库端点：文档 → 规则提取指标 → health_metrics

隐私：全程本地规则，不调任何模型。
"""

from pydantic import BaseModel
from fastapi import Header, HTTPException
from datetime import date
import hashlib
import uuid


class HealthIngestRequest(BaseModel):
    person_name: str  # 如 "父亲"、"母亲"
    file_name: str
    content: str
    measured_date: str = ""  # YYYY-MM-DD，空则用今天


@app.post("/health/ingest")
async def health_ingest(req: HealthIngestRequest,
                        authorization: str = Header("")):
    """
    健康档案入库：
    1. 文档存入 04_Health_Archive（走 RLS）
    2. 规则提取健康指标 → health_metrics
    3. 全程不调模型
    """
    token = authorization.removeprefix("Bearer ").strip()
    if not token:
        raise HTTPException(401, "Missing token")
    assert pool is not None

    try:
        async with profiled_connection(pool, token) as (conn, user_id):
            from health_extractor import HealthExtractor

            # 1. 存文档（04_Health_Archive）
            file_hash = hashlib.sha256(req.content.encode()).hexdigest()
            existing = await conn.fetchrow(
                "SELECT doc_id FROM documents WHERE file_hash = $1 LIMIT 1",
                file_hash,
            )
            if existing:
                doc_id = existing["doc_id"]
            else:
                doc_id = uuid.uuid4()
                await conn.execute(
                    """INSERT INTO documents
                       (doc_id, space_id, category, title, file_hash,
                        wiki_file_path, created_by)
                       VALUES ($1, 'space_family_01', '04_Health_Archive',
                               $2, $3, $4, $5)""",
                    doc_id, req.file_name, file_hash,
                    f"wiki/04_Health_Archive/{req.file_name}", user_id,
                )
                await conn.execute(
                    """INSERT INTO document_chunks
                       (doc_id, space_id, category, doc_title,
                        section_path, content)
                       VALUES ($1, 'space_family_01', '04_Health_Archive',
                               $2, '全文', $3)""",
                    doc_id, req.file_name, req.content[:10000],
                )

            # 2. 规则提取指标（不调模型）
            extractor = HealthExtractor()
            m_date = (date.fromisoformat(req.measured_date)
                      if req.measured_date else date.today())
            metrics = extractor.extract(req.content, req.person_name, m_date)

            # 3. 入库 health_metrics
            inserted = 0
            abnormal = []
            for m in metrics:
                await conn.execute(
                    """INSERT INTO health_metrics
                       (space_id, source_doc_id, person_name, metric_name,
                        metric_value, unit, reference_range, is_abnormal,
                        measured_date)
                       VALUES ('space_family_01', $1, $2, $3, $4, $5, $6, $7, $8)""",
                    doc_id, m.person_name, m.metric_name, m.metric_value,
                    m.unit, m.reference_range, m.is_abnormal, m.measured_date,
                )
                inserted += 1
                if m.is_abnormal:
                    abnormal.append(f"{m.metric_name}: {m.metric_value}{m.unit}")

            # 4. 写台账
            ledger.log_ingest(
                title=f"[健康]{req.file_name}",
                doc_id=str(doc_id),
                category="04_Health_Archive",
                space_id="space_family_01",
                file_hash=file_hash,
                routing_rule="R-01-health",
            )

            return {
                "doc_id": str(doc_id),
                "metrics_extracted": inserted,
                "abnormal": abnormal,
                "privacy_note": "全程规则提取，未调用任何模型",
            }
    except PermissionError as e:
        raise HTTPException(403, str(e))


@app.get("/health/metrics")
async def health_metrics(person_name: str = "",
                         authorization: str = Header("")):
    """查询健康指标（走 RLS，只能看有权限的）"""
    token = authorization.removeprefix("Bearer ").strip()
    if not token:
        raise HTTPException(401, "Missing token")
    assert pool is not None

    try:
        async with profiled_connection(pool, token) as (conn, user_id):
            if person_name:
                rows = await conn.fetch(
                    """SELECT person_name, metric_name, metric_value, unit,
                              reference_range, is_abnormal, measured_date
                       FROM health_metrics
                       WHERE person_name = $1
                       ORDER BY measured_date DESC, metric_name""",
                    person_name,
                )
            else:
                rows = await conn.fetch(
                    """SELECT person_name, metric_name, metric_value, unit,
                              reference_range, is_abnormal, measured_date
                       FROM health_metrics
                       ORDER BY measured_date DESC, person_name, metric_name
                       LIMIT 100""",
                )
            return {
                "metrics": [
                    {
                        "person": r["person_name"],
                        "metric": r["metric_name"],
                        "value": float(r["metric_value"]),
                        "unit": r["unit"],
                        "ref": r["reference_range"],
                        "abnormal": r["is_abnormal"],
                        "date": str(r["measured_date"]),
                    }
                    for r in rows
                ]
            }
    except PermissionError as e:
        raise HTTPException(403, str(e))
"""Web UI 静态服务：加到 main.py"""

from fastapi.responses import FileResponse
import os

WEB_DIR = os.environ.get("WEB_DIR", "/app/web")


@app.get("/")
async def web_ui():
    index = os.path.join(WEB_DIR, "index.html")
    if os.path.exists(index):
        return FileResponse(index)
    return {"message": "Web UI 未部署", "api": "ok"}
"""文件上传入库端点：接收文件 → markitdown 解析 → 切分 → 入库 → 落盘

支持：PDF、Word、Excel、PPT、图片、HTML、CSV、TXT、MD
"""

from fastapi import Header, HTTPException, UploadFile, File, Form
from typing import Optional
import hashlib
import uuid
import os


@app.post("/ingest/file")
async def ingest_file(
    file: UploadFile = File(...),
    target_space_id: str = Form("AUTO"),
    user_instruction: Optional[str] = Form(None),
    authorization: str = Header(""),
):
    """
    文件上传入库：
    1. 收文件 → 存临时
    2. markitdown 解析 → Markdown
    3. 按标题切分 chunks（章节级引用）
    4. 去重 → 路由 → 写库
    5. 落盘到 /data/wiki/（Obsidian 可读）
    """
    token = authorization.removeprefix("Bearer ").strip()
    if not token:
        raise HTTPException(401, "Missing token")
    assert pool is not None

    # 存临时文件
    tmp_path = f"/tmp/upload_{uuid.uuid4().hex}_{file.filename}"
    content = await file.read()
    with open(tmp_path, "wb") as f:
        f.write(content)

    try:
        async with profiled_connection(pool, token) as (conn, user_id):
            from doc_parser import DocumentParser

            # 1. 解析
            parser = DocumentParser()
            try:
                parsed = parser.parse(tmp_path)
            except Exception as e:
                raise HTTPException(400, f"文档解析失败: {e}")

            markdown = parsed["markdown"]
            title = parsed["title"]
            file_hash = parsed["file_hash"]

            if not markdown.strip():
                raise HTTPException(400, "文档内容为空，无法入库")

            # 2. 去重
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

            # 3. 路由
            category, rule_id = route_category(title, user_instruction)
            space_id = (target_space_id
                        if target_space_id != "AUTO"
                        else "space_personal_u1")

            # 4. 写库
            doc_id = uuid.uuid4()
            wiki_path = f"wiki/{category}/{title}.md"
            await conn.execute(
                """INSERT INTO documents
                   (doc_id, space_id, category, title, file_hash,
                    wiki_file_path, created_by)
                   VALUES ($1, $2, $3, $4, $5, $6, $7)""",
                doc_id, space_id, category, title,
                file_hash, wiki_path, user_id,
            )

            # 5. 切分入库
            chunks = parser.chunk_markdown(markdown)
            for ch in chunks:
                await conn.execute(
                    """INSERT INTO document_chunks
                       (doc_id, space_id, category, doc_title,
                        section_path, content)
                       VALUES ($1, $2, $3, $4, $5, $6)""",
                    doc_id, space_id, category, title,
                    ch["section_path"], ch["content"][:10000],
                )

            # 6. 落盘（Obsidian 可读）
            disk_path = f"/data/wiki/{space_id}/{category}/{title}.md"
            os.makedirs(os.path.dirname(disk_path), exist_ok=True)
            with open(disk_path, "w", encoding="utf-8") as f:
                f.write(f"---\n")
                f.write(f"title: {title}\n")
                f.write(f"doc_id: {doc_id}\n")
                f.write(f"category: {category}\n")
                f.write(f"space: {space_id}\n")
                f.write(f"---\n\n")
                f.write(markdown)

            # 7. 台账
            ledger.log_ingest(
                title=title,
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
                "chunks": len(chunks),
                "chars": len(markdown),
                "wiki_path": disk_path,
            }
    except PermissionError as e:
        raise HTTPException(403, str(e))
    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
