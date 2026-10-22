-- seed_test_data.sql : Phase 0 安全测试用的种子数据
-- 用 migrator 角色执行（绕过 RLS）

-- 用户
INSERT INTO users (user_id, display_name) VALUES
    ('u1', '测试用户1'),
    ('u_reader', '只读用户'),
    ('u_attacker', '攻击者'),
    ('u_victim', '受害者')
ON CONFLICT (user_id) DO NOTHING;

-- 空间
INSERT INTO spaces (space_id, space_name, space_archetype, tree_path, vault_rel_path, allowed_l1_folders, created_by) VALUES
    ('space_personal_u1', 'u1 的个人空间', 'PERSONAL_VAULT', 'root.u1', 'spaces/space_personal_u1',
     '{01_Work,02_Life,03_Family,04_Health_Archive}', 'u1'),
    ('space_personal_other', '别人的个人空间', 'PERSONAL_VAULT', 'root.other', 'spaces/space_personal_other',
     '{01_Work}', 'u_victim')
ON CONFLICT (space_id) DO NOTHING;

-- 成员
INSERT INTO space_memberships (user_id, space_id, role) VALUES
    ('u1', 'space_personal_u1', 'ADMIN'),
    ('u_reader', 'space_personal_u1', 'READER'),
    ('u_attacker', 'space_personal_u1', 'READER'),
    ('u_victim', 'space_personal_other', 'ADMIN')
ON CONFLICT (user_id, space_id) DO NOTHING;

-- 文档（含健康类别）
INSERT INTO documents (space_id, category, title, file_hash, wiki_file_path, created_by) VALUES
    ('space_personal_u1', '01_Work', '工作文档', 'hash_work_001', 'wiki/01_Work/doc1.md', 'u1'),
    ('space_personal_u1', '04_Health_Archive', '体检报告', 'hash_health_001', 'wiki/04_Health_Archive/report.md', 'u1')
ON CONFLICT (space_id, file_hash) DO NOTHING;

-- 健康指标
INSERT INTO health_metrics (space_id, source_doc_id, person_name, metric_name, metric_value, unit, measured_date)
SELECT 'space_personal_u1', doc_id, '父亲', '空腹血糖', 5.8, 'mmol/L', CURRENT_DATE
FROM documents WHERE title = '体检报告'
ON CONFLICT DO NOTHING;

-- 任务（u_victim 的，u_attacker 不该看到）
INSERT INTO ingestion_tasks (task_id, idempotency_key, user_id, task_status) VALUES
    ('ing_victim_001', 'idem_victim_001', 'u_victim', 'COMMITTED')
ON CONFLICT (task_id) DO NOTHING;
