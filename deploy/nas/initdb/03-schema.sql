-- 03-schema.sql : 核心表（Phase 0 最小集）
-- 完整模型见 ARCHITECTURE.md §8；Phase 2 的 health_metrics 在此预留表结构

-- 用户
CREATE TABLE IF NOT EXISTS users (
    user_id     VARCHAR(64) PRIMARY KEY,
    display_name VARCHAR(128) NOT NULL,
    email       VARCHAR(128) UNIQUE,
    created_at  TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
);

-- 空间
CREATE TABLE IF NOT EXISTS spaces (
    space_id        VARCHAR(64) PRIMARY KEY,
    space_name      VARCHAR(128) NOT NULL,
    space_archetype VARCHAR(32) NOT NULL CHECK (space_archetype IN (
        'PERSONAL_VAULT', 'FAMILY_HUB', 'ORG_RESTRICTED', 'ORG_PUBLIC'
    )),
    parent_space_id VARCHAR(64) REFERENCES spaces(space_id),
    tree_path       ltree NOT NULL,
    vault_rel_path  TEXT NOT NULL UNIQUE,
    allowed_l1_folders TEXT[] NOT NULL DEFAULT '{}',
    created_by      VARCHAR(64) REFERENCES users(user_id),
    created_at      TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_spaces_tree_path ON spaces USING gist (tree_path);

-- 成员关系
CREATE TABLE IF NOT EXISTS space_memberships (
    membership_id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id   VARCHAR(64) NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    space_id  VARCHAR(64) NOT NULL REFERENCES spaces(space_id) ON DELETE CASCADE,
    role      VARCHAR(32) NOT NULL CHECK (role IN ('READER', 'WRITER', 'ADMIN')),
    revoked_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(user_id, space_id)
);

-- Token
CREATE TABLE IF NOT EXISTS access_tokens (
    token_hash  VARCHAR(128) PRIMARY KEY,  -- 只存哈希，不存原文
    user_id     VARCHAR(64) NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    profile_name VARCHAR(64) NOT NULL,
    allowed_personal_categories TEXT[] NOT NULL DEFAULT '{01_Work}',
    expires_at  TIMESTAMPTZ,
    revoked_at  TIMESTAMPTZ,
    created_at  TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
);

-- 文档
CREATE TABLE IF NOT EXISTS documents (
    doc_id      UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    space_id    VARCHAR(64) NOT NULL REFERENCES spaces(space_id) ON DELETE CASCADE,
    category    VARCHAR(64) NOT NULL,
    title       VARCHAR(256) NOT NULL,
    file_hash   VARCHAR(64) NOT NULL,
    wiki_file_path TEXT NOT NULL,
    version_key VARCHAR(128),
    version_label VARCHAR(32) DEFAULT 'v1.0',
    is_latest   BOOLEAN DEFAULT TRUE,
    relation_type VARCHAR(32) DEFAULT 'NEW'
        CHECK (relation_type IN ('NEW', 'REPLACEMENT', 'SUPPLEMENT')),
    supersedes_doc_id UUID REFERENCES documents(doc_id),
    superseded_by UUID REFERENCES documents(doc_id),
    parent_doc_id UUID REFERENCES documents(doc_id),
    doc_status  VARCHAR(32) DEFAULT 'ACTIVE'
        CHECK (doc_status IN ('ACTIVE', 'SUPERSEDED', 'REVERTED')),
    created_by  VARCHAR(64) NOT NULL REFERENCES users(user_id),
    created_at  TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    updated_at  TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_space_raw_file_hash UNIQUE (space_id, file_hash)
);
-- 版本唯一性硬约束：同一空间同一 version_key 只有一个当前版本
CREATE UNIQUE INDEX IF NOT EXISTS uq_docs_active_version_key
    ON documents (space_id, version_key)
    WHERE is_latest = TRUE AND version_key IS NOT NULL;

-- 切片（含章节溯源；embedding 列预留）
CREATE TABLE IF NOT EXISTS document_chunks (
    chunk_id    UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    doc_id      UUID NOT NULL REFERENCES documents(doc_id) ON DELETE CASCADE,
    space_id    VARCHAR(64) NOT NULL,
    category    VARCHAR(64) NOT NULL,
    is_latest   BOOLEAN DEFAULT TRUE,
    doc_title   VARCHAR(256) NOT NULL,
    section_path TEXT NOT NULL,
    page_number INT,
    content     TEXT NOT NULL,
    -- embedding vector(1024),  -- Phase 1 按需启用
    created_at  TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_chunks_space_cat
    ON document_chunks (space_id, category) WHERE is_latest = TRUE;
-- 全文检索索引（Phase 0 先用 FTS，向量后加）
CREATE INDEX IF NOT EXISTS idx_chunks_content_trgm
    ON document_chunks USING gin (content gin_trgm_ops);

-- 入库任务状态机
CREATE TABLE IF NOT EXISTS ingestion_tasks (
    task_id         VARCHAR(64) PRIMARY KEY,
    idempotency_key VARCHAR(128) UNIQUE NOT NULL,
    user_id         VARCHAR(64) NOT NULL REFERENCES users(user_id),
    target_space_id VARCHAR(64) REFERENCES spaces(space_id),
    task_status     VARCHAR(32) NOT NULL CHECK (task_status IN (
        'RECEIVED', 'PRECHECKING', 'ROUTING', 'STAGED', 'COMMITTING',
        'COMMITTED', 'NEEDS_REVIEW',
        'FAILED_RETRYABLE', 'FAILED_FINAL', 'CANCELLED', 'REVERTED'
    )),
    source_file_name TEXT,
    result_doc_id   UUID REFERENCES documents(doc_id),
    review_reason   TEXT,
    retry_count     SMALLINT DEFAULT 0,
    created_at      TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    updated_at      TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
);

-- 审计（只追加）
CREATE TABLE IF NOT EXISTS audit_events (
    event_id    UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    actor       VARCHAR(64) NOT NULL,
    action      VARCHAR(64) NOT NULL,
    object_type VARCHAR(32) NOT NULL,
    object_id   VARCHAR(128) NOT NULL,
    summary     TEXT,
    created_at  TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
);

-- 健康指标（Phase 2 用，表结构先建）
CREATE TABLE IF NOT EXISTS health_metrics (
    metric_id   UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    space_id    VARCHAR(64) NOT NULL REFERENCES spaces(space_id) ON DELETE CASCADE,
    source_doc_id UUID NOT NULL REFERENCES documents(doc_id) ON DELETE CASCADE,
    person_name VARCHAR(64) NOT NULL,
    metric_name VARCHAR(128) NOT NULL,
    metric_value NUMERIC(10, 3),
    unit        VARCHAR(32),
    reference_range VARCHAR(64),
    is_abnormal BOOLEAN DEFAULT FALSE,
    verification_status VARCHAR(32) DEFAULT 'AI_EXTRACTED'
        CHECK (verification_status IN ('AI_EXTRACTED', 'NEEDS_CHECK', 'HUMAN_VERIFIED')),
    measured_date DATE NOT NULL,
    created_at  TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
);

-- 业务角色授权（精确到表）
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO contexthub_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO contexthub_app;
