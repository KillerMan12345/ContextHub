# ContextHub Gateway - Phase 0
# FastAPI + asyncpg，RLS 上下文注入，Fail-Closed

from contextlib import asynccontextmanager
from typing import AsyncGenerator
import asyncpg
import hashlib
import os

DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql://contexthub_app:change_me_in_env@localhost:5432/contexthub",
)


@asynccontextmanager
async def profiled_connection(
    pool: asyncpg.Pool, token: str
) -> AsyncGenerator[tuple[asyncpg.Connection, str], None]:
    """
    在独立事务内注入读写分离的 Fail-Closed RLS 上下文。
    事务结束自动重置，杜绝连接池污染。
    """
    async with pool.acquire() as conn:
        async with conn.transaction():
            # 先清空（Fail-Closed 保底）
            await conn.execute(
                "SELECT set_config('app.allowed_read_spaces', '', true), "
                "set_config('app.allowed_write_spaces', '', true), "
                "set_config('app.allowed_personal_cats', '', true), "
                "set_config('app.current_user_id', '', true)"
            )
            # 校验 Token（只存哈希）
            token_hash = hashlib.sha256(token.encode()).hexdigest()
            row = await conn.fetchrow(
                """
                SELECT user_id, allowed_personal_categories
                FROM access_tokens
                WHERE token_hash = $1
                  AND revoked_at IS NULL
                  AND (expires_at IS NULL OR expires_at > CURRENT_TIMESTAMP)
                """,
                token_hash,
            )
            if not row:
                raise PermissionError("Invalid, expired, or revoked token")

            user_id: str = row["user_id"]
            # 解析挂载空间（简化版：直接查 memberships；完整版调 fn_resolve_mounted_spaces）
            read_spaces = await conn.fetch(
                """
                SELECT DISTINCT s.space_id
                FROM space_memberships m
                JOIN spaces s ON m.space_id = s.space_id
                WHERE m.user_id = $1 AND m.revoked_at IS NULL
                """,
                user_id,
            )
            write_spaces = await conn.fetch(
                """
                SELECT DISTINCT s.space_id
                FROM space_memberships m
                JOIN spaces s ON m.space_id = s.space_id
                WHERE m.user_id = $1 AND m.revoked_at IS NULL
                  AND m.role IN ('WRITER', 'ADMIN')
                """,
                user_id,
            )
            personal_cats = ",".join(row["allowed_personal_categories"] or [])

            await conn.execute(
                "SELECT set_config('app.allowed_read_spaces', $1, true)",
                ",".join(r["space_id"] for r in read_spaces),
            )
            await conn.execute(
                "SELECT set_config('app.allowed_write_spaces', $1, true)",
                ",".join(r["space_id"] for r in write_spaces),
            )
            await conn.execute(
                "SELECT set_config('app.allowed_personal_cats', $1, true)",
                personal_cats,
            )
            await conn.execute(
                "SELECT set_config('app.current_user_id', $1, true)", user_id
            )
            yield conn, user_id
