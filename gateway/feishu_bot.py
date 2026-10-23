"""
ContextHub 飞书 Bot - WebSocket 长连接模式

监听用户发给 Bot 的消息，调用 ContextHub /ask 接口问答，回复结果。
无需公网 IP，适合 NAS 环境。

环境变量：
  FEISHU_APP_ID / FEISHU_APP_SECRET - 飞书应用凭证
  GATEWAY_URL - ContextHub 网关地址（默认 http://gateway:8000）
  GATEWAY_TOKEN - 调用 /ask 的 Bearer token
"""
import json
import logging
import os
import sys

import httpx
import lark_oapi as lark
from lark_oapi.api.im.v1 import (
    ReplyMessageRequest,
    ReplyMessageRequestBody,
)
from lark_oapi.api.im.v1.model.p2_im_message_receive_v1 import P2ImMessageReceiveV1

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("feishu-bot")

APP_ID = os.environ.get("FEISHU_APP_ID", "")
APP_SECRET = os.environ.get("FEISHU_APP_SECRET", "")
GATEWAY_URL = os.environ.get("GATEWAY_URL", "http://gateway:8000").rstrip("/")
GATEWAY_TOKEN = os.environ.get("GATEWAY_TOKEN", "test-token-001")

MAX_REPLY_LEN = 4000

# 复用一个 lark Client 发回复
_lark_client: lark.Client = None

# 用户上下文：记住每个人上次问的指标（用于"此指标/这个"指代）
_user_last_metric: dict[str, str] = {}
import re as _re2
_METRIC_WORDS = ["尿酸", "血糖", "血压", "胆固醇", "甘油三酯", "肌酐",
                 "尿素", "血红蛋白", "白细胞", "血小板", "肝功能", "肾功能"]

def _resolve_reference(user_id: str, text: str) -> str:
    """把'此指标/这个/它'替换为上次问的指标"""
    if _re2.search(r"(此指标|这个指标|这个|它)", text):
        last = _user_last_metric.get(user_id, "")
        if last:
            text = _re2.sub(r"(此指标|这个指标|这个|它)", last, text)
    # 记录本次问的指标
    for w in _METRIC_WORDS:
        if w in text:
            _user_last_metric[user_id] = w
            break
    return text


def get_client() -> lark.Client:
    global _lark_client
    if _lark_client is None:
        _lark_client = lark.Client.builder().app_id(APP_ID).app_secret(APP_SECRET).build()
    return _lark_client


def ask_contexthub(question: str) -> str:
    """调用 ContextHub /ask 接口（同步）"""
    try:
        with httpx.Client(timeout=120) as client:
            resp = client.post(
                f"{GATEWAY_URL}/ask",
                json={"question": question, "limit": 5},
                headers={"Authorization": f"Bearer {GATEWAY_TOKEN}"},
            )
            resp.raise_for_status()
            data = resp.json()
    except Exception as e:
        log.error(f"/ask 调用失败: {e}")
        return f"知识库查询失败：{e}"

    answer = data.get("answer", "（无回答）")
    citations = data.get("citations", [])

    parts = [answer]
    if citations:
        parts.append("\n---\n引用来源：")
        for i, c in enumerate(citations, 1):
            parts.append(f"[{i}] {c}")

    result = "\n".join(parts)
    if len(result) > MAX_REPLY_LEN:
        result = result[:MAX_REPLY_LEN] + "\n\n（内容过长已截断）"
    return result


def do_reply(message_id: str, text: str):
    """回复消息（形成消息线程）"""
    client = get_client()
    req = (
        ReplyMessageRequest.builder()
        .message_id(message_id)
        .request_body(
            ReplyMessageRequestBody.builder()
            .content(json.dumps({"text": text}, ensure_ascii=False))
            .msg_type("text")
            .build()
        )
        .build()
    )
    resp = client.im.v1.message.reply(req)
    if not resp.success():
        log.error(f"回复失败: {resp.code} {resp.msg}")


def handle_message(data: P2ImMessageReceiveV1):
    """消息事件处理"""
    event = data.event
    if not event or not event.message:
        return
    msg = event.message
    sender = event.sender

    # 跳过机器人自己发的消息
    if sender and sender.sender_type == "2":
        return

    # 只处理文本消息
    if msg.message_type != "text":
        return

    try:
        content = json.loads(msg.content or "{}")
        text = (content.get("text") or "").strip()
    except Exception:
        return
    if not text:
        return

    # 群聊里没被 @ 就忽略
    if msg.chat_type == "group":
        if not msg.mentions:
            log.info(f"群聊未 @Bot，忽略: {text[:30]}")
            return
        for m in msg.mentions or []:
            key = getattr(m, "key", "")
            if key:
                text = text.replace(key, "").strip()

    if not text:
        return

    # 解析指代
    sender_open_id = ""
    if sender and hasattr(sender, "sender_id") and sender.sender_id:
        sender_open_id = sender.sender_id.open_id or ""
    text = _resolve_reference(sender_open_id, text)
    log.info(f"收到问题 ({msg.chat_type}): {text[:60]}")
    answer = ask_contexthub(text)
    log.info(f"回复 {len(answer)} 字")
    do_reply(msg.message_id, answer)


def main():
    if not APP_ID or not APP_SECRET:
        log.error("FEISHU_APP_ID / FEISHU_APP_SECRET 未设置")
        sys.exit(1)

    log.info(f"ContextHub 飞书 Bot 启动，网关: {GATEWAY_URL}")

    handler = (
        lark.EventDispatcherHandler.builder("", "")
        .register_p2_im_message_receive_v1(handle_message)
        .build()
    )

    ws_client = lark.ws.Client(
        APP_ID,
        APP_SECRET,
        event_handler=handler,
    )
    log.info("WebSocket 连接中，等待消息...")
    ws_client.start()


if __name__ == "__main__":
    main()
