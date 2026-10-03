"""Quark cloud-drive daily sign-in.

The script accepts one or more account entries from ``COOKIE_QUARK``. Accounts
may be separated by a newline or ``&&``. Both the legacy kps/sign/vcode format
and the newer captured-URL format are supported.

After every account is processed the summary is pushed to each notification
channel configured through the environment (see ``notify.py``); a failing
channel only logs a warning and never changes the sign-in exit code.
"""

from __future__ import annotations

import os
import re
import sys
from typing import Callable
from urllib.parse import unquote, urlparse

import notify
import requests


INFO_URL = "https://drive-m.quark.cn/1/clouddrive/capacity/growth/info"
SIGN_URL = "https://drive-m.quark.cn/1/clouddrive/capacity/growth/sign"
REQUIRED_PARAMS = ("kps", "sign", "vcode")


class ConfigError(ValueError):
    """Raised when COOKIE_QUARK is missing or malformed."""


class QuarkAPIError(RuntimeError):
    """Raised when the Quark API cannot complete a sign-in operation."""


def log(message: str = "") -> None:
    """Print one line after scrubbing every registered credential."""

    print(notify.redact(message))


def send(title: str, message: str) -> list[str]:
    """Print the summary and push it to every configured channel.

    Both the console output and the pushed message are scrubbed first, so a
    credential that somehow ended up in an error string never reaches the
    Actions log or a chat group.
    """

    title = notify.redact(title)
    message = notify.redact(message)
    log(f"{title}:\n{message}")
    return list(notify.push(title, message).failures)


def report_push_failures(failures: list[str]) -> None:
    """Print push failures as warnings without changing the sign-in result."""

    for failure in failures:
        log(f"⚠️ 推送失败：{failure}")


def split_account_entries(raw_value: str | None) -> list[str]:
    """Split COOKIE_QUARK into non-empty account entries."""

    if not raw_value or not raw_value.strip():
        raise ConfigError("未配置 COOKIE_QUARK，或变量内容为空")

    entries = [entry.strip() for entry in re.split(r"\r?\n|&&", raw_value)]
    entries = [entry for entry in entries if entry]
    if not entries:
        raise ConfigError("COOKIE_QUARK 中没有可用的账号配置")
    return entries


def extract_params(url: str) -> dict[str, str]:
    """Extract the credentials used by the mobile growth API from a URL."""

    # Quark credentials may contain literal "+" characters.  parse_qs() treats
    # "+" as a space (application/x-www-form-urlencoded semantics), which
    # corrupts newer captured URLs.  Split the raw query ourselves and apply
    # percent-decoding only, preserving literal plus signs exactly as captured.
    query: dict[str, str] = {}
    for part in urlparse(url).query.split("&"):
        if not part:
            continue
        key, separator, value = part.partition("=")
        if not separator:
            value = ""
        key = unquote(key)
        if key not in query:
            query[key] = unquote(value)

    return {name: query.get(name, "") for name in REQUIRED_PARAMS}


_NOTE_MAX_LENGTH = 32
_NOTE_FORBIDDEN = (
    "kps=",
    "sign=",
    "vcode=",
    "token=",
    "key=",
    "secret",
    "http://",
    "https://",
    "cookie",
)


def _sanitize_user_note(value: str) -> str:
    """Make a ``user=`` note safe to display.

    The note only labels an account, so anything that looks like a captured
    URL or a credential is dropped instead of being logged or pushed.
    """

    note = "".join(char for char in value.strip() if char.isprintable())
    lowered = note.lower()
    if any(hint in lowered for hint in _NOTE_FORBIDDEN):
        return ""
    if len(note) > _NOTE_MAX_LENGTH:
        note = note[:_NOTE_MAX_LENGTH] + "…"
    return note


def extract_user_note(entry: str) -> str:
    """Read the ``user=`` note from a raw entry without validating it.

    This runs before :func:`parse_account`, so an entry that is invalid (or
    whose credentials were rejected) can still be reported by name.
    """

    for field in re.split(r";|\r?\n", entry):
        if "=" not in field:
            continue
        key, value = field.split("=", 1)
        if key.strip().lower() == "user":
            return _sanitize_user_note(value)
    return ""


