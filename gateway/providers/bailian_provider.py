"""
阿里云百炼 Provider（OpenAI 兼容接口）
- 通用 chat/vision 走这里
- API Key 从环境变量 BAILIAN_API_KEY 读
"""

import os
import httpx
from model_adapter import ModelProvider, TaskType


class BailianProvider(ModelProvider):
    def __init__(self,
                 api_key: str = "",
                 base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1",
                 chat_model: str = "qwen-plus"):
        self.api_key = api_key or os.environ.get("BAILIAN_API_KEY", "")
        self.base_url = os.environ.get("BAILIAN_BASE_URL", base_url)
        self.chat_model = os.environ.get("BAILIAN_CHAT_MODEL", chat_model)

    def name(self) -> str:
        return "bailian"

    def supports(self, task: TaskType) -> bool:
        return task in (TaskType.CHAT, TaskType.VISION, TaskType.CLASSIFY,
                       TaskType.EMBED)

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json"}

    async def chat(self, prompt: str) -> str:
        if not self.api_key:
            raise RuntimeError("BAILIAN_API_KEY 未设置")
        async with httpx.AsyncClient(timeout=120) as client:
            r = await client.post(
                f"{self.base_url}/chat/completions",
                headers=self._headers(),
                json={"model": self.chat_model,
                      "messages": [{"role": "user", "content": prompt}]},
            )
            r.raise_for_status()
            return r.json()["choices"][0]["message"]["content"]

    async def vision(self, prompt: str, image_data: bytes) -> str:
        # 图片转 base64，走 qwen-vl
        import base64
        b64 = base64.b64encode(image_data).decode()
        async with httpx.AsyncClient(timeout=120) as client:
            r = await client.post(
                f"{self.base_url}/chat/completions",
                headers=self._headers(),
                json={"model": "qwen3-vl-plus",
                      "messages": [{
                          "role": "user",
                          "content": [
                              {"type": "text", "text": prompt},
                              {"type": "image_url",
                               "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
                          ]}]},
            )
            r.raise_for_status()
            return r.json()["choices"][0]["message"]["content"]

    async def embed(self, text: str) -> list[float]:
        """百炼 text-embedding-v4，1024 维。"""
        if not self.api_key:
            raise RuntimeError("BAILIAN_API_KEY 未设置")
        async with httpx.AsyncClient(timeout=60) as client:
            r = await client.post(
                f"{self.base_url}/embeddings",
                headers=self._headers(),
                json={"model": "text-embedding-v4",
                      "input": text[:2048]},
            )
            r.raise_for_status()
            return r.json()["data"][0]["embedding"]
