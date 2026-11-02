"""Webhook 触发自动化:验签、事件整理与投递入队。

外部系统(GitHub、GitCode、GitLab 或自建服务)把事件 POST 到专用 webhook 端口的
`/hooks/<项目>/<脚本短 id>`。每个开启 webhook 的脚本有自己的密钥,投递至少带一种
用它验得过的凭据:GitHub 风格 HMAC-SHA256 签名头(`X-Hub-Signature-256` /
`X-GitCode-Signature-256`)、Standard Webhooks 签名(`webhook-id` / `webhook-timestamp`
/ `webhook-signature`,svix-* 同义)或密码头(`X-GitCode-Token` / `X-Gitlab-Token` /
`Authorization: Bearer`)。验签通过的投递整理成事件文件交给脚本,由脚本自己决定
处理哪些事件;平台只代答 GitHub 的 ping。HTTP 监听本身在 api/webhooks.py,这里
不依赖任何 Web 框架,便于单测。
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
# GitHub 风格签名头:对原始正文做 HMAC-SHA256;GitCode 的摘要可能是 hex 也可能是 base64
SIGNATURE_HEADERS = ("x-hub-signature-256", "x-gitcode-signature-256")
TOKEN_HEADERS = ("x-gitcode-token", "x-gitlab-token")
# Standard Webhooks(standardwebhooks.com)的 (id, 时间戳, 签名) 三个头;
# svix-* 是同一规范的原名(Svix 及基于它的服务仍在用)
STANDARD_HEADER_SETS = (
    ("webhook-id", "webhook-timestamp", "webhook-signature"),
    ("svix-id", "svix-timestamp", "svix-signature"),
)
# 规范建议的时间戳容差:超出即视为重放
STANDARD_TOLERANCE_SECONDS = 5 * 60
STANDARD_SECRET_PREFIX = "whsec_"
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
    """Standard Webhooks 格式的密钥(`whsec_` + 32 字节 base64):规范的签名库可以
    直接使用;GitHub 风格签名与密码方式把整串当作密钥原文,同样适用。"""
    return STANDARD_SECRET_PREFIX + base64.b64encode(secrets.token_bytes(32)).decode()


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


def _standard_keys(secret: str) -> list[bytes]:
    """Standard Webhooks 用 `whsec_<base64>` 解码后的字节作 HMAC 密钥;其他格式的
    密钥(含旧版生成的)按原文字节,对应规范签名库的 raw 格式。"""
    keys = [secret.encode()]
    if secret.startswith(STANDARD_SECRET_PREFIX):
        try:
            keys.insert(0, base64.b64decode(
                secret[len(STANDARD_SECRET_PREFIX):], validate=True))
        except (ValueError, binascii.Error):
            pass
    return keys


def _verify_standard(secret: str, headers: dict[str, str], body: bytes,
                     now: float) -> Optional[str]:
    """验 Standard Webhooks 签名:验过返回 None,否则返回失败原因。

    签名内容是 `<id>.<时间戳>.<正文>`;签名头可带多个以空格分隔的 `v1,<base64>`
    (密钥轮换期间),任一匹配即可。
    """
    id_name, ts_name, sig_name = next(
        names for names in STANDARD_HEADER_SETS if names[2] in headers)
    timestamp = headers.get(ts_name, "").strip()
    try:
        if abs(now - int(timestamp)) > STANDARD_TOLERANCE_SECONDS:
            return "timestamp outside tolerance"
    except ValueError:
        return "invalid timestamp"
    signed = f"{headers.get(id_name, '').strip()}.{timestamp}.".encode() + body
    given = []
    for item in headers[sig_name].split():
        version, _, value = item.partition(",")
        if version == "v1":
            try:
                given.append(base64.b64decode(value, validate=True))
            except (ValueError, binascii.Error):
                continue
    for key in _standard_keys(secret):
        expected = hmac.new(key, signed, hashlib.sha256).digest()
        if any(hmac.compare_digest(item, expected) for item in given):
            return None
    return "signature mismatch"


def verify_webhook(secret: str, headers: dict[str, str], body: bytes,
                   now: Optional[float] = None) -> str:
    """带的凭据里任一种验过即通过,返回方式(hmac/standard/token);都没验过抛 401。

    不能按第一个出现的签名头定论:转发服务会原样保留上游平台的签名头(那是用
    上游的密钥签的,这里验不过),同时用本脚本的密钥另签一份或带上密码头。每种
    凭据都要知道密钥才能伪造,逐个验证不降低安全性。
    """
    if not secret:
        raise WebhookRejected(401, "webhook secret not configured")
    failures = []
    expected = hmac.new(secret.encode(), body, hashlib.sha256).digest()
    for name in SIGNATURE_HEADERS:
        if name in headers:
            given = _signature_bytes(headers[name])
            if given is not None and hmac.compare_digest(given, expected):
                return "hmac"
            failures.append("signature mismatch")
    if any(names[2] in headers for names in STANDARD_HEADER_SETS):
        reason = _verify_standard(secret, headers, body,
                                  time.time() if now is None else now)
        if reason is None:
            return "standard"
        failures.append(reason)
    tokens = [headers[name] for name in TOKEN_HEADERS if name in headers]
    auth = headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        tokens.append(auth[len("bearer "):].strip())
    if tokens:
        if any(hmac.compare_digest(token.encode(), secret.encode()) for token in tokens):
            return "token"
        failures.append("token mismatch")
    # 失败原因去重后按出现顺序回给发送方,便于对照自己发了哪些凭据
    raise WebhookRejected(401, "; ".join(dict.fromkeys(failures)) or "missing signature")


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
    # Standard Webhooks 的发送方没有平台事件头,投递 id 取 webhook-id
    delivery = delivery or headers.get("webhook-id") or headers.get("svix-id") or ""
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
