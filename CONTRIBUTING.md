# Contributing to ContextHub

## 基本要求

- 所有 PR 必须通过 Phase 0 的 6 项负向安全测试
- 数据库变更必须附带 RLS 策略审查
- 中文 issue/PR 均可，代码注释用英文

## 开发流程

1. Fork 本仓库
2. 从 `main` 切 feature 分支：`feature/<简短描述>`
3. 本地跑 `deploy/nas/docker-compose.yml` 验证
4. 提交 PR，CI 会跑安全测试矩阵

## 安全红线（不可妥协）

- 网关层是唯一的授权边界，n8n/Worker 不得做权限判断
- 客户端只传 `doc_id`，不传文件路径
- 空授权上下文默认拒绝（Fail-Closed）
- Token 不得出现在日志、错误信息、任务回执中
