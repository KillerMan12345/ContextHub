"""
统一模型适配层 (Model Adapter)

设计原则（对应架构图）：
- 不在 Obsidian 装模型插件，不在 n8n 每个节点直调模型
- 所有模型调用走这一层：Chat / Vision / Embedding 三个接口
- 路由规则：隐私敏感（健康档案）强制本地，其他按配置走云端
- MVP 阶段就是 Python 模块，不独立部署

Providers:
- ollama: 本地（隐私红线：健康档案的 embedding/分类必须走这里）
- bailian: 阿里云百炼（通用 chat）
- 后续可加：gemini（用户有 Pro 订阅）、openai 等
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
from typing import Optional
import os


class TaskType(Enum):
    CHAT = "chat"           # 问答、组织答案
    VISION = "vision"       # 图片理解
    EMBEDDING = "embedding"  # 向量
    CLASSIFY = "classify"   # 文档分类（路由用）


@dataclass
class ModelRequest:
    task: TaskType
    prompt: str = ""
    image_data: Optional[bytes] = None
    # 隐私标记：True 则强制本地，忽略云端配置
    privacy_sensitive: bool = False
    # 调用方：search / ingest / health
    caller: str = ""


@dataclass
class ModelResponse:
    text: str = ""
    embedding: Optional[list[float]] = None
    provider: str = ""
    model: str = ""


class ModelProvider(ABC):
    """Provider 接口：每个模型服务实现这个"""

    @abstractmethod
    def name(self) -> str: ...

    @abstractmethod
    def supports(self, task: TaskType) -> bool: ...

    @abstractmethod
    async def chat(self, prompt: str) -> str: ...

    @abstractmethod
    async def vision(self, prompt: str, image_data: bytes) -> str: ...

    @abstractmethod
    async def embed(self, text: str) -> list[float]: ...


class ModelAdapter:
    """
    统一入口。路由逻辑：
    1. privacy_sensitive=True → 强制本地 Ollama（隐私红线，不可覆盖）
    2. embedding → 优先本地 bge-m3（省钱+快）
    3. 其他 → 按 MODEL_PROVIDER 环境变量（默认 bailian）
    """

    def __init__(self):
        self._providers: dict[str, ModelProvider] = {}
        self._default_cloud = os.environ.get("MODEL_PROVIDER", "bailian")

    def register(self, provider: ModelProvider):
        self._providers[provider.name()] = provider

    def _route(self, req: ModelRequest) -> ModelProvider:
        # 红线：隐私敏感强制本地
        if req.privacy_sensitive:
            p = self._providers.get("ollama")
            if not p:
                raise RuntimeError("隐私任务需要本地 Ollama，但未注册")
            if not p.supports(req.task):
                raise RuntimeError(f"Ollama 不支持 {req.task}")
            return p

        # embedding 优先本地
        if req.task == TaskType.EMBEDDING and "ollama" in self._providers:
            p = self._providers["ollama"]
            if p.supports(TaskType.EMBEDDING):
                return p

        # 默认云端
        p = self._providers.get(self._default_cloud)
        if not p:
            raise RuntimeError(f"Provider 未注册: {self._default_cloud}")
        if not p.supports(req.task):
            # 降级到本地
            p = self._providers.get("ollama")
            if not p or not p.supports(req.task):
                raise RuntimeError(f"无可用 provider 支持 {req.task}")
        return p

    async def chat(self, prompt: str,
                   privacy_sensitive: bool = False,
                   caller: str = "") -> ModelResponse:
        req = ModelRequest(task=TaskType.CHAT, prompt=prompt,
                           privacy_sensitive=privacy_sensitive, caller=caller)
        p = self._route(req)
        text = await p.chat(prompt)
        return ModelResponse(text=text, provider=p.name(), model="")

    async def embed(self, text: str,
                    privacy_sensitive: bool = False) -> ModelResponse:
        req = ModelRequest(task=TaskType.EMBEDDING, prompt=text,
                           privacy_sensitive=privacy_sensitive)
        p = self._route(req)
        vec = await p.embed(text)
        return ModelResponse(embedding=vec, provider=p.name(), model="")

    async def classify(self, title: str, snippet: str,
                       privacy_sensitive: bool = False) -> str:
        """文档分类：返回类别 ID。规则优先，LLM 只做兜底。"""
        # 规则先行（和网关的 route_category 保持一致）
        t = (title + " " + snippet).lower()
        if any(k in t for k in ["体检", "血压", "血糖", "医院", "医生", "处方", "化验", "健康"]):
            return "04_Health_Archive"
        if any(k in t for k in ["家庭", "孩子", "女儿", "父母", "生日", "派对"]):
            return "03_Family"
        if any(k in t for k in ["生活", "购物", "旅游", "美食", "电影"]):
            return "02_Life"
        if any(k in t for k in ["工作", "项目", "会议", "代码", "文档"]):
            return "01_Work"

        # 规则没命中，LLM 兜底
        prompt = (
            f"文档标题：《{title}》\n摘要：{snippet[:200]}\n"
            "请分类，只返回以下之一：01_Work / 02_Life / 03_Family / 04_Health_Archive"
        )
        resp = await self.chat(prompt, privacy_sensitive=privacy_sensitive,
                               caller="classify")
        for cat in ["01_Work", "02_Life", "03_Family", "04_Health_Archive"]:
            if cat in resp.text:
                return cat
        return "01_Work"  # 兜底


# 全局单例
_adapter: Optional[ModelAdapter] = None


def get_adapter() -> ModelAdapter:
    global _adapter
    if _adapter is None:
        _adapter = ModelAdapter()
        # 自动注册已配置的 providers
        if os.environ.get("OLLAMA_HOST"):
            from providers.ollama_provider import OllamaProvider
            _adapter.register(OllamaProvider())
        if os.environ.get("BAILIAN_API_KEY"):
            from providers.bailian_provider import BailianProvider
            _adapter.register(BailianProvider())
    return _adapter
