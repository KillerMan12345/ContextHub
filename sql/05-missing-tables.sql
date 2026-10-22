-- v2.3 补全：5 张缺失表
-- 在 NAS 上执行

-- 1. document_series：逻辑文档系列
CREATE TABLE IF NOT EXISTS document_series (
    series_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    space_id VARCHAR(64) NOT NULL REFERENCES spaces(space_id),
    version_key VARCHAR(256) NOT NULL,
    current_doc_id UUID REFERENCES documents(doc_id),
    revision INT NOT NULL DEFAULT 1,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(space_id, version_key)
);

-- 2. document_relations：文档关系（SUPPLEMENT/派生/引用）
CREATE TABLE IF NOT EXISTS document_relations (
    relation_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    source_doc_id UUID NOT NULL REFERENCES documents(doc_id) ON DELETE CASCADE,
    target_doc_id UUID NOT NULL REFERENCES documents(doc_id) ON DELETE CASCADE,
    relation_type VARCHAR(32) NOT NULL
        CHECK (relation_type IN ('SUPPLEMENT','DERIVED','CITES','PROMOTED_FROM')),
    created_by VARCHAR(64),
    created_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(source_doc_id, target_doc_id, relation_type)
);

-- 3. assets：原始资产独立表
CREATE TABLE IF NOT EXISTS assets (
    asset_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    sha256 VARCHAR(64) NOT NULL UNIQUE,
    storage_path VARCHAR(512) NOT NULL,
    original_name VARCHAR(512),
    mime_type VARCHAR(128),
    size_bytes BIGINT,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- 4. outbox_events：可恢复 Outbox
CREATE TABLE IF NOT EXISTS outbox_events (
    event_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    event_type VARCHAR(64) NOT NULL,
    payload JSONB NOT NULL,
    status VARCHAR(16) NOT NULL DEFAULT 'PENDING'
        CHECK (status IN ('PENDING','DELIVERED','FAILED')),
    retry_count SMALLINT DEFAULT 0,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    delivered_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_outbox_pending
    ON outbox_events(status, created_at) WHERE status='PENDING';

-- 5. model_runs：模型调用审计
CREATE TABLE IF NOT EXISTS model_runs (
    run_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    task_id VARCHAR(64),
    doc_id UUID,
    provider VARCHAR(32) NOT NULL,
    model_id VARCHAR(128) NOT NULL,
    prompt_version VARCHAR(32),
    input_tokens INT,
    output_tokens INT,
    duration_ms INT,
    status VARCHAR(16) NOT NULL DEFAULT 'OK'
        CHECK (status IN ('OK','TIMEOUT','RATE_LIMIT','ERROR')),
    error_code VARCHAR(64),
    created_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_model_runs_task
    ON model_runs(task_id, created_at);
