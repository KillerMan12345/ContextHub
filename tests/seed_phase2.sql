-- Phase 2: 家庭空间 + 健康档案种子数据
-- 用 migrator 执行

-- 家庭空间
INSERT INTO spaces (space_id, space_name, space_archetype, tree_path, vault_rel_path, allowed_l1_folders, created_by) VALUES
    ('space_family_01', '家庭空间', 'FAMILY_HUB', 'root.family', 'spaces/space_family_01',
     '{01_Work,02_Life,03_Family,04_Health_Archive}', 'u1')
ON CONFLICT (space_id) DO NOTHING;

-- 成员：u1 是 ADMIN（后续可加妻子）
INSERT INTO space_memberships (user_id, space_id, role) VALUES
    ('u1', 'space_family_01', 'ADMIN')
ON CONFLICT (user_id, space_id) DO NOTHING;

-- 示例：父母健康档案分类说明文档（占位，真实体检报告由用户上传）
INSERT INTO documents (space_id, category, title, file_hash, wiki_file_path, created_by) VALUES
    ('space_family_01', '04_Health_Archive', '健康档案说明.md', 'hash_health_readme_001',
     'wiki/04_Health_Archive/健康档案说明.md', 'u1')
ON CONFLICT (space_id, file_hash) DO NOTHING;
