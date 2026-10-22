-- 02-roles.sql : 最小权限角色
-- 迁移角色：建表、建策略（initdb 用 POSTGRES_USER 即 migrator 执行）
-- 业务角色：contexthub_app，NOBYPASSRLS，用于网关在线请求

DO $$ BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'contexthub_app') THEN
        CREATE ROLE contexthub_app LOGIN PASSWORD 'change_me_in_env'
            NOBYPASSRLS NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT;
    END IF;
END $$;

-- 业务角色默认只能连 contexthub 库
GRANT CONNECT ON DATABASE contexthub TO contexthub_app;