def account_label(
    entry: str, index: int, account: dict[str, str] | None = None
) -> str:
    """Return ``第 N 个账号（备注名）`` for logs and push messages."""

    name = _sanitize_user_note((account or {}).get("user", "")) or extract_user_note(
        entry
    )
    return f"第 {index} 个账号（{name or f'账号{index}'}）"


def register_account_secrets(
    raw_value: str | None, entries: list[str]
) -> None:
    """Register account credentials so any echo of them is scrubbed.

    Values are split on ``;``, ``&`` and ``?`` so the credentials inside a
    captured URL are registered even when the entry itself is malformed.
    ``user`` is intentionally excluded: it is the display name we want to
    keep visible in the push message.
    """

    notify.register_secret(raw_value)
    for entry in entries:
        notify.register_secret(entry)
        for field in re.split(r";|\r?\n|&|\?", entry):
            if "=" not in field:
                continue
            key, value = field.split("=", 1)
            if key.strip().lower() == "user":
                continue
            notify.register_secret(value)


def register_account_credentials(account: dict[str, str]) -> None:
    """Register the credentials of a successfully parsed account."""

    for key in REQUIRED_PARAMS:
        notify.register_secret(account.get(key))


def parse_account(entry: str, index: int) -> dict[str, str]:
    """Parse and validate one account entry.

    Error messages stay account-agnostic; the caller prefixes them with
    :func:`account_label`, which knows the ``user`` note.
    """

    account: dict[str, str] = {}
    for position, field in enumerate(entry.split(";"), start=1):
        field = field.strip()
        if not field:
            continue
        if "=" not in field:
            # Never echo the raw text: it may be a stray credential.
            raise ConfigError(f"第 {position} 个字段格式错误（应为 key=value）")
        key, value = field.split("=", 1)
        key = key.strip()
        if not key:
            raise ConfigError(f"第 {position} 个字段的字段名为空")
        account[key] = value.strip()

    if "url" in account:
        for key, value in extract_params(account["url"]).items():
            account.setdefault(key, value)

    missing = [key for key in REQUIRED_PARAMS if not account.get(key)]
    if missing:
        raise ConfigError(f"缺少必要参数：{', '.join(missing)}")

    account.setdefault("user", f"账号{index}")
    return account


def _api_error_message(payload: dict, fallback: str) -> str:
    message = payload.get("message") or payload.get("msg")
    code = payload.get("code")
    if message:
        return str(message)
    if code is not None:
        return f"{fallback}（code={code}）"
    return fallback


