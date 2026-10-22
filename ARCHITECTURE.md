# ContextHub 最终架构方案（合并版）

> **版本：** v3.0（合并定稿）
> **日期：** 2026-10-22
> **来源：** v2.1（Gemini 起草）+ v2.2（ChatGPT 评审）逐项对比后的独立结论
> **定位：** 开源项目；NAS 为自我实践环境；目标推广至部门使用
> **许可证：** Apache-2.0（本阶段确定，不 deferred）

---

## 0. 评审结论：不站队，逐项取优

| 维度 | 采用 | 理由 |
|---|---|---|
| 安全架构（RLS、fail-closed、GUC 认知） | v2.2 | 实质性差距，v2.1 只提了概念 |
| n8n 定位 | v2.2 的原则 + v2.1 的实践 | 编排归 n8n，授权死守网关层；开源项目需要可视化工作流降低接手门槛 |
| Living Ledger 台账 | v2.1 | v2.2 遗漏，细粒度知识沉淀的关键机制，必须加回 |
| Phase 0 先验证再开发 | v2.2 | 工程纪律，不妥协 |
| 检索策略 | v2.1 起步 → v2.2 目标 | 先 qmd 轻量本地搜索跑通，向量混合检索为优化项 |
| 版本模型 | v2.2 表述 | 更干净，等价 |
| 人看界面 | 新增判断 | Obsidian 留给个人深度用户；部门推广必须做 Web UI |
| 推广钩子 | 新增判断 | MCP + IDE 编码感知进 Phase 1，不是 Phase 3 |

---

## 1. 架构原则（定稿）

| ID | 原则 | 说明 |
|---|---|---|
| D-01 | 原始文件不可变 | 原始字节只读；SHA-256 用于去重与完整性校验 |
| D-02 | Wiki Markdown 是知识正文载体 | 人可读、可编辑；Obsidian 直接打开 |
| D-03 | 系统元数据由数据库管 | doc_id、空间、版本指针、任务状态只走受控写入；Frontmatter 对应字段是投影，不可手改生效 |
| D-04 | 检索索引是派生数据 | 向量、全文、切片均可重建；不作为权限事实源 |
| D-05 | 不确定路由进隔离 | 禁止自动跨边界共享；归属不明进 NEEDS_REVIEW |
| D-06 | 版本唯一性 | 同一 `(space_id, version_key)` 下只有一个当前版本，数据库唯一约束保证 |
| D-07 | 无跨系统原子事务 | 文件系统 + PostgreSQL 之间用 Saga（暂存→事务→发布→确认），可恢复、可审计 |
| D-08 | 授权 = Token ∩ Membership ∩ 空间规则 ∩ 类别规则 | 缺任何一项即拒绝 |
| D-09 | RLS 是纵深防御 | 不替代网关层授权；GUC 变量不是独立身份凭证 |
| D-10 | 分期交付 | 先个人闭环（NAS 验证），再家庭健康，再部门推广；数据模型一次到位 |

---

## 2. 系统总体架构

```text
┌─ 客户端层 ─────────────────────────────────────────────┐
│ IDE (MCP) │ Obsidian │ Web UI (Phase 2+) │ 飞书 Bot      │
└──────────────────────┬─────────────────────────────────┘
                       │ MCP / HTTPS；只传 doc_id，不传裸路径
                       ▼
┌─ 网关层 (FastAPI + FastMCP) ───────────────────────────┐
│ Token 校验 → Membership 解析 → 参数校验 → 授权 → 审计   │
│ 工具: search / ingest / commit / review / asset / health│
└──────────┬────────────────────────────┬────────────────┘
           │ 受控 SQL + RLS              │ 任务 ID
           ▼                             ▼
┌─ PostgreSQL 16 ──────────┐  ┌─ n8n / Worker ──────────┐
│ 用户/空间/成员/Token      │  │ WF-01 入库状态机         │
│ 文档/版本/任务/审计       │  │ WF-02 健康指标提取       │
│ chunks/全文/(向量*)       │  │ WF-03 每周 Lint         │
│ RLS 强制 + 最小权限角色   │  │ WF-04 Obsidian 回写     │
└──────────┬───────────────┘  └──────────┬──────────────┘
           └──────────────┬──────────────┘
                          ▼
┌─ 文件存储 (NAS) ───────────────────────────────────────┐
│ raw/ (不可变原件) │ wiki/ (Markdown) │ _review/ (隔离区) │
└──────────────────────────────────────────────────────┘
```

*向量索引为可选优化项，初期用 PostgreSQL 全文检索（FTS）+ qmd 本地搜索。*

---

## 3. 数据模型（核心表）

