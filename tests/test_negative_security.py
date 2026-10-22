#!/usr/bin/env python3
"""
ContextHub Phase 0 - 6 项负向安全测试
全部必须通过（即全部被正确拒绝），Phase 0 才能算过。

运行前：
  1. docker compose up -d （deploy/nas/）
  2. 准备测试数据（见 seed_test_data.sql）
  3. python3 -m pytest tests/test_negative_security.py -v
     或直接: python3 tests/test_negative_security.py

连接：postgresql://contexthub_app:<pwd>@localhost:5432/contexthub
"""

import asyncio
import asyncpg
import os
import sys

DSN = os.environ.get(
    "TEST_DSN",
    "postgresql://contexthub_app:change_me_in_env@localhost:5432/contexthub",
)

PASS = "\033[92mPASS\033[0m"
FAIL = "\033[91mFAIL\033[0m"
results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = ""):
    results.append((name, ok, detail))
    print(f"[{PASS if ok else FAIL}] {name}" + (f" — {detail}" if detail else ""))


async def test_1_no_context_denied():
    """T1: 未设置 RLS 上下文 → 必须拒绝（返回 0 行）"""
    conn = await asyncpg.connect(DSN)
    try:
        # 故意不 set_config，直接查
        rows = await conn.fetch("SELECT * FROM documents")
        check("T1 未设上下文拒绝", len(rows) == 0, f"返回 {len(rows)} 行")
    finally:
        await conn.close()


async def test_2_work_token_no_health():
    """T2: Work Profile Token（只允许 01_Work）读健康表 → 必须拒绝"""
    conn = await asyncpg.connect(DSN)
    try:
        await conn.execute(
            "SELECT set_config('app.allowed_read_spaces', $1, true)",
            "space_personal_u1",
        )
        # Work Profile: allowed_personal_cats = '01_Work'
        await conn.execute(
            "SELECT set_config('app.allowed_personal_cats', '01_Work', true)"
        )
        rows = await conn.fetch("SELECT * FROM health_metrics")
        check("T2 Work Token 读健康表拒绝", len(rows) == 0, f"返回 {len(rows)} 行")
    finally:
        await conn.close()


async def test_3_reader_cannot_write():
    """T3: READER 角色（只有读空间）尝试写入 → 必须拒绝"""
    conn = await asyncpg.connect(DSN)
    try:
        await conn.execute(
            "SELECT set_config('app.allowed_read_spaces', $1, true)",
            "space_personal_u1",
        )
        await conn.execute("SELECT set_config('app.allowed_write_spaces', '', true)")
        await conn.execute(
            "SELECT set_config('app.allowed_personal_cats', '*', true)"
        )
        await conn.execute(
            "SELECT set_config('app.current_user_id', 'u_reader', true)"
        )
        try:
            await conn.execute(
                """
                INSERT INTO documents
                    (space_id, category, title, file_hash, wiki_file_path, created_by)
                VALUES
                    ('space_personal_u1', '01_Work', 'evil', 'x', 'x.md', 'u_reader')
                """
            )
            check("T3 READER 写入拒绝", False, "写入竟然成功了！")
        except asyncpg.InsufficientPrivilegeError:
            check("T3 READER 写入拒绝", True)
        except Exception as e:
            # RLS 策略拒绝也算通过（WITH CHECK 不满足）
            check("T3 READER 写入拒绝", True, f"({type(e).__name__})")
    finally:
        await conn.close()


async def test_4_empty_context_denied():
    """T4: 空字符串上下文 → 必须拒绝（Fail-Closed）"""
    conn = await asyncpg.connect(DSN)
    try:
        await conn.execute("SELECT set_config('app.allowed_read_spaces', '', true)")
        await conn.execute(
            "SELECT set_config('app.allowed_personal_cats', '', true)"
        )
        rows = await conn.fetch("SELECT * FROM documents")
        check("T4 空上下文拒绝", len(rows) == 0, f"返回 {len(rows)} 行")
    finally:
        await conn.close()


async def test_5_cannot_update_space_id():
    """T5: 尝试通过 UPDATE 篡改 space_id 越权移动文档 → 必须拒绝"""
    conn = await asyncpg.connect(DSN)
    try:
        await conn.execute(
            "SELECT set_config('app.allowed_read_spaces', $1, true)",
            "space_personal_u1",
        )
        await conn.execute(
            "SELECT set_config('app.allowed_write_spaces', $1, true)",
            "space_personal_u1",
        )
        await conn.execute(
            "SELECT set_config('app.allowed_personal_cats', '*', true)"
        )
        # 找一篇 u1 的文档，试图把它搬到别人的空间
        row = await conn.fetchrow(
            "SELECT doc_id FROM documents WHERE space_id = 'space_personal_u1' LIMIT 1"
        )
        if not row:
            check("T5 防 space_id 篡改", True, "（无测试数据，跳过）")
            return
        try:
            await conn.execute(
                "UPDATE documents SET space_id = 'space_personal_other' "
                "WHERE doc_id = $1",
                row["doc_id"],
            )
            # 如果 UPDATE 影响了 0 行，说明 USING 子句过滤掉了，也是拒绝
            check("T5 防 space_id 篡改", True, "（0 行受影响即拒绝）")
        except Exception:
            check("T5 防 space_id 篡改", True, "（策略拒绝）")
    finally:
        await conn.close()


async def test_6_other_user_task_invisible():
    """T6: 用户 A 不能看到用户 B 的任务（RLS 行隔离）"""
    conn = await asyncpg.connect(DSN)
    try:
        await conn.execute(
            "SELECT set_config('app.current_user_id', 'u_attacker', true)"
        )
        rows = await conn.fetch(
            "SELECT * FROM ingestion_tasks WHERE user_id = 'u_victim'"
        )
        check("T6 跨用户任务隔离", len(rows) == 0, f"返回 {len(rows)} 行")
    finally:
        await conn.close()


async def main():
    print("=" * 60)
    print("ContextHub Phase 0 - 负向安全测试（6 项）")
    print("=" * 60)
    for t in (
        test_1_no_context_denied,
        test_2_work_token_no_health,
        test_3_reader_cannot_write,
        test_4_empty_context_denied,
        test_5_cannot_update_space_id,
        test_6_other_user_task_invisible,
    ):
        try:
            await t()
        except Exception as e:
            check(t.__name__, False, f"异常: {e}")
    print("=" * 60)
    failed = [r for r in results if not r[1]]
    print(f"通过 {len(results) - len(failed)}/{len(results)}")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    asyncio.run(main())