class Quark:
    """Small client for Quark's mobile growth endpoints."""

    def __init__(
        self,
        account: dict[str, str],
        session: requests.Session | None = None,
        timeout: int = 20,
    ) -> None:
        self.account = account
        self.session = session or requests.Session()
        self.timeout = timeout

    @staticmethod
    def convert_bytes(value: int | float) -> str:
        units = ("B", "KB", "MB", "GB", "TB", "PB", "EB", "ZB", "YB")
        size = float(value)
        unit_index = 0
        while size >= 1024 and unit_index < len(units) - 1:
            size /= 1024
            unit_index += 1
        return f"{size:.2f} {units[unit_index]}"

    def _params(self) -> dict[str, str]:
        return {
            "pr": "ucpro",
            "fr": "android",
            **{key: self.account[key] for key in REQUIRED_PARAMS},
        }

    def _request(self, method: str, url: str, **kwargs) -> dict:
        try:
            response = self.session.request(
                method, url, timeout=self.timeout, **kwargs
            )
            response.raise_for_status()
            payload = response.json()
        except requests.Timeout as exc:
            raise QuarkAPIError("请求夸克接口超时") from exc
        except requests.RequestException as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            detail = f"HTTP {status}" if status else type(exc).__name__
            raise QuarkAPIError(f"请求夸克接口失败（{detail}）") from exc
        except ValueError as exc:
            raise QuarkAPIError("夸克接口返回了无法解析的数据") from exc

        if not isinstance(payload, dict):
            raise QuarkAPIError("夸克接口返回格式异常")
        return payload

    def get_growth_info(self) -> dict:
        payload = self._request("GET", INFO_URL, params=self._params())
        data = payload.get("data")
        if not isinstance(data, dict):
            raise QuarkAPIError(_api_error_message(payload, "获取成长信息失败"))
        return data

    def get_growth_sign(self) -> int | float:
        payload = self._request(
            "POST",
            SIGN_URL,
            params=self._params(),
            json={"sign_cyclic": True},
        )
        data = payload.get("data")
        if not isinstance(data, dict) or "sign_daily_reward" not in data:
            raise QuarkAPIError(_api_error_message(payload, "签到失败"))
        return data["sign_daily_reward"]

    def do_sign(self) -> str:
        growth_info = self.get_growth_info()
        cap_sign = growth_info.get("cap_sign")
        if not isinstance(cap_sign, dict):
            raise QuarkAPIError("成长信息中缺少 cap_sign 字段")

        vip_label = "88VIP" if growth_info.get("88VIP") else "普通用户"
        total_capacity = growth_info.get("total_capacity", 0)
        composition = growth_info.get("cap_composition") or {}
        accumulated = composition.get("sign_reward", 0)
        progress = cap_sign.get("sign_progress", "?")
        target = cap_sign.get("sign_target", "?")

        # 账号备注名由调用方放在标题行，这里只描述签到结果本身。
        lines = [
            vip_label,
            f"💾 网盘总容量：{self.convert_bytes(total_capacity)}，"
            f"签到累计容量：{self.convert_bytes(accumulated)}",
        ]

        if cap_sign.get("sign_daily"):
            reward = cap_sign.get("sign_daily_reward", 0)
            lines.append(
                f"✅ 今日已签到 +{self.convert_bytes(reward)}，"
                f"连签进度（{progress}/{target}）"
            )
        else:
            reward = self.get_growth_sign()
            next_progress = progress + 1 if isinstance(progress, int) else "?"
            lines.append(
                f"✅ 签到成功 +{self.convert_bytes(reward)}，"
                f"连签进度（{next_progress}/{target}）"
            )

        return "\n".join(lines)


def main(
    cookie_value: str | None = None,
    quark_factory: Callable[[dict[str, str]], Quark] | None = None,
) -> int:
    """Run every configured account and return a process-compatible exit code."""

    notify.relax_console_encoding()
    notify.register_env_secrets()
    log("----------夸克网盘开始签到----------")
    labels = notify.channel_labels()
    if labels:
        log(f"📮 已启用推送渠道：{'、'.join(labels)}")
    else:
        log("📮 未配置推送渠道，结果仅输出到日志")
    if cookie_value is None:
        cookie_value = os.getenv("COOKIE_QUARK")
    quark_factory = quark_factory or Quark

    try:
        entries = split_account_entries(cookie_value)
    except ConfigError as exc:
        log(f"❌ {exc}")
        report_push_failures(send("夸克自动签到（配置错误）", str(exc)))
        return 2

    register_account_secrets(cookie_value, entries)
    log(f"✅ 检测到共 {len(entries)} 个夸克账号\n")
    results: list[str] = []
    failures = 0

    for index, entry in enumerate(entries, start=1):
        account: dict[str, str] | None = None
        try:
            account = parse_account(entry, index)
            register_account_credentials(account)
            result = quark_factory(account).do_sign()
        except (ConfigError, QuarkAPIError, KeyError, TypeError, ValueError) as exc:
            failures += 1
            results.append(
                f"❌ {account_label(entry, index, account)}\n❌ {notify.redact(str(exc))}"
            )
        except Exception as exc:  # Keep later accounts running on unexpected errors.
            failures += 1
            results.append(
                f"❌ {account_label(entry, index, account)}\n"
                f"❌ 未知错误：{type(exc).__name__}"
            )
        else:
            results.append(
                f"✅ {account_label(entry, index, account)}\n{notify.redact(result)}"
            )

    headline = (
        f"共 {len(entries)} 个账号，成功 {len(entries) - failures}，失败 {failures}"
    )
    summary = "\n\n".join([headline, *results])
    title = "夸克自动签到"
    if failures:
        title = f"夸克自动签到（{failures} 个账号失败）"
    report_push_failures(send(title, summary))
    log(
        f"\n----------执行完毕：成功 {len(entries) - failures}，失败 {failures}----------"
    )
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