```sql
-- 空间：个人/家庭/组织统一抽象
spaces(space_id, space_name, space_archetype, parent_space_id,
       tree_path ltree, vault_rel_path, allowed_l1_folders TEXT[])

-- 成员：谁在什么空间是什么角色
space_memberships(user_id, space_id, role, revoked_at)
-- role: READER / WRITER / ADMIN

-- Token：场景化视图钥匙
access_tokens(token_hash, user_id, profile_name,
              allowed_personal_categories TEXT[],  -- Fail-Closed：空=拒绝
              expires_at, revoked_at)

-- 文档：版本链核心
documents(doc_id, space_id, category, title,
          file_hash, wiki_file_path,
          version_key, version_label, is_latest,
          relation_type,              -- NEW / REPLACEMENT / SUPPLEMENT
          supersedes_doc_id, superseded_by, parent_doc_id,
          doc_status)                 -- ACTIVE / SUPERSEDED / REVERTED
-- 硬约束：
--   UNIQUE(space_id, file_hash)                    -- 去重
--   UNIQUE(space_id, version_key) WHERE is_latest   -- 版本唯一

-- 切片：检索单元，带章节溯源
document_chunks(chunk_id, doc_id, space_id, category,
                section_path, page_number, content)
-- 注：embedding 列预留，初期不上向量

-- 入库任务：幂等状态机
ingestion_tasks(task_id, idempotency_key UNIQUE, user_id,
                target_space_id, task_status, review_reason,
                retry_count, audit_payload JSONB)
-- task_status: RECEIVED → PRECHECKING → ROUTING
--            → STAGED → COMMITTING → COMMITTED
--            → NEEDS_REVIEW / FAILED_RETRYABLE / FAILED_FINAL / CANCELLED

-- 健康指标（Phase 2）
health_metrics(metric_id, source_doc_id, person_name, metric_name,
               metric_value, unit, reference_range, is_abnormal,
               verification_status)  -- AI_EXTRACTED / NEEDS_CHECK / HUMAN_VERIFIED

-- 审计（只追加）
audit_events(event_id, actor, action, object_type, object_id,
             before_summary, after_summary, created_at)
```

### RLS 要点（v2.2 精髓，保留）

- 业务角色 `contexthub_app` 为 `NOBYPASSRLS`；所有业务表 `FORCE ROW LEVEL SECURITY`
- 读写策略分离；空授权上下文默认拒绝（Fail-Closed）
- 个人空间子目录用 `allowed_personal_categories` 白名单控制
- 网关层每次请求重新计算授权，不信任缓存；Token 撤销 5 秒内生效（Phase 0 实测）

---

## 4. 入库流水线（n8n WF-01）

```text
RECEIVED（幂等登记：SHA-256 + idempotency_key）
  → PRECHECKING（Token 有效？目标空间可写？文件可读？敏感预检）
  → ROUTING（显式目标 > 确定性规则 > LLM 语义分类）
  → STAGED（编译 Markdown 草稿到暂存区）
  → COMMITTING（Saga：写 DB 事务 → 原子移动文件 → 更新索引）
  → COMMITTED（可检索）

  分支：归属不明 / 跨边界 / 敏感 → NEEDS_REVIEW（隔离区，人工确认）
  分支：临时故障 → FAILED_RETRYABLE（自动重试）
  分支：输入不合规 → FAILED_FINAL
```

**路由铁律**：规则优先于 LLM；LLM 只给候选+理由，不做权限决定。

---

## 5. Living Ledger 台账（从 v2.1 加回）

整文档入库之外，细粒度事实走台账：

```text
wiki/_ledgers/_living_ledger_<主题>.md
```

- 对话中的结论、Bug 根因、决策记录 → `commit_memory_note` 追加
- 每条有 `event_id`，幂等追加
- 用户在 Obsidian 改台账 → WF-04 回写数据库
- 每周 Lint 检查：过期项归档、同义项合并建议（只建议不自动改）

---

## 6. MCP 接口（推广钩子，Phase 1 必备）

| 工具 | 用途 | 强制返回 |
|---|---|---|
| `search_knowledge` | 混合检索 | 切片 + `来源：《文档名(版本)》- 章节路径，第X页` |
| `get_document_asset` | 凭 doc_id 取原件/附件 | RLS 鉴权后返回；拒绝不泄露存在性 |
| `ingest_document_or_file` | 入库 | 结构化回执卡（任务ID/状态/路径/摘要/纠错指令） |
| `commit_memory_note` | 对话沉淀到台账 | 回执 + 台账快照 |
| `review_and_fix_knowledge` | 批准/重分类/回滚/纠错 | 白名单动作，变更前后 Diff |
| `query_health_trends` | 健康指标趋势（Phase 2） | 指标表 + 参考范围 + 核验状态 |

**IDE 编码感知**（v2.1 FR-11，提前到 Phase 1）：MCP 暴露代码符号检索，Cursor/Claude Code 写代码时自动拉取关联的接口规范和 ADR。这是部门愿意试用的理由。

---

## 7. 分阶段路线图

| 阶段 | 周期 | 交付 | 退出标准 |
|---|---|---|---|
| **Phase 0** | 3-5 天 | PG + RLS + 网关 PoC；6 项负向安全测试 | 全部通过；双向同步无死循环 |
| **Phase 1** | 1.5-2 周 | 个人空间闭环：入库/分类/版本/检索/MCP/IDE 感知；Living Ledger；Obsidian 控制台 | 每天稳定入库；版本无并发双激活；引用 100% 可溯源 |
| **Phase 2** | 2 周 | 家庭空间 + 健康档案（VLM 提取 + 人工复核）；Web UI v1；每周 Lint | 健康指标 100% 异常触发复核；家庭隔离测试通过 |
| **Phase 3** | 按需 | 部门组织树；多成员；审计报表 | 跨边界误共享率为 0 |

---

## 8. 开源工程（现在就定）

| 项 | 决策 |
|---|---|
| License | **Apache-2.0** |
| 仓库 | 单仓 monorepo：`gateway/` `worker/` `web/` `docs/` `deploy/` |
| 依赖审计 | Phase 0 检查 n8n 等依赖的协议兼容性 |
| 文档 | README（5 分钟上手）+ ARCHITECTURE.md（本文件）+ CONTRIBUTING.md |
| NAS 部署 | `deploy/nas/docker-compose.yml`（PG + 网关 + n8n）即 dogfood 环境 |

---

## 9. 与 v2.1/v2.2 的关系

本文件为最终依据。v2.2 的安全设计全文采纳；v2.1 的 Living Ledger、n8n 实践、qmd 轻检索采纳；两份中与本文件冲突的，以本文件为准。

*—— 2026-10-22，基于事实的独立评审结论。*
