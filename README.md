# ContextHub

AI-native auditable knowledge hub: compile scattered documents into versioned, source-traced, permission-aware knowledge.

> 个人 NAS 自我实践 → 部门推广的开源项目。

## 5 分钟上手

```bash
git clone https://github.com/KillerMan12345/ContextHub.git
cd ContextHub
# Phase 0 进行中，完整部署指南见 deploy/nas/
```

## 这是什么

把你丢进来的文档（PDF、Word、图片、聊天记录）自动变成**有来源、有版本、有权限**的知识库：

- **自动归类**：丢进去就行，系统判断放哪；重复的自动去重，新版的链到旧版
- **来源可溯**：每条知识都能点回原件的第几章第几页
- **权限隔离**：工作、家庭、健康档案物理隔离，不会串台
- **任意 Agent 可查**：标准 MCP 接口，Claude Code、Cursor、飞书 Bot 都能接

## 架构

详见 [ARCHITECTURE.md](./ARCHITECTURE.md)。一句话：

```
你丢文档 → 网关(授权) → 入库流水线 → PostgreSQL + Markdown Wiki → MCP 对外
```

设计原则：**先可信、后自动；统一底座、分期交付；权限先于检索**。

## 目录结构

```
gateway/    FastAPI + FastMCP 权限网关（唯一对外入口）
worker/     n8n 工作流：入库状态机、健康提取、每周 Lint
web/        Web UI（Phase 2，部门推广用）
docs/       文档
deploy/
  nas/      NAS Docker Compose 部署（dogfood 环境）
```

## 路线图

| 阶段 | 内容 | 状态 |
|---|---|---|
| Phase 0 | PostgreSQL + RLS + 网关 PoC，6 项安全测试 | 🔨 进行中 |
| Phase 1 | 个人闭环：入库/版本/检索/MCP/IDE 感知 | 📋 待开始 |
| Phase 2 | 家庭空间 + 健康档案 + Web UI | 📋 待开始 |
| Phase 3 | 部门组织树（按需） | 📋 待开始 |

## License

Apache-2.0
