"""把签到结果推送到常用的消息服务。

所有渠道都是可选的：只要配置了对应的环境变量，签到结束后就会自动推送；
没有配置任何渠道时脚本仍然只输出到日志，不会发起任何请求。

已支持的渠道（括号内为环境变量）：

- 企业微信（群机器人）：``WECOM_WEBHOOK``
- 飞书（自定义机器人）：``FEISHU_WEBHOOK``，可选 ``FEISHU_SECRET`` 加签
- 钉钉（自定义机器人）：``DINGTALK_WEBHOOK``，可选 ``DINGTALK_SECRET`` 加签
- 群晖 Chat（传入 Webhook）：``SYNOLOGY_CHAT_URL``
- Server 酱：``SERVERCHAN_SENDKEY``
- PushPlus：``PUSHPLUS_TOKEN``
- Bark：``BARK_URL``（形如 ``https://api.day.app/设备Key``）或 ``BARK_KEY`` + ``BARK_SERVER``
- Telegram：``TELEGRAM_BOT_TOKEN`` + ``TELEGRAM_CHAT_ID``
- ntfy：``NTFY_TOPIC``，可选 ``NTFY_SERVER``、``NTFY_TOKEN``
- Gotify：``GOTIFY_URL`` + ``GOTIFY_TOKEN``
- 通用 Webhook：``GENERIC_WEBHOOK``，可选 ``GENERIC_WEBHOOK_HEADERS``

通用开关：

- ``NOTIFY_ENABLED``：设为 ``false`` 时禁用全部推送。
- ``NOTIFY_TIMEOUT``：单次请求超时秒数，默认 10。

Webhook 类渠道的变量可以填多个地址（每行一个），实现一次签到推送到多个群。

推送失败不会抛出异常，也不会影响签到脚本的退出码，只会在日志中以
``⚠️`` 开头输出，且日志中不会出现完整的 Webhook 地址或密钥。
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from typing import Callable, Mapping, Sequence
from urllib.parse import parse_qs, quote, quote_plus, urlsplit, urlunsplit

import requests


DEFAULT_TIMEOUT = 10.0
NOTIFY_GROUP = "夸克网盘签到"
BARK_DEFAULT_SERVER = "https://api.day.app"
_FALSE_VALUES = {"0", "false", "no", "off", "disable", "disabled"}
_SERVERCHAN_TURBO = re.compile(r"^sctp(\d+)t(.+)$")
_MIN_SECRET_LENGTH = 6
_PATH_TOKEN = re.compile(r"^[A-Za-z0-9_\-:.]{12,}$")


class NotifyError(RuntimeError):
    """单个渠道推送失败，消息已经过脱敏处理，可安全写入日志。"""


# --------------------------------------------------------------- 日志脱敏 ----
# 所有会出现在 GitHub Actions 日志里的文本都会经过 :func:`redact`：
# 渠道地址、Token 以及账号凭据会被登记在这里，一旦被第三方接口回显就会
# 被替换成 ``***``，即使某个错误分支将来意外拼上了密钥也不会泄露。

_SECRETS: list[str] = []


def register_secret(value: str | None) -> None:
    """登记一个绝不允许出现在日志或推送内容里的字符串。"""

    secret = (value or "").strip()
    if len(secret) < _MIN_SECRET_LENGTH or secret in _SECRETS:
        return
    _SECRETS.append(secret)


def reset_secrets() -> None:
    """清空已登记的敏感串（主要供测试使用）。"""

    _SECRETS.clear()


def registered_secret_count() -> int:
    """已登记的敏感串数量，便于排查脱敏是否生效。"""

    return len(_SECRETS)


def redact(text: str) -> str:
    """把已登记的敏感串替换成 ``***``，返回可以安全输出的文本。"""

    if not text or not _SECRETS:
        return text
    for secret in _SECRETS:
        if secret in text:
            text = text.replace(secret, "***")
    return text


def register_env_secrets(env: Mapping[str, str] | None = None) -> None:
    """把推送相关环境变量里的完整地址、查询参数和 Token 登记为敏感串。"""

    resolved = _resolve_env(env)
    for name in ENV_KEYS:
        for line in re.split(r"[\r\n]+", resolved.get(name) or ""):
            line = line.strip()
            if not line:
                continue
            register_secret(line)
            if "//" not in line:
                continue
            parts = urlsplit(line)
            for values in parse_qs(parts.query).values():
                for item in values:
                    register_secret(item)
            for segment in parts.path.split("/"):
                if _PATH_TOKEN.match(segment):
                    register_secret(segment)
    headers = (resolved.get("GENERIC_WEBHOOK_HEADERS") or "").strip()
    if headers:
        try:
            parsed = json.loads(headers)
        except ValueError:
            parsed = None
        if isinstance(parsed, dict):
            for value in parsed.values():
                text = str(value).strip()
                register_secret(text)
                if " " in text:
                    register_secret(text.split()[-1])


@dataclass(frozen=True)
class Message:
    """一条待推送的消息。"""

    title: str
    content: str

    def text(self) -> str:
        if not self.title:
            return self.content
        if not self.content:
            return self.title
        return f"{self.title}\n{self.content}"


@dataclass(frozen=True)
class PushResult:
    """一次推送的结果汇总。"""

    sent: tuple[str, ...] = ()
    failures: tuple[str, ...] = ()


def host_of(url: str) -> str:
    """返回 URL 的主机名，用于日志中安全地标识渠道目标。"""

    try:
        return urlsplit(url).netloc
    except ValueError:
        return ""


def truncate(text: str, limit: int) -> str:
    """按 UTF-8 字节数截断文本，避免超出渠道的单条消息上限。"""

    if limit <= 0:
        return text
    encoded = text.encode("utf-8")
    if len(encoded) <= limit:
        return text
    suffix = "\n…（内容过长，已截断）"
    budget = limit - len(suffix.encode("utf-8"))
    if budget <= 0:
        return suffix.strip()
    return encoded[:budget].decode("utf-8", "ignore") + suffix


def _http_detail(exc: Exception) -> str:
    status = getattr(getattr(exc, "response", None), "status_code", None)
    if status:
        return f"请求失败（HTTP {status}）"
    return f"请求失败（{type(exc).__name__}）"


def _api_message(payload: Mapping[str, object], fallback: str = "未知错误") -> str:
    for key in ("errmsg", "msg", "message", "error", "description", "StatusMessage"):
        value = payload.get(key)
        if value:
            return str(value)
    return fallback


def _rfc2047(text: str) -> str:
    """把非 ASCII 文本编码成 HTTP 头可以安全传输的形式。"""

    if text.isascii():
        return text
    token = base64.b64encode(text.encode("utf-8")).decode("ascii")
    return f"=?UTF-8?B?{token}?="


def _positive_float(value: str | None, fallback: float) -> float:
    try:
        parsed = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return fallback
    return parsed if parsed > 0 else fallback


def _is_false(value: str | None) -> bool:
    return (value or "").strip().lower() in _FALSE_VALUES


def relax_console_encoding() -> None:
    """让 Windows(GBK) 控制台打印 emoji 时降级为替代字符，而不是直接报错。

    Linux / GitHub Actions 的 UTF-8 输出不受影响。
    """

    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(errors="replace")
        except (ValueError, OSError):
            pass


class Notifier:
    """单个推送目标。"""

    name = "notifier"
    label = "消息推送"
    max_bytes = 0

    def __init__(
        self,
        session: requests.Session | None = None,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self.session = session if session is not None else requests.Session()
        self.timeout = _positive_float(str(timeout), DEFAULT_TIMEOUT)
        self.endpoint = ""

    @property
    def target(self) -> str:
        return host_of(self.endpoint)

    def send(self, message: Message) -> None:
        raise NotImplementedError

    def render(self, message: Message) -> str:
        return truncate(message.text(), self.max_bytes)

    def fail(self, detail: str) -> NotifyError:
        prefix = f"{self.label}（{self.target}）" if self.target else self.label
        return NotifyError(f"{prefix}{detail}")

    def _post(self, url: str, **kwargs) -> dict:
        try:
            response = self.session.request(
                "POST", url, timeout=self.timeout, **kwargs
            )
            response.raise_for_status()
        except requests.Timeout as exc:
            raise self.fail("请求超时") from exc
        except requests.RequestException as exc:
            raise self.fail(_http_detail(exc)) from exc
        try:
            payload = response.json()
        except ValueError:
            return {}
        return payload if isinstance(payload, dict) else {}

    def _require_ok(self, payload: Mapping[str, object], code: object) -> None:
        if code in (0, None, "0"):
            return
        raise self.fail(f"返回错误：{_api_message(payload, f'code={code}')}")


class WecomNotifier(Notifier):
    """企业微信群机器人。"""

    name = "wecom"
    label = "企业微信"
    max_bytes = 4000

    def __init__(self, webhook: str, **kwargs) -> None:
        super().__init__(**kwargs)
        self.endpoint = webhook

    def send(self, message: Message) -> None:
        payload = self._post(
            self.endpoint,
            json={
                "msgtype": "markdown",
                "markdown": {"content": self.render(message)},
            },
        )
        self._require_ok(payload, payload.get("errcode"))


class FeishuNotifier(Notifier):
    """飞书自定义机器人。"""

    name = "feishu"
    label = "飞书"
    max_bytes = 20000

    def __init__(self, webhook: str, secret: str = "", **kwargs) -> None:
        super().__init__(**kwargs)
        self.endpoint = webhook
        self.secret = secret.strip()

    @staticmethod
    def sign(timestamp: str, secret: str) -> str:
        raw = f"{timestamp}\n{secret}".encode("utf-8")
        digest = hmac.new(raw, digestmod=hashlib.sha256).digest()
        return base64.b64encode(digest).decode("utf-8")

    def send(self, message: Message) -> None:
        body: dict[str, object] = {
            "msg_type": "text",
            "content": {"text": self.render(message)},
        }
        if self.secret:
            timestamp = str(int(time.time()))
            body["timestamp"] = timestamp
            body["sign"] = self.sign(timestamp, self.secret)
        payload = self._post(self.endpoint, json=body)
        code = payload.get("code", payload.get("StatusCode"))
        self._require_ok(payload, code)


class DingTalkNotifier(Notifier):
    """钉钉自定义机器人。"""

    name = "dingtalk"
    label = "钉钉"
    max_bytes = 18000

    def __init__(self, webhook: str, secret: str = "", **kwargs) -> None:
        super().__init__(**kwargs)
        self.endpoint = dingtalk_endpoint(webhook, secret)

    def send(self, message: Message) -> None:
        payload = self._post(
            self.endpoint,
            json={
                "msgtype": "markdown",
                "markdown": {
                    "title": message.title or NOTIFY_GROUP,
                    "text": self.render(message),
                },
            },
        )
        self._require_ok(payload, payload.get("errcode"))


def dingtalk_endpoint(webhook: str, secret: str = "") -> str:
    """按钉钉要求把加签参数拼到 Webhook 地址上。"""

    url = webhook.strip()
    secret = secret.strip()
    if not secret:
        return url
    timestamp = str(round(time.time() * 1000))
    raw = f"{timestamp}\n{secret}".encode("utf-8")
    digest = hmac.new(secret.encode("utf-8"), raw, digestmod=hashlib.sha256).digest()
    sign = quote_plus(base64.b64encode(digest).decode("utf-8"))
    separator = "&" if "?" in url else "?"
    return f"{url}{separator}timestamp={timestamp}&sign={sign}"


class SynologyChatNotifier(Notifier):
    """群晖 Chat 传入 Webhook。"""

    name = "synologychat"
    label = "群晖 Chat"
    max_bytes = 4000

    def __init__(self, webhook: str, **kwargs) -> None:
        super().__init__(**kwargs)
        self.endpoint = webhook

    def send(self, message: Message) -> None:
        body = json.dumps({"text": self.render(message)}, ensure_ascii=False)
        payload = self._post(
            self.endpoint,
            data={"payload": body},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        if payload.get("success") is False or payload.get("error"):
            raise self.fail(f"返回错误：{_api_message(payload)}")


class ServerChanNotifier(Notifier):
    """Server 酱（同时兼容 Server 酱³ 的 sctp 密钥）。"""

    name = "serverchan"
    label = "Server酱"
    max_bytes = 20000

    def __init__(self, sendkey: str, **kwargs) -> None:
        super().__init__(**kwargs)
        self.endpoint = serverchan_endpoint(sendkey)

    def send(self, message: Message) -> None:
        payload = self._post(
            self.endpoint,
            data={
                "title": message.title or NOTIFY_GROUP,
                "desp": truncate(message.content, self.max_bytes),
            },
        )
        self._require_ok(payload, payload.get("code"))


def serverchan_endpoint(sendkey: str) -> str:
    key = sendkey.strip()
    match = _SERVERCHAN_TURBO.match(key)
    if match:
        return f"https://{match.group(1)}.push.ft07.com/send/{key}.send"
    return f"https://sctapi.ftqq.com/{key}.send"


class PushPlusNotifier(Notifier):
    """PushPlus 微信推送。"""

    name = "pushplus"
    label = "PushPlus"
    max_bytes = 20000
    push_url = "https://www.pushplus.plus/send"

    def __init__(self, token: str, **kwargs) -> None:
        super().__init__(**kwargs)
        self.token = token.strip()
        self.endpoint = self.push_url

    def send(self, message: Message) -> None:
        payload = self._post(
            self.endpoint,
            json={
                "token": self.token,
                "title": message.title or NOTIFY_GROUP,
                "content": self.render(message),
                "template": "markdown",
            },
        )
        code = payload.get("code")
        if code in (None, 200, "200"):
            return
        raise self.fail(f"返回错误：{_api_message(payload, f'code={code}')}")


class BarkNotifier(Notifier):
    """Bark（iOS 推送）。"""

    name = "bark"
    label = "Bark"
    max_bytes = 3000

    def __init__(self, server: str, key: str = "", **kwargs) -> None:
        super().__init__(**kwargs)
        self.endpoint, self.device_key = bark_endpoint(server, key)

    def send(self, message: Message) -> None:
        if not self.device_key:
            raise self.fail("缺少设备 Key，请填写 BARK_URL 或 BARK_KEY")
        payload = self._post(
            self.endpoint,
            json={
                "device_key": self.device_key,
                "title": message.title or NOTIFY_GROUP,
                "body": truncate(message.content, self.max_bytes),
                "group": NOTIFY_GROUP,
            },
        )
        if payload.get("code") not in (None, 200, "200"):
            raise self.fail(f"返回错误：{_api_message(payload)}")


def bark_endpoint(server: str, key: str = "") -> tuple[str, str]:
    """从 Bark 地址中拆出推送接口和设备 Key。"""

    url = server.strip().rstrip("/")
    if not url:
        url = BARK_DEFAULT_SERVER
    parts = urlsplit(url)
    segments = [segment for segment in parts.path.split("/") if segment]
    if segments and segments[-1] == "push":
        return url, key.strip()
    if segments and not key.strip():
        key = segments[-1]
        segments = segments[:-1]
    path = "/" + "/".join([*segments, "push"])
    return urlunsplit((parts.scheme, parts.netloc, path, "", "")), key.strip()


class TelegramNotifier(Notifier):
    """Telegram 机器人。"""

    name = "telegram"
    label = "Telegram"
    max_bytes = 3500

    def __init__(self, token: str, chat_id: str, **kwargs) -> None:
        super().__init__(**kwargs)
        self.token = token.strip()
        self.chat_id = chat_id.strip()
        self.endpoint = f"https://api.telegram.org/bot{self.token}/sendMessage"

    def send(self, message: Message) -> None:
        payload = self._post(
            self.endpoint,
            json={
                "chat_id": self.chat_id,
                "text": self.render(message),
                "disable_web_page_preview": True,
            },
        )
        if payload and payload.get("ok") is not True:
            raise self.fail(f"返回错误：{_api_message(payload)}")


class NtfyNotifier(Notifier):
    """ntfy 推送。"""

    name = "ntfy"
    label = "ntfy"
    max_bytes = 3500

    def __init__(
        self, server: str, topic: str, token: str = "", **kwargs
    ) -> None:
        super().__init__(**kwargs)
        self.topic = topic.strip()
        self.token = token.strip()
        self.endpoint = f"{server.strip().rstrip('/') or 'https://ntfy.sh'}/{self.topic}"

    def send(self, message: Message) -> None:
        headers = {
            "Title": _rfc2047(message.title or NOTIFY_GROUP),
            "Content-Type": "text/plain; charset=utf-8",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        payload = self._post(
            self.endpoint,
            data=self.render(message).encode("utf-8"),
            headers=headers,
        )
        if payload.get("error"):
            raise self.fail(f"返回错误：{_api_message(payload)}")


class GotifyNotifier(Notifier):
    """Gotify 推送。"""

    name = "gotify"
    label = "Gotify"
    max_bytes = 0

    def __init__(self, server: str, token: str, **kwargs) -> None:
        super().__init__(**kwargs)
        self.token = token.strip()
        base = server.strip().rstrip("/")
        self.endpoint = f"{base}/message?token={quote(self.token, safe='')}"

    def send(self, message: Message) -> None:
        payload = self._post(
            self.endpoint,
            json={
                "title": message.title or NOTIFY_GROUP,
                "message": self.render(message),
                "priority": 0,
            },
        )
        if payload.get("error") or payload.get("errorCode"):
            raise self.fail(f"返回错误：{_api_message(payload)}")


class GenericWebhookNotifier(Notifier):
    """通用 Webhook：以 JSON 方式 POST 标题和正文。"""

    name = "webhook"
    label = "通用Webhook"
    max_bytes = 0

    def __init__(self, webhook: str, headers: str = "", **kwargs) -> None:
        super().__init__(**kwargs)
        self.endpoint = webhook
        self.raw_headers = headers.strip()

    def extra_headers(self) -> dict[str, str]:
        if not self.raw_headers:
            return {}
        try:
            parsed = json.loads(self.raw_headers)
        except ValueError as exc:
            raise self.fail("GENERIC_WEBHOOK_HEADERS 不是合法的 JSON") from exc
        if not isinstance(parsed, dict):
            raise self.fail("GENERIC_WEBHOOK_HEADERS 必须是 JSON 对象")
        return {str(key): str(value) for key, value in parsed.items()}

    def send(self, message: Message) -> None:
        self._post(
            self.endpoint,
            json={
                "title": message.title,
                "content": message.content,
                "source": NOTIFY_GROUP,
            },
            headers=self.extra_headers(),
        )


@dataclass(frozen=True)
class EnvConfig:
    """构建渠道时使用的一次性环境配置。"""

    env: Mapping[str, str]
    timeout: float
    session_factory: Callable[[], requests.Session]

    def value(self, name: str, default: str = "") -> str:
        return (self.env.get(name) or default).strip()

    def multi(self, name: str) -> list[str]:
        raw = self.env.get(name) or ""
        return [line.strip() for line in re.split(r"[\r\n]+", raw) if line.strip()]

    def session(self) -> requests.Session:
        return self.session_factory()


def _build_wecom(config: EnvConfig) -> list[Notifier]:
    return [
        WecomNotifier(url, session=config.session(), timeout=config.timeout)
        for url in config.multi("WECOM_WEBHOOK")
    ]


def _build_feishu(config: EnvConfig) -> list[Notifier]:
    secret = config.value("FEISHU_SECRET")
    return [
        FeishuNotifier(
            url, secret=secret, session=config.session(), timeout=config.timeout
        )
        for url in config.multi("FEISHU_WEBHOOK")
    ]


def _build_dingtalk(config: EnvConfig) -> list[Notifier]:
    secret = config.value("DINGTALK_SECRET")
    return [
        DingTalkNotifier(
            url, secret=secret, session=config.session(), timeout=config.timeout
        )
        for url in config.multi("DINGTALK_WEBHOOK")
    ]


def _build_synologychat(config: EnvConfig) -> list[Notifier]:
    return [
        SynologyChatNotifier(url, session=config.session(), timeout=config.timeout)
        for url in config.multi("SYNOLOGY_CHAT_URL")
    ]


def _build_serverchan(config: EnvConfig) -> list[Notifier]:
    return [
        ServerChanNotifier(
            sendkey, session=config.session(), timeout=config.timeout
        )
        for sendkey in config.multi("SERVERCHAN_SENDKEY")
    ]


def _build_pushplus(config: EnvConfig) -> list[Notifier]:
    return [
        PushPlusNotifier(token, session=config.session(), timeout=config.timeout)
        for token in config.multi("PUSHPLUS_TOKEN")
    ]


def _build_bark(config: EnvConfig) -> list[Notifier]:
    server = config.value("BARK_URL") or config.value("BARK_SERVER")
    return [
        BarkNotifier(
            server,
            key=config.value("BARK_KEY"),
            session=config.session(),
            timeout=config.timeout,
        )
    ]


def _build_telegram(config: EnvConfig) -> list[Notifier]:
    return [
        TelegramNotifier(
            config.value("TELEGRAM_BOT_TOKEN"),
            config.value("TELEGRAM_CHAT_ID"),
            session=config.session(),
            timeout=config.timeout,
        )
    ]


def _build_ntfy(config: EnvConfig) -> list[Notifier]:
    server = config.value("NTFY_SERVER", "https://ntfy.sh")
    token = config.value("NTFY_TOKEN")
    return [
        NtfyNotifier(
            server, topic, token=token, session=config.session(), timeout=config.timeout
        )
        for topic in config.multi("NTFY_TOPIC")
    ]


def _build_gotify(config: EnvConfig) -> list[Notifier]:
    return [
        GotifyNotifier(
            config.value("GOTIFY_URL"),
            config.value("GOTIFY_TOKEN"),
            session=config.session(),
            timeout=config.timeout,
        )
    ]


def _build_generic(config: EnvConfig) -> list[Notifier]:
    headers = config.value("GENERIC_WEBHOOK_HEADERS")
    return [
        GenericWebhookNotifier(
            url, headers=headers, session=config.session(), timeout=config.timeout
        )
        for url in config.multi("GENERIC_WEBHOOK")
    ]


@dataclass(frozen=True)
class ChannelSpec:
    """渠道元数据：用于判断是否启用以及构建 Notifier。"""

    name: str
    label: str
    env: tuple[str, ...]
    build: Callable[[EnvConfig], list[Notifier]]
    require_all: bool = True

    def enabled(self, env: Mapping[str, str]) -> bool:
        checks = [(env.get(name) or "").strip() for name in self.env]
        if self.require_all:
            return all(checks)
        return any(checks)


CHANNELS: tuple[ChannelSpec, ...] = (
    ChannelSpec("wecom", "企业微信", ("WECOM_WEBHOOK",), _build_wecom),
    ChannelSpec("feishu", "飞书", ("FEISHU_WEBHOOK",), _build_feishu),
    ChannelSpec("dingtalk", "钉钉", ("DINGTALK_WEBHOOK",), _build_dingtalk),
    ChannelSpec(
        "synologychat", "群晖 Chat", ("SYNOLOGY_CHAT_URL",), _build_synologychat
    ),
    ChannelSpec("serverchan", "Server酱", ("SERVERCHAN_SENDKEY",), _build_serverchan),
    ChannelSpec("pushplus", "PushPlus", ("PUSHPLUS_TOKEN",), _build_pushplus),
    ChannelSpec(
        "bark", "Bark", ("BARK_URL", "BARK_KEY", "BARK_SERVER"), _build_bark,
        require_all=False,
    ),
    ChannelSpec(
        "telegram",
        "Telegram",
        ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"),
        _build_telegram,
    ),
    ChannelSpec("ntfy", "ntfy", ("NTFY_TOPIC",), _build_ntfy),
    ChannelSpec(
        "gotify", "Gotify", ("GOTIFY_URL", "GOTIFY_TOKEN"), _build_gotify
    ),
    ChannelSpec("webhook", "通用Webhook", ("GENERIC_WEBHOOK",), _build_generic),
)

EXTRA_ENV_KEYS = ("NOTIFY_ENABLED", "NOTIFY_TIMEOUT")

#: 所有与推送相关的环境变量名，便于测试或排查时整体清理。
ENV_KEYS: tuple[str, ...] = tuple(
    dict.fromkeys(
        [name for spec in CHANNELS for name in spec.env] + list(EXTRA_ENV_KEYS)
    )
)


def _resolve_env(env: Mapping[str, str] | None) -> Mapping[str, str]:
    return os.environ if env is None else env


def channel_specs(env: Mapping[str, str] | None = None) -> list[ChannelSpec]:
    """返回当前已配置（即会真正推送）的渠道。"""

    resolved = _resolve_env(env)
    if _is_false(resolved.get("NOTIFY_ENABLED")):
        return []
    return [spec for spec in CHANNELS if spec.enabled(resolved)]


def channel_labels(env: Mapping[str, str] | None = None) -> list[str]:
    """返回已启用渠道的中文名称，用于日志展示。"""

    return [spec.label for spec in channel_specs(env)]


def build_notifiers(
    env: Mapping[str, str] | None = None,
    timeout: float | None = None,
    session_factory: Callable[[], requests.Session] | None = None,
) -> list[Notifier]:
    """根据环境变量构建本次需要使用的全部推送渠道。"""

    resolved = _resolve_env(env)
    if _is_false(resolved.get("NOTIFY_ENABLED")):
        return []
    register_env_secrets(resolved)
    effective_timeout = (
        _positive_float(resolved.get("NOTIFY_TIMEOUT"), DEFAULT_TIMEOUT)
        if timeout is None
        else timeout
    )
    config = EnvConfig(
        env=resolved,
        timeout=effective_timeout,
        session_factory=session_factory or requests.Session,
    )
    notifiers: list[Notifier] = []
    for spec in CHANNELS:
        if not spec.enabled(resolved):
            continue
        notifiers.extend(spec.build(config))
    return notifiers


def push(
    title: str,
    content: str,
    env: Mapping[str, str] | None = None,
    timeout: float | None = None,
    session_factory: Callable[[], requests.Session] | None = None,
) -> PushResult:
    """把结果推送到所有已配置渠道，返回成功与失败明细。

    单个渠道失败不会影响其他渠道，也不会抛出异常。
    """

    message = Message(title=redact(title), content=redact(content))
    sent: list[str] = []
    failures: list[str] = []
    for notifier in build_notifiers(env, timeout, session_factory):
        try:
            notifier.send(message)
        except NotifyError as exc:
            failures.append(redact(str(exc)))
        except Exception as exc:  # 任何意外都不能影响签到主流程
            failures.append(
                redact(f"{notifier.label} 推送异常：{type(exc).__name__}")
            )
        else:
            sent.append(notifier.label)
    return PushResult(sent=tuple(sent), failures=tuple(failures))


def main(argv: Sequence[str] | None = None) -> int:
    """命令行入口：``python notify.py --title 标题 --content 内容``。"""

    relax_console_encoding()
    register_env_secrets()
    parser = argparse.ArgumentParser(
        description="把一条消息推送到所有已配置的渠道，用于验证推送配置。"
    )
    parser.add_argument("--title", default=NOTIFY_GROUP, help="消息标题")
    parser.add_argument(
        "--content", default="", help="消息内容，使用 - 表示从标准输入读取"
    )
    parser.add_argument(
        "--list-channels", action="store_true", help="只列出当前已启用的渠道"
    )
    args = parser.parse_args(argv)

    if args.list_channels:
        labels = channel_labels()
        print("已启用推送渠道：" + ("、".join(labels) if labels else "无"))
        return 0

    content = sys.stdin.read().strip() if args.content == "-" else args.content
    result = push(args.title, content or "这是一条推送测试消息。")
    for label in result.sent:
        print(f"✅ 推送成功：{label}")
    for failure in result.failures:
        print(f"❌ 推送失败：{failure}")
    if not result.sent:
        print("⚠️ 没有成功推送任何渠道，请检查推送相关的环境变量。")
    return 0 if result.sent and not result.failures else 1


if __name__ == "__main__":
    sys.exit(main())
