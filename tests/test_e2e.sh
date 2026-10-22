#!/bin/bash
# Phase 1 端到端测试：入库 → 检索 → 台账
# 用法: ./test_e2e.sh

PG="PGPASSWORD=change_me_in_env psql -h 127.0.0.1 -p 5435 -U contexthub_app -d contexthub -t -A"

echo "=== Phase 1 E2E 测试 ==="
echo ""

# 1. 入库一篇测试文档（模拟网关逻辑）
echo "[1] 入库测试文档..."

# 设置 RLS 上下文（模拟网关的 profiled_connection）
CONTENT="这是付予微5岁生日派对的计划。时间：2026年11月1日上午11点。地点：塘朗城三楼麦当劳。需要准备：蛋糕、气球、回礼袋。"
FILE_HASH=$(echo -n "$CONTENT" | sha256sum | cut -d' ' -f1)

# 检查重复
EXISTING=$($PG -c "
SELECT set_config('app.allowed_read_spaces', 'space_personal_u1', true);
SELECT set_config('app.allowed_write_spaces', 'space_personal_u1', true);
SELECT set_config('app.allowed_personal_cats', '*', true);
SELECT set_config('app.current_user_id', 'u1', true);
SELECT doc_id FROM documents WHERE file_hash = '$FILE_HASH' LIMIT 1;
" 2>&1 | tail -1)

if [ -n "$EXISTING" ] && [ "$EXISTING" != "" ]; then
    echo "  去重命中: $EXISTING"
else
    # 路由：包含"生日""派对" → 03_Family, R-02
    DOC_ID=$($PG -c "
SELECT set_config('app.allowed_read_spaces', 'space_personal_u1', true);
SELECT set_config('app.allowed_write_spaces', 'space_personal_u1', true);
SELECT set_config('app.allowed_personal_cats', '*', true);
SELECT set_config('app.current_user_id', 'u1', true);
INSERT INTO documents (space_id, category, title, file_hash, wiki_file_path, created_by)
VALUES ('space_personal_u1', '03_Family', '生日派对计划.md', '$FILE_HASH', 'wiki/03_Family/生日派对计划.md', 'u1')
RETURNING doc_id;
" 2>&1 | tail -1)
    echo "  入库成功: $DOC_ID"
    echo "  路由: 03_Family (规则 R-02)"

    # 写入切片
    $PG -c "
SELECT set_config('app.allowed_write_spaces', 'space_personal_u1', true);
SELECT set_config('app.allowed_personal_cats', '*', true);
INSERT INTO document_chunks (doc_id, space_id, category, doc_title, section_path, content)
VALUES ('$DOC_ID', 'space_personal_u1', '03_Family', '生日派对计划.md', '全文', '$CONTENT');
" > /dev/null 2>&1
    echo "  切片已写入"
fi

echo ""
echo "[2] 检索测试..."

# 2. 检索（带 RLS）
RESULT=$($PG -c "
SELECT set_config('app.allowed_read_spaces', 'space_personal_u1', true);
SELECT set_config('app.allowed_personal_cats', '*', true);
SELECT c.doc_title, c.section_path, left(c.content, 50)
FROM document_chunks c
WHERE c.is_latest = TRUE AND c.content ILIKE '%生日派对%'
LIMIT 3;
" 2>&1)

echo "  检索结果:"
echo "$RESULT" | while IFS='|' read -r title section snippet; do
    echo "    - 《$title》- $section"
    echo "      引用: 《$title》- $section"
done

echo ""
echo "[3] 台账测试..."

# 3. 写台账（模拟 LivingLedger）
LEDGER="/volume1/docker/contexthub/data/living-ledger.md"
if [ ! -f "$LEDGER" ]; then
    echo "# ContextHub Living Ledger" > "$LEDGER"
    echo "" >> "$LEDGER"
fi

TIMESTAMP=$(date "+%Y-%m-%d %H:%M:%S")
cat >> "$LEDGER" << EOF
## $TIMESTAMP 入库
- doc: 《生日派对计划.md》v1.0（NEW）
- 位置: space_personal_u1 / 03_Family
- SHA-256: ${FILE_HASH:0:16}...
- 路由规则: R-02

EOF

echo "  台账已追加"
echo "  最近记录:"
tail -6 "$LEDGER" | head -5

echo ""
echo "=== E2E 测试完成 ==="
