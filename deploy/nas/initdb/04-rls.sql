-- 04-rls.sql : 行级安全策略（Fail-Closed）
-- 原则：空授权上下文默认拒绝；读写分离；个人空间子目录白名单

-- 辅助函数：个人空间子目录是否放行
CREATE OR REPLACE FUNCTION fn_check_category_access(p_space_id VARCHAR, p_category VARCHAR)
RETURNS BOOLEAN LANGUAGE sql STABLE AS $$
    SELECT CASE
        WHEN p_space_id NOT LIKE 'space_personal_%' THEN TRUE
        WHEN COALESCE(current_setting('app.allowed_personal_cats', true), '') = '' THEN FALSE
        WHEN current_setting('app.allowed_personal_cats', true) = '*' THEN TRUE
        ELSE p_category = ANY (string_to_array(current_setting('app.allowed_personal_cats', true), ','))
    END;
$$;

-- ===== documents =====
ALTER TABLE documents ENABLE ROW LEVEL SECURITY;
ALTER TABLE documents FORCE ROW LEVEL SECURITY;

CREATE POLICY rls_docs_select ON documents FOR SELECT TO contexthub_app
USING (
    COALESCE(current_setting('app.allowed_read_spaces', true), '') <> ''
    AND space_id = ANY (string_to_array(current_setting('app.allowed_read_spaces', true), ','))
    AND fn_check_category_access(space_id, category)
);

CREATE POLICY rls_docs_insert ON documents FOR INSERT TO contexthub_app
WITH CHECK (
    COALESCE(current_setting('app.allowed_write_spaces', true), '') <> ''
    AND space_id = ANY (string_to_array(current_setting('app.allowed_write_spaces', true), ','))
    AND fn_check_category_access(space_id, category)
);

CREATE POLICY rls_docs_update ON documents FOR UPDATE TO contexthub_app
USING (
    COALESCE(current_setting('app.allowed_write_spaces', true), '') <> ''
    AND space_id = ANY (string_to_array(current_setting('app.allowed_write_spaces', true), ','))
    AND fn_check_category_access(space_id, category)
)
WITH CHECK (
    COALESCE(current_setting('app.allowed_write_spaces', true), '') <> ''
    AND space_id = ANY (string_to_array(current_setting('app.allowed_write_spaces', true), ','))
    AND fn_check_category_access(space_id, category)
);

CREATE POLICY rls_docs_delete ON documents FOR DELETE TO contexthub_app
USING (
    COALESCE(current_setting('app.allowed_write_spaces', true), '') <> ''
    AND space_id = ANY (string_to_array(current_setting('app.allowed_write_spaces', true), ','))
    AND fn_check_category_access(space_id, category)
);

-- ===== document_chunks =====
ALTER TABLE document_chunks ENABLE ROW LEVEL SECURITY;
ALTER TABLE document_chunks FORCE ROW LEVEL SECURITY;

CREATE POLICY rls_chunks_select ON document_chunks FOR SELECT TO contexthub_app
USING (
    COALESCE(current_setting('app.allowed_read_spaces', true), '') <> ''
    AND space_id = ANY (string_to_array(current_setting('app.allowed_read_spaces', true), ','))
    AND fn_check_category_access(space_id, category)
);

CREATE POLICY rls_chunks_write ON document_chunks FOR ALL TO contexthub_app
USING (
    COALESCE(current_setting('app.allowed_write_spaces', true), '') <> ''
    AND space_id = ANY (string_to_array(current_setting('app.allowed_write_spaces', true), ','))
    AND fn_check_category_access(space_id, category)
)
WITH CHECK (
    COALESCE(current_setting('app.allowed_write_spaces', true), '') <> ''
    AND space_id = ANY (string_to_array(current_setting('app.allowed_write_spaces', true), ','))
    AND fn_check_category_access(space_id, category)
);

-- ===== health_metrics =====
ALTER TABLE health_metrics ENABLE ROW LEVEL SECURITY;
ALTER TABLE health_metrics FORCE ROW LEVEL SECURITY;

CREATE POLICY rls_health_select ON health_metrics FOR SELECT TO contexthub_app
USING (
    COALESCE(current_setting('app.allowed_read_spaces', true), '') <> ''
    AND space_id = ANY (string_to_array(current_setting('app.allowed_read_spaces', true), ','))
    AND fn_check_category_access(space_id, '04_Health_Archive')
);

CREATE POLICY rls_health_write ON health_metrics FOR ALL TO contexthub_app
USING (
    COALESCE(current_setting('app.allowed_write_spaces', true), '') <> ''
    AND space_id = ANY (string_to_array(current_setting('app.allowed_write_spaces', true), ','))
    AND fn_check_category_access(space_id, '04_Health_Archive')
)
WITH CHECK (
    COALESCE(current_setting('app.allowed_write_spaces', true), '') <> ''
    AND space_id = ANY (string_to_array(current_setting('app.allowed_write_spaces', true), ','))
    AND fn_check_category_access(space_id, '04_Health_Archive')
);

-- ===== ingestion_tasks : 用户只能看自己的任务 =====
ALTER TABLE ingestion_tasks ENABLE ROW LEVEL SECURITY;
ALTER TABLE ingestion_tasks FORCE ROW LEVEL SECURITY;

CREATE POLICY rls_tasks_select ON ingestion_tasks FOR SELECT TO contexthub_app
USING (
    user_id = current_setting('app.current_user_id', true)
);

CREATE POLICY rls_tasks_write ON ingestion_tasks FOR ALL TO contexthub_app
USING (
    user_id = current_setting('app.current_user_id', true)
)
WITH CHECK (
    user_id = current_setting('app.current_user_id', true)
);
