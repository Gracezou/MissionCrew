"""Webhook 触发自动化:验签、事件整理与投递入队。

外部系统(GitHub、GitCode、GitLab 或自建服务)把事件 POST 到专用 webhook 端口的
`/hooks/<项目>/<脚本短 id>`。每个开启 webhook 的脚本有自己的密钥,投递必须带
其一:HMAC-SHA256 签名头(`X-Hub-Signature-256` / `X-GitCode-Signature-256`)或
密码头(`X-GitCode-Token` / `X-Gitlab-Token` / `Authorization: Bearer`)。验签通过
的投递整理成事件文件交给脚本,由脚本自己决定处理哪些事件;平台只代答 GitHub
的 ping。HTTP 监听本身在 api/webhooks.py,这里不依赖任何 Web 框架,便于单测。
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import secrets
import time
from typing import Optional
from urllib.parse import parse_qs, quote

from ..core.models import Automation
from ..core.store import Store

# GitHub 单次投递上限 25 MiB;超出直接拒收,不读完正文
WEBHOOK_MAX_BODY = 25 * 1024 * 1024
# 签名头按顺序取第一个出现的;GitCode 的摘要可能是 hex 也可能是 base64
SIGNATURE_HEADERS = ("x-hub-signature-256", "x-gitcode-signature-256")
TOKEN_HEADERS = ("x-gitcode-token", "x-gitlab-token")
# 携带密钥的请求头不写进事件文件,脚本打印事件时不会泄露
SECRET_HEADERS = frozenset({"authorization", *TOKEN_HEADERS})
# (来源, 事件名头, 投递 id 头);按顺序识别,都没有时来源为空
PROVIDER_HEADERS = (
    ("github", "x-github-event", "x-github-delivery"),
    ("gitcode", "x-gitcode-event", "x-gitcode-delivery"),
    ("gitlab", "x-gitlab-event", "x-gitlab-event-uuid"),
)


class WebhookRejected(Exception):
    """投递被拒:status 是回给发送方的 HTTP 状态码,message 只含简短原因。"""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def new_webhook_secret() -> str:
    return secrets.token_urlsafe(32)


def webhook_path(automation: Automation) -> str:
    short = automation.id.removeprefix(f"{automation.project_id}:")
    return f"/hooks/{quote(automation.project_id, safe='')}/{quote(short, safe='')}"


def _signature_bytes(value: str) -> Optional[bytes]:
    """解析 `sha256=<hex 或 base64>`;格式不对返回 None。"""
    text = value.strip()
    if text.lower().startswith("sha256="):
        text = text[len("sha256="):]
    try:
        if len(text) == 64:
            return bytes.fromhex(text)
        decoded = base64.b64decode(text, validate=True)
        return decoded if len(decoded) == 32 else None
    except (ValueError, binascii.Error):
        return None


def verify_webhook(secret: str, headers: dict[str, str], body: bytes) -> str:
    """验签通过返回方式(hmac/token),否则抛 401。有签名头时只认签名。"""
    if not secret:
        raise WebhookRejected(401, "webhook secret not configured")
    for name in SIGNATURE_HEADERS:
        if name in headers:
            expected = hmac.new(secret.encode(), body, hashlib.sha256).digest()
            given = _signature_bytes(headers[name])
            if given is not None and hmac.compare_digest(given, expected):
                return "hmac"
            raise WebhookRejected(401, "signature mismatch")
    tokens = [headers[name] for name in TOKEN_HEADERS if name in headers]
    auth = headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        tokens.append(auth[len("bearer "):].strip())
    if tokens:
        if any(hmac.compare_digest(token.encode(), secret.encode()) for token in tokens):
            return "token"
        raise WebhookRejected(401, "token mismatch")
    raise WebhookRejected(401, "missing signature")


def webhook_event(headers: dict[str, str], body: bytes) -> dict:
    """整理成交给脚本的事件:来源、事件名、投递 id、去掉密钥的请求头与正文。

    JSON 正文与 GitHub 表单格式(`payload=<json>`)解析进 payload;其他正文
    payload 为 null,原文放 body。JSON 声明了却解析失败按坏请求拒收。
    """
    provider, event, delivery = "", "", ""
    for name, event_header, delivery_header in PROVIDER_HEADERS:
        if event_header in headers:
            provider, event = name, headers[event_header]
            delivery = headers.get(delivery_header, "")
            break
    content_type = headers.get("content-type", "").split(";")[0].strip().lower()
    text = body.decode("utf-8", "replace")
    payload = None
    try:
        if content_type == "application/json" or content_type.endswith("+json"):
            payload = json.loads(text) if text.strip() else None
        elif content_type == "application/x-www-form-urlencoded":
            form = parse_qs(text)
            if "payload" in form:
                payload = json.loads(form["payload"][0])
    except json.JSONDecodeError as exc:
        raise WebhookRejected(400, "invalid JSON body") from exc
    return {
        "provider": provider, "event": event, "delivery": delivery,
        "received_at": time.time(), "content_type": content_type,
        "headers": {key: value for key, value in headers.items()
                    if key not in SECRET_HEADERS},
        "payload": payload,
        "body": text if payload is None else None,
    }


def accept_webhook(store: Store, service, project_id: str, short_id: str,
                   headers: dict[str, str], body: bytes) -> tuple[int, dict]:
    """处理一次投递,返回 (HTTP 状态码, 响应 JSON);拒收抛 WebhookRejected。

    headers 的键须已转成小写。脚本不存在、不属于该项目或没开 webhook 一律 404,
    不区分原因。脚本正在运行时事件排队,结束后按顺序接着跑。
    """
    from .automations import normalize_automation_id

    try:
        full_id = normalize_automation_id(project_id, short_id)
    except ValueError as exc:
        raise WebhookRejected(404, "not found") from exc
    automation = store.get_automation(full_id)
    if automation is None or automation.project_id != project_id \
            or not automation.webhook:
        raise WebhookRejected(404, "not found")
    try:
        mode = verify_webhook(automation.webhook_secret, headers, body)
    except WebhookRejected as exc:
        store.audit("platform", "automation_webhook_rejected",
                    detail=f"project={project_id} automation={full_id} "
                           f"reason={exc.message}")
        raise
    event = webhook_event(headers, body)
    if event["provider"] == "github" and event["event"] == "ping":
        return 200, {"ok": True, "pong": True}
    try:
        run_id = service.trigger_webhook(automation, event)
    except ValueError as exc:   # 排队已满
        raise WebhookRejected(503, str(exc)) from exc
    store.audit("platform", "automation_webhook_accepted",
                detail=f"project={project_id} automation={full_id} auth={mode} "
                       f"event={event['provider']}:{event['event']} "
                       f"delivery={event['delivery']} run={run_id or 'queued'}")
    return 202, ({"ok": True, "run_id": run_id} if run_id is not None
                 else {"ok": True, "queued": True})
