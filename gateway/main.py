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


def route_category(file_name: str, instruction: str | None) -> tuple[str, str, float]:
    """
    规则优先路由。返回 (category, rule_id, confidence)。
    confidence < 0.5 → 进 _review 隔离，待人工确认。
    """
    text = (file_name + " " + (instruction or "")).lower()

    # 用户明确指定 → 高置信度
    if instruction and any(k in instruction for k in
                           ["工作", "生活", "家庭", "健康"]):
        if "健康" in instruction:
            return "04_Health_Archive", "R-01-explicit", 1.0
        if "家庭" in instruction:
            return "03_Family", "R-02-explicit", 1.0
        if "生活" in instruction:
            return "02_Life", "R-03-explicit", 1.0
        return "01_Work", "R-00-explicit", 1.0

    if any(k in text for k in ["体检", "血压", "血糖", "医院", "医生", "处方", "化验", "健康"]):
        return "04_Health_Archive", "R-01", 0.9
    if any(k in text for k in ["家庭", "孩子", "女儿", "父母", "生日", "派对"]):
        return "03_Family", "R-02", 0.9
    if any(k in text for k in ["生活", "购物", "旅游", "美食", "电影"]):
        return "02_Life", "R-03", 0.9
    # 工作类关键词 → 中置信度
    if any(k in text for k in ["工作", "项目", "会议", "报告", "文档", "需求",
                                "设计", "开发", "测试", "接口", "api", "jwt",
                                "配置", "教程", "手册", "指南", "规范"]):
        return "01_Work", "R-00-work", 0.7
    # 完全无法判断 → 低置信度，进隔离
    return "01_Work", "R-00-uncertain", 0.3


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
                await _task_set(conn, task_id, "COMMITTED",
                                existing["doc_id"], "去重命中")
                return {
                    "doc_id": str(existing["doc_id"]),
                    "status": "DUPLICATE",
                    "message": f"已存在：《{existing['title']}》",
                    "task_id": task_id,
                }

            # 2. 路由
            category, rule_id, confidence = route_category(
                req.file_name, req.user_instruction)
            space_id = (
                req.target_space_id
                if req.target_space_id != "AUTO"
                else "space_personal_u1"  # Phase 1 简化
            )

            # 3. 写库（含版本链）
            doc_id = uuid.uuid4()
            wiki_path = f"wiki/{category}/{req.file_name}"
            version_key, version_label = extract_version_key(req.file_name)
            await conn.execute(
                """
                INSERT INTO documents
                    (doc_id, space_id, category, title, file_hash,
                     wiki_file_path, version_key, version_label,
                     is_latest, doc_status, created_by)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, TRUE,
                        'ACTIVE', $9)
                """,
                doc_id,
                space_id,
                category,
                req.file_name,
                file_hash,
                wiki_path,
                version_key,
                version_label,
                user_id,
            )
            # 版本链：替代旧版本
            superseded = await handle_version_chain(
                conn, space_id, version_key, doc_id, req.file_name)

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

            # 0. 任务登记
            idem_key = f"file:{file.filename}:{len(content)}"
            task_id = await _task_create(
                conn, user_id, file.filename or "unknown",
                target_space_id, idem_key)
            await _task_set(conn, task_id, "PRECHECKING")

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
                await _task_set(conn, task_id, "COMMITTED",
                                existing["doc_id"], "去重命中")
                return {
                    "doc_id": str(existing["doc_id"]),
                    "status": "DUPLICATE",
                    "message": f"已存在：《{existing['title']}》",
                    "task_id": task_id,
                }

            # 3. 路由
            category, rule_id, confidence = route_category(
                title, user_instruction)
            await _task_set(conn, task_id, "ROUTING")
            # 低置信度 → 进 _review 隔离
            needs_review = confidence < 0.5
            space_id = (target_space_id
                        if target_space_id != "AUTO"
                        else "space_personal_u1")

            await _task_set(
                conn, task_id,
                "NEEDS_REVIEW" if needs_review else "STAGED")
            # 4. 写库（含版本链 + raw）
            await _task_set(conn, task_id, "COMMITTING")
            doc_id = uuid.uuid4()
            wiki_path = f"wiki/{category}/{title}.md"
            # 保存原始文件
            raw_rel = save_raw_file(tmp_path, file_hash, file.filename or title)
            # 版本链
            version_key, version_label = extract_version_key(title)
            await conn.execute(
                """INSERT INTO documents
                   (doc_id, space_id, category, title, file_hash,
                    wiki_file_path, raw_file_path,
                    version_key, version_label, is_latest,
                    doc_status, created_by)
                   VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, TRUE,
                           $10, $11)""",
                doc_id, space_id, category, title,
                file_hash, wiki_path, raw_rel,
                version_key, version_label,
                'NEEDS_REVIEW' if needs_review else 'ACTIVE',
                user_id,
            )
            # 低置信度 → 写 review_reason + 隔离文件
            if needs_review:
                reason = (f"路由置信度 {confidence:.2f} < 0.5 "
                          f"(rule={rule_id})，需人工确认分类")
                await conn.execute(
                    "UPDATE documents SET review_reason=$1 "
                    "WHERE doc_id=$2",
                    reason, doc_id,
                )
                review_rel = save_review_file(
                    tmp_path, file.filename or title, reason)
                ledger.log_review(
                    doc_id=str(doc_id),
                    action="AUTO_ISOLATED",
                    reason=reason,
                )
            # 处理版本替代
            superseded = await handle_version_chain(
                conn, space_id, version_key, doc_id, title)
            if superseded:
                ledger.log_commit(
                    topic=f"版本替代: {title}",
                    content=f"新版本 {version_label or ''} 替代了 "
                            f"《{superseded['title']}》({superseded['version_label'] or ''})",
                    event_id=str(uuid.uuid4()),
                    user_id=user_id,
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

            await _task_set(
                conn, task_id,
                "NEEDS_REVIEW" if needs_review else "COMMITTED",
                doc_id,
                "待人工确认" if needs_review else None)
            return {
                "doc_id": str(doc_id),
                "task_id": task_id,
                "status": "NEEDS_REVIEW" if needs_review else "COMMITTED",
                "category": category,
                "rule": rule_id,
                "confidence": confidence,
                "chunks": len(chunks),
                "chars": len(markdown),
                "wiki_path": disk_path,
                "message": ("已隔离到待审核，请在 Web UI 确认分类"
                            if needs_review else "入库成功"),
            }
    except PermissionError as e:
        raise HTTPException(403, str(e))
    except HTTPException:
        raise
    except Exception as e:
        # 尝试标记失败（conn 可能已关闭，忽略错误）
        raise HTTPException(500, f"入库失败: {e}")
    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
"""v3.0 补全：raw 原件保存 + _review 隔离 + 版本链 + commit/review/asset HTTP 端点"""

from fastapi import Header, HTTPException
from pydantic import BaseModel
from typing import Optional
import hashlib
import uuid
import os
import shutil
import re


# ============ 版本链：从文件名提取 version_key ============

def extract_version_key(file_name: str) -> tuple[str, str | None]:
    """
    从文件名提取版本标识。
    返回 (version_key, version_label)。
    如 "接入文档 v1.3.md" → ("接入文档", "v1.3")
    无版本号 → (文件名去后缀, None)
    """
    base = os.path.splitext(os.path.basename(file_name))[0]
    # 匹配 v1.3 / V2.0 / 版本1.2 / 1.3版 等
    m = re.search(r'[vV]?\d+\.\d+', base)
    if m:
        ver = m.group(0)
        if not ver.lower().startswith('v'):
            ver = 'v' + ver
        key = re.sub(r'\s*[vV]?\d+\.\d+\s*', '', base).strip()
        # 清理括号备注
        key = re.sub(r'[（(].*[）)]', '', key).strip()
        return key or base, ver
    return base, None
async def _task_create(conn, user_id: str, file_name: str,
                       target_space: str, idem_key: str) -> str:
    """创建任务 RECEIVED，幂等。"""
    task_id = str(uuid.uuid4())
    try:
        await conn.execute(
            """INSERT INTO ingestion_tasks
               (task_id, idempotency_key, user_id, target_space_id,
                task_status, source_file_name)
               VALUES ($1, $2, $3, $4, 'RECEIVED', $5)""",
            task_id, idem_key, user_id, target_space, file_name,
        )
    except Exception:
        row = await conn.fetchrow(
            "SELECT task_id, task_status FROM ingestion_tasks "
            "WHERE idempotency_key=$1",
            idem_key,
        )
        if row:
            return row["task_id"]
        raise
    return task_id


async def _task_set(conn, task_id: str, status: str,
                    doc_id=None, reason: str | None = None):
    await conn.execute(
        """UPDATE ingestion_tasks
           SET task_status=$1, result_doc_id=$2::uuid,
               review_reason=$3, updated_at=NOW()
           WHERE task_id=$4""",
        status, str(doc_id) if doc_id else None, reason, task_id,
    )




async def handle_version_chain(conn, space_id: str, version_key: str,
                                new_doc_id: uuid.UUID, new_title: str):
    """
    版本链处理：
    - 同一 (space_id, version_key) 下，把旧的 is_latest=0, doc_status='SUPERSEDED'
    - 新文档 supersedes_doc_id 指向旧文档
    返回被替代的旧文档信息（或 None）
    """
    old = await conn.fetchrow(
        """SELECT doc_id, title, version_label FROM documents
           WHERE space_id = $1 AND version_key = $2 AND is_latest = TRUE
           LIMIT 1""",
        space_id, version_key,
    )
    if old:
        await conn.execute(
            """UPDATE documents SET is_latest = FALSE,
               doc_status = 'SUPERSEDED',
               superseded_by = $1,
               updated_at = NOW()
               WHERE doc_id = $2""",
            new_doc_id, old["doc_id"],
        )
        await conn.execute(
            """UPDATE documents SET supersedes_doc_id = $1,
               relation_type = 'REPLACEMENT'
               WHERE doc_id = $2""",
            old["doc_id"], new_doc_id,
        )
        return {"doc_id": str(old["doc_id"]), "title": old["title"],
                "version_label": old["version_label"]}
    return None


# ============ raw 原件保存 ============

def save_raw_file(tmp_path: str, file_hash: str, file_name: str) -> str:
    """
    保存原始文件到 /data/raw/{hash前2位}/{hash}{后缀}。
    不可变：已存在则不覆盖。
    返回 raw 相对路径。
    """
    ext = os.path.splitext(file_name)[1]
    rel = f"raw/{file_hash[:2]}/{file_hash}{ext}"
    full = f"/data/{rel}"
    if not os.path.exists(full):
        os.makedirs(os.path.dirname(full), exist_ok=True)
        shutil.copy2(tmp_path, full)
    return rel


# ============ _review 隔离 ============

def save_review_file(tmp_path: str, file_name: str, reason: str) -> str:
    """保存到 /data/_review/，返回相对路径。"""
    safe = re.sub(r'[^\w\-_.()（） ]', '_', file_name)
    rel = f"_review/{safe}"
    full = f"/data/{rel}"
    os.makedirs(os.path.dirname(full), exist_ok=True)
    shutil.copy2(tmp_path, full)
    # 写原因文件
    with open(full + ".reason.txt", "w", encoding="utf-8") as f:
        f.write(f"隔离原因: {reason}\n")
    return rel


# ============ HTTP 端点：commit (台账) ============

class CommitNoteRequest(BaseModel):
    topic: str
    content: str
    event_id: str | None = None


@app.post("/commit")
async def commit_note(
    req: CommitNoteRequest,
    authorization: str = Header(""),
):
    """对话沉淀到 Living Ledger 台账。"""
    token = authorization.removeprefix("Bearer ").strip()
    if not token:
        raise HTTPException(401, "Missing token")
    assert pool is not None
    try:
        async with profiled_connection(pool, token) as (conn, user_id):
            eid = req.event_id or str(uuid.uuid4())
            ledger.log_commit(
                topic=req.topic,
                content=req.content,
                event_id=eid,
                user_id=user_id,
            )
            return {"event_id": eid, "status": "COMMITTED"}
    except PermissionError as e:
        raise HTTPException(403, str(e))


# ============ HTTP 端点：review (审核) ============

class ReviewRequest(BaseModel):
    doc_id: str
    action: str  # APPROVE / RECLASSIFY / ROLLBACK
    new_category: str | None = None
    reason: str | None = None


@app.post("/review")
async def review_document(
    req: ReviewRequest,
    authorization: str = Header(""),
):
    """审核文档：批准入库 / 重分类 / 回滚版本。"""
    token = authorization.removeprefix("Bearer ").strip()
    if not token:
        raise HTTPException(401, "Missing token")
    assert pool is not None
    try:
        async with profiled_connection(pool, token) as (conn, user_id):
            doc = await conn.fetchrow(
                "SELECT * FROM documents WHERE doc_id = $1::uuid",
                req.doc_id,
            )
            if not doc:
                raise HTTPException(404, "文档不存在")

            if req.action == "APPROVE":
                # 从 _review 移到正式位置
                await conn.execute(
                    "UPDATE documents SET doc_status='ACTIVE' "
                    "WHERE doc_id=$1::uuid",
                    req.doc_id,
                )
                ledger.log_review(
                    doc_id=req.doc_id,
                    action="APPROVE",
                    reason=req.reason or "",
                )
                return {"doc_id": req.doc_id, "status": "APPROVED"}

            elif req.action == "RECLASSIFY":
                if not req.new_category:
                    raise HTTPException(400, "RECLASSIFY 需要 new_category")
                await conn.execute(
                    "UPDATE documents SET category=$1, doc_status='ACTIVE' "
                    "WHERE doc_id=$2::uuid",
                    req.new_category, req.doc_id,
                )
                await conn.execute(
                    "UPDATE document_chunks SET category=$1 "
                    "WHERE doc_id=$2::uuid",
                    req.new_category, req.doc_id,
                )
                ledger.log_review(
                    doc_id=req.doc_id,
                    action=f"RECLASSIFY → {req.new_category}",
                    reason=req.reason or "",
                )
                return {"doc_id": req.doc_id, "status": "RECLASSIFIED",
                        "new_category": req.new_category}

            elif req.action == "ROLLBACK":
                # 回滚到上一个版本
                prev_id = doc["supersedes_doc_id"]
                if not prev_id:
                    raise HTTPException(400, "没有可回滚的上一个版本")
                await conn.execute(
                    "UPDATE documents SET is_latest=FALSE, "
                    "doc_status='REVERTED' WHERE doc_id=$1::uuid",
                    req.doc_id,
                )
                await conn.execute(
                    "UPDATE documents SET is_latest=TRUE, "
                    "doc_status='ACTIVE', superseded_by=NULL "
                    "WHERE doc_id=$1::uuid",
                    prev_id,
                )
                ledger.log_review(
                    doc_id=req.doc_id,
                    action=f"ROLLBACK → {prev_id}",
                    reason=req.reason or "",
                )
                return {"doc_id": req.doc_id, "status": "ROLLED_BACK",
                        "restored": str(prev_id)}
            else:
                raise HTTPException(400, f"未知动作: {req.action}")
    except PermissionError as e:
        raise HTTPException(403, str(e))


# ============ HTTP 端点：asset (取原件) ============

@app.get("/asset/{doc_id}")
async def get_asset(
    doc_id: str,
    authorization: str = Header(""),
):
    """凭 doc_id 取原始文件（RLS 鉴权）。"""
    token = authorization.removeprefix("Bearer ").strip()
    if not token:
        raise HTTPException(401, "Missing token")
    assert pool is not None
    try:
        async with profiled_connection(pool, token) as (conn, user_id):
            doc = await conn.fetchrow(
                "SELECT title, raw_file_path FROM documents "
                "WHERE doc_id = $1::uuid",
                doc_id,
            )
            if not doc:
                # 不泄露存在性
                raise HTTPException(404, "Not found")
            raw_path = doc["raw_file_path"]
            if not raw_path or not os.path.exists(f"/data/{raw_path}"):
                raise HTTPException(404, "原件不存在")
            from fastapi.responses import FileResponse
            return FileResponse(
                f"/data/{raw_path}",
                filename=doc["title"],
            )
    except PermissionError as e:
        raise HTTPException(403, str(e))


# ============ HTTP 端点：inbox (待审核列表) ============

@app.get("/inbox")
async def list_inbox(
    authorization: str = Header(""),
):
    """列出 NEEDS_REVIEW 状态的文档（待审核收件箱）。"""
    token = authorization.removeprefix("Bearer ").strip()
    if not token:
        raise HTTPException(401, "Missing token")
    assert pool is not None
    try:
        async with profiled_connection(pool, token) as (conn, user_id):
            rows = await conn.fetch(
                """SELECT doc_id, title, category, space_id,
                          created_at, review_reason
                   FROM documents
                   WHERE doc_status = 'NEEDS_REVIEW'
                   ORDER BY created_at DESC
                   LIMIT 50""",
            )
            return {
                "count": len(rows),
                "items": [
                    {
                        "doc_id": str(r["doc_id"]),
                        "title": r["title"],
                        "category": r["category"],
                        "space_id": r["space_id"],
                        "created_at": r["created_at"].isoformat(),
                        "reason": r.get("review_reason", ""),
                    }
                    for r in rows
                ],
            }
    except PermissionError as e:
        raise HTTPException(403, str(e))


@app.get("/tasks")
async def list_tasks(
    limit: int = 20,
    authorization: str = Header(""),
):
    """查询入库任务状态。"""
    token = authorization.removeprefix("Bearer ").strip()
    if not token:
        raise HTTPException(401, "Missing token")
    assert pool is not None
    try:
        async with profiled_connection(pool, token) as (conn, user_id):
            rows = await conn.fetch(
                """SELECT task_id, source_file_name, task_status,
                          result_doc_id, review_reason,
                          created_at, updated_at
                   FROM ingestion_tasks
                   ORDER BY created_at DESC
                   LIMIT $1""",
                min(limit, 100),
            )
            return {
                "count": len(rows),
                "tasks": [
                    {
                        "task_id": r["task_id"],
                        "file": r["source_file_name"],
                        "status": r["task_status"],
                        "doc_id": str(r["result_doc_id"])
                                  if r["result_doc_id"] else None,
                        "reason": r["review_reason"],
                        "created_at": r["created_at"].isoformat(),
                        "updated_at": r["updated_at"].isoformat(),
                    }
                    for r in rows
                ],
            }
    except PermissionError as e:
        raise HTTPException(403, str(e))
