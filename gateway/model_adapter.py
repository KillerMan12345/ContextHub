"""
统一模型适配层 (Model Adapter) - 修订版

变更（2026-10-22）：
- 去掉 Ollama：NAS 内存吃紧（20+ 容器，可用 6.7G），跑不动本地大模型
- 隐私红线改走纯规则：健康档案不调任何模型，只做规则分类+关键词提取
- 通用任务走百炼云端

Providers:
- bailian: 阿里云百炼（chat/vision）
- rule: 本地规则引擎（分类、关键词，不调模型）
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
from typing import Optional
import os
import re


class TaskType(Enum):
    CHAT = "chat"
    VISION = "vision"
    EMBEDDING = "embedding"
    EMBED = "embedding"
    CLASSIFY = "classify"


@dataclass
class ModelRequest:
    task: TaskType
    prompt: str = ""
    image_data: Optional[bytes] = None
    privacy_sensitive: bool = False
    caller: str = ""


@dataclass
class ModelResponse:
    text: str = ""
    embedding: Optional[list[float]] = None
    provider: str = ""
    model: str = ""


class ModelProvider(ABC):
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


class RuleProvider(ModelProvider):
    """
    纯规则引擎，不调模型。
    用于隐私敏感任务：只做分类和关键词提取，数据不出网关。
    """

    def name(self) -> str:
        return "rule"

    def supports(self, task: TaskType) -> bool:
        return task in (TaskType.CLASSIFY,)

    async def chat(self, prompt: str) -> str:
        raise NotImplementedError("隐私任务不做 LLM 问答，只返回检索原文")

    async def vision(self, prompt: str, image_data: bytes) -> str:
        raise NotImplementedError("隐私任务不支持图片理解")

    async def embed(self, text: str) -> list[float]:
        raise NotImplementedError("NAS 跑不动本地 embedding，暂不做向量")

    def classify(self, title: str, snippet: str) -> str:
        t = (title + " " + snippet).lower()
        if any(k in t for k in ["体检", "血压", "血糖", "医院", "医生",
                                 "处方", "化验", "健康", "病历"]):
            return "04_Health_Archive"
        if any(k in t for k in ["家庭", "孩子", "女儿", "父母",
                                 "生日", "派对"]):
            return "03_Family"
        if any(k in t for k in ["生活", "购物", "旅游", "美食", "电影"]):
            return "02_Life"
        return "01_Work"

    def extract_keywords(self, text: str, top_k: int = 10) -> list[str]:
        """简单关键词提取：按词频（去停用词）"""
        stop = set("的了是在有和与或但而于对就都也还不没很最更以从到把被让给"
                   "个种些这么那么这样那样这里那里这个那个这些那些")
        words = re.findall(r'[\u4e00-\u9fff]{2,}', text)
        freq: dict[str, int] = {}
        for w in words:
            if w not in stop and len(w) >= 2:
                freq[w] = freq.get(w, 0) + 1
        return sorted(freq, key=freq.get, reverse=True)[:top_k]


class ModelAdapter:
    """
    路由逻辑（修订版）：
    1. privacy_sensitive=True → 走 rule（纯本地规则，不调模型）
    2. embedding → 暂不支持（NAS 跑不动）
    3. 其他 → 百炼
    """

    def __init__(self):
        self._providers: dict[str, ModelProvider] = {}
        self._rule = RuleProvider()
        self._default_cloud = os.environ.get("MODEL_PROVIDER", "bailian")

    def register(self, provider: ModelProvider):
        self._providers[provider.name()] = provider

    @property
    def rule(self) -> RuleProvider:
        return self._rule

    def _route(self, req: ModelRequest) -> ModelProvider:
        # 隐私敏感：纯规则，不调模型
        if req.privacy_sensitive:
            return self._rule

        if req.task == TaskType.EMBEDDING:
            raise RuntimeError("向量功能暂未启用（NAS 跑不动本地模型）")

        p = self._providers.get(self._default_cloud)
        if not p:
            raise RuntimeError(f"Provider 未注册: {self._default_cloud}")
        return p

    async def chat(self, prompt: str,
                   privacy_sensitive: bool = False,
                   caller: str = "") -> ModelResponse:
        req = ModelRequest(task=TaskType.CHAT, prompt=prompt,
                           privacy_sensitive=privacy_sensitive, caller=caller)
        p = self._route(req)
        text = await p.chat(prompt)
        return ModelResponse(text=text, provider=p.name(), model="")

    async def classify(self, title: str, snippet: str,
                       privacy_sensitive: bool = False) -> str:
        # 分类永远先走规则
        result = self._rule.classify(title, snippet)
        if result != "01_Work":
            return result  # 规则命中，直接返回

        # 规则没命中且非隐私 → LLM 兜底
        if privacy_sensitive:
            return "01_Work"

        prompt = (f"文档标题：《{title}》\n摘要：{snippet[:200]}\n"
                  "请分类，只返回以下之一："
                  "01_Work / 02_Life / 03_Family / 04_Health_Archive")
        try:
            resp = await self.chat(prompt, caller="classify")
            for cat in ["01_Work", "02_Life", "03_Family", "04_Health_Archive"]:
                if cat in resp.text:
                    return cat
        except RuntimeError:
            pass
        return "01_Work"


_adapter: Optional[ModelAdapter] = None


def get_adapter() -> ModelAdapter:
    global _adapter
    if _adapter is None:
        _adapter = ModelAdapter()
        if os.environ.get("BAILIAN_API_KEY"):
            from providers.bailian_provider import BailianProvider
            _adapter.register(BailianProvider())
    return _adapter

_provider = None

def get_provider():
    """直接返回可用的 provider（用于 embedding 等绕过 adapter 路由的场景）"""
    global _provider
    if _provider is None:
        if os.environ.get("BAILIAN_API_KEY"):
            from providers.bailian_provider import BailianProvider
            _provider = BailianProvider()
    return _provider
