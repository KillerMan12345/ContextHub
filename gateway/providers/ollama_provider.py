"""
Ollama 本地 Provider
- 隐私红线：健康档案的 embedding/分类/问答必须走这里，不出内网
- embedding 用 bge-m3，chat 用 qwen2.5（按需换）
"""

import os
import httpx
from model_adapter import ModelProvider, TaskType


class OllamaProvider(ModelProvider):
    def __init__(self,
                 host: str = "",
                 chat_model: str = "qwen2.5:7b",
                 embed_model: str = "bge-m3"):
        self.host = host or os.environ.get("OLLAMA_HOST", "http://localhost:11434")
        self.chat_model = os.environ.get("OLLAMA_CHAT_MODEL", chat_model)
        self.embed_model = os.environ.get("OLLAMA_EMBED_MODEL", embed_model)

    def name(self) -> str:
        return "ollama"

    def supports(self, task: TaskType) -> bool:
        return task in (TaskType.CHAT, TaskType.EMBEDDING, TaskType.CLASSIFY)

    async def chat(self, prompt: str) -> str:
        async with httpx.AsyncClient(timeout=120) as client:
            r = await client.post(
                f"{self.host}/api/generate",
                json={"model": self.chat_model, "prompt": prompt,
                      "stream": False},
            )
            r.raise_for_status()
            return r.json().get("response", "")

    async def vision(self, prompt: str, image_data: bytes) -> str:
        raise NotImplementedError("Ollama vision 按需接 llava")

    async def embed(self, text: str) -> list[float]:
        async with httpx.AsyncClient(timeout=60) as client:
            r = await client.post(
                f"{self.host}/api/embeddings",
                json={"model": self.embed_model, "prompt": text},
            )
            r.raise_for_status()
            return r.json().get("embedding", [])
