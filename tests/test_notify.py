import base64
import hashlib
import hmac
import io
import json
import os
import unittest
from contextlib import redirect_stdout
from unittest import mock
from urllib.parse import parse_qs, urlparse

import requests

import notify
from notify import (
    BarkNotifier,
    DingTalkNotifier,
    FeishuNotifier,
    GenericWebhookNotifier,
    GotifyNotifier,
    Message,
    NotifyError,
    NtfyNotifier,
    PushPlusNotifier,
    PushResult,
    ServerChanNotifier,
    SynologyChatNotifier,
    TelegramNotifier,
    WecomNotifier,
    bark_endpoint,
    build_notifiers,
    channel_labels,
    dingtalk_endpoint,
    push,
    serverchan_endpoint,
    truncate,
)


WECOM_URL = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=SUPER-SECRET-KEY"
FEISHU_URL = "https://open.feishu.cn/open-apis/bot/v2/hook/abcd-1234"


class FakeResponse:
    def __init__(self, payload=None, status_code=200):
        self.payload = payload
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}", response=self)

    def json(self):
        if self.payload is None:
            raise ValueError("响应不是 JSON")
        return self.payload


class FakeSession:
    """记录请求并按顺序返回预设响应（响应可以是异常实例）。"""

    def __init__(self, responses=()):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        if not self.responses:
            return FakeResponse({})
        item = self.responses.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


def clean_env(**overrides):
    """在没有推送配置的环境下运行，避免受本机变量影响。"""

    return mock.patch.dict(
        os.environ, {key: "" for key in notify.ENV_KEYS} | overrides, clear=False
    )


class MessageTests(unittest.TestCase):
    def test_text_combines_title_and_content(self):
        self.assertEqual(Message("标题", "正文").text(), "标题\n正文")

    def test_text_without_title_or_content(self):
        self.assertEqual(Message("", "正文").text(), "正文")
        self.assertEqual(Message("标题", "").text(), "标题")

    def test_truncate_respects_utf8_byte_limit(self):
        result = truncate("中文内容" * 50, 40)
        self.assertLessEqual(len(result.encode("utf-8")), 40)
        self.assertIn("已截断", result)
        self.assertNotIn("\ufffd", result)

    def test_truncate_keeps_short_text(self):
        self.assertEqual(truncate("短消息", 100), "短消息")
        self.assertEqual(truncate("不限长度", 0), "不限长度")


class WecomTests(unittest.TestCase):
    def test_sends_plain_text_and_accepts_ok(self):
        session = FakeSession([FakeResponse({"errcode": 0, "errmsg": "ok"})])
        WecomNotifier(WECOM_URL, session=session).send(Message("夸克自动签到", "✅ 签到成功"))

        method, url, kwargs = session.calls[0]
        self.assertEqual(method, "POST")
        self.assertEqual(url, WECOM_URL)
        self.assertEqual(kwargs["json"]["msgtype"], "text")
        self.assertNotIn("markdown", kwargs["json"])
        self.assertIn("✅ 签到成功", kwargs["json"]["text"]["content"])

    def test_long_content_fits_text_message_limit(self):
        """企业微信 text 消息上限 2048 字节，超长内容必须截断。"""

        session = FakeSession([FakeResponse({"errcode": 0})])
        WecomNotifier(WECOM_URL, session=session).send(
            Message("夸克自动签到", "第 1 个账号签到成功\n" * 500)
        )
        content = session.calls[0][2]["json"]["text"]["content"]
        self.assertLessEqual(len(content.encode("utf-8")), 2048)
        self.assertIn("已截断", content)

    def test_error_code_raises_without_leaking_webhook_key(self):
        session = FakeSession(
            [FakeResponse({"errcode": 93000, "errmsg": "invalid webhook url"})]
        )
        with self.assertRaises(NotifyError) as ctx:
            WecomNotifier(WECOM_URL, session=session).send(Message("标题", "正文"))

        message = str(ctx.exception)
        self.assertIn("invalid webhook url", message)
        self.assertIn("qyapi.weixin.qq.com", message)
        self.assertNotIn("SUPER-SECRET-KEY", message)

    def test_http_error_and_timeout_are_reported(self):
        with self.assertRaisesRegex(NotifyError, "HTTP 500"):
            WecomNotifier(
                WECOM_URL, session=FakeSession([FakeResponse({}, status_code=500)])
            ).send(Message("标题", "正文"))

        with self.assertRaisesRegex(NotifyError, "请求超时"):
            WecomNotifier(
                WECOM_URL, session=FakeSession([requests.Timeout()])
            ).send(Message("标题", "正文"))

    def test_empty_body_is_treated_as_success(self):
        session = FakeSession([FakeResponse(None)])
        WecomNotifier(WECOM_URL, session=session).send(Message("标题", "正文"))
        self.assertEqual(len(session.calls), 1)


class FeishuTests(unittest.TestCase):
    def test_sends_text_message(self):
        session = FakeSession([FakeResponse({"code": 0, "msg": "success"})])
        FeishuNotifier(FEISHU_URL, session=session).send(Message("夸克自动签到", "✅ 签到成功"))

        _, url, kwargs = session.calls[0]
        self.assertEqual(url, FEISHU_URL)
        self.assertEqual(kwargs["json"]["msg_type"], "text")
        self.assertIn("✅ 签到成功", kwargs["json"]["content"]["text"])

    def test_secret_adds_valid_signature(self):
        session = FakeSession([FakeResponse({"code": 0, "msg": "success"})])
        notifier = FeishuNotifier(FEISHU_URL, secret="s3cret", session=session)
        with mock.patch("notify.time.time", return_value=1700000000):
            notifier.send(Message("标题", "正文"))

        body = session.calls[0][2]["json"]
        self.assertEqual(body["timestamp"], "1700000000")
        raw = b"1700000000\ns3cret"
        expected = base64.b64encode(
            hmac.new(raw, digestmod=hashlib.sha256).digest()
        ).decode("utf-8")
        self.assertEqual(body["sign"], expected)

    def test_accepts_legacy_status_code_success(self):
        session = FakeSession([FakeResponse({"StatusCode": 0, "StatusMessage": "success"})])
        FeishuNotifier(FEISHU_URL, session=session).send(Message("标题", "正文"))

    def test_error_code_raises(self):
        session = FakeSession([FakeResponse({"code": 19021, "msg": "sign match fail"})])
        with self.assertRaisesRegex(NotifyError, "sign match fail"):
            FeishuNotifier(FEISHU_URL, session=session).send(Message("标题", "正文"))


class DingTalkTests(unittest.TestCase):
    def test_endpoint_without_secret_is_unchanged(self):
        self.assertEqual(dingtalk_endpoint("https://oapi.dingtalk.com/robot/send?x=1"), "https://oapi.dingtalk.com/robot/send?x=1")

    def test_endpoint_with_secret_is_signed(self):
        endpoint = dingtalk_endpoint(
            "https://oapi.dingtalk.com/robot/send?access_token=tok", "SECret"
        )
        query = parse_qs(urlparse(endpoint).query)
        self.assertEqual(query["access_token"], ["tok"])
        timestamp = query["timestamp"][0]
        raw = f"{timestamp}\nSECret".encode("utf-8")
        expected = base64.b64encode(
            hmac.new(b"SECret", raw, digestmod=hashlib.sha256).digest()
        ).decode("utf-8")
        self.assertEqual(query["sign"], [expected])

    def test_sends_markdown(self):
        session = FakeSession([FakeResponse({"errcode": 0, "errmsg": "ok"})])
        DingTalkNotifier("https://oapi.dingtalk.com/robot/send?access_token=tok", session=session).send(
            Message("夸克自动签到", "✅ 签到成功")
        )
        body = session.calls[0][2]["json"]
        self.assertEqual(body["msgtype"], "markdown")
        self.assertEqual(body["markdown"]["title"], "夸克自动签到")


class SynologyChatTests(unittest.TestCase):
    def test_posts_form_encoded_payload(self):
        session = FakeSession([FakeResponse({"success": True})])
        SynologyChatNotifier("http://nas.local:5000/webapi/chatbot/v1/incoming?token=t", session=session).send(
            Message("夸克自动签到", "✅ 签到成功")
        )

        _, _, kwargs = session.calls[0]
        self.assertIn("application/x-www-form-urlencoded", kwargs["headers"]["Content-Type"])
        payload = json.loads(kwargs["data"]["payload"])
        self.assertIn("✅ 签到成功", payload["text"])

    def test_failure_flag_raises(self):
        session = FakeSession([FakeResponse({"success": False, "error": "token invalid"})])
        with self.assertRaisesRegex(NotifyError, "token invalid"):
            SynologyChatNotifier(
                "http://nas.local:5000/webapi/chatbot/v1/incoming?token=t", session=session
            ).send(Message("标题", "正文"))


class ServerChanTests(unittest.TestCase):
    def test_endpoint_for_classic_and_turbo_keys(self):
        self.assertEqual(
            serverchan_endpoint("SCT123abc"), "https://sctapi.ftqq.com/SCT123abc.send"
        )
        self.assertEqual(
            serverchan_endpoint("sctp9876tToken"),
            "https://9876.push.ft07.com/send/sctp9876tToken.send",
        )

    def test_sends_title_and_markdown_body(self):
        session = FakeSession([FakeResponse({"code": 0, "message": ""})])
        ServerChanNotifier("SCT123", session=session).send(Message("夸克自动签到", "✅ 签到成功"))
        _, url, kwargs = session.calls[0]
        self.assertEqual(url, "https://sctapi.ftqq.com/SCT123.send")
        self.assertEqual(kwargs["data"]["title"], "夸克自动签到")
        self.assertIn("✅ 签到成功", kwargs["data"]["desp"])

    def test_error_code_raises(self):
        session = FakeSession([FakeResponse({"code": 40001, "message": "bad key"})])
        with self.assertRaisesRegex(NotifyError, "bad key"):
            ServerChanNotifier("SCT123", session=session).send(Message("标题", "正文"))


class PushPlusTests(unittest.TestCase):
    def test_sends_markdown_template(self):
        session = FakeSession([FakeResponse({"code": 200, "msg": "请求成功"})])
        PushPlusNotifier("token-123", session=session).send(Message("夸克自动签到", "✅ 签到成功"))
        _, url, kwargs = session.calls[0]
        self.assertEqual(url, "https://www.pushplus.plus/send")
        self.assertEqual(kwargs["json"]["token"], "token-123")
        self.assertEqual(kwargs["json"]["template"], "markdown")

    def test_error_code_raises(self):
        session = FakeSession([FakeResponse({"code": 500, "msg": "token 无效"})])
        with self.assertRaisesRegex(NotifyError, "token 无效"):
            PushPlusNotifier("token-123", session=session).send(Message("标题", "正文"))


class BarkTests(unittest.TestCase):
    def test_endpoint_variants(self):
        self.assertEqual(
            bark_endpoint("https://api.day.app/deviceKey"),
            ("https://api.day.app/push", "deviceKey"),
        )
        self.assertEqual(
            bark_endpoint("https://api.day.app", "deviceKey"),
            ("https://api.day.app/push", "deviceKey"),
        )
        self.assertEqual(
            bark_endpoint("http://nas.local:8080/push", "deviceKey"),
            ("http://nas.local:8080/push", "deviceKey"),
        )

    def test_sends_device_key_and_group(self):
        session = FakeSession([FakeResponse({"code": 200, "message": "success"})])
        BarkNotifier("https://api.day.app/deviceKey", session=session).send(
            Message("夸克自动签到", "✅ 签到成功")
        )
        _, url, kwargs = session.calls[0]
        self.assertEqual(url, "https://api.day.app/push")
        self.assertEqual(kwargs["json"]["device_key"], "deviceKey")
        self.assertEqual(kwargs["json"]["group"], notify.NOTIFY_GROUP)
        self.assertIn("✅ 签到成功", kwargs["json"]["body"])

    def test_missing_device_key_raises(self):
        with self.assertRaisesRegex(NotifyError, "设备 Key"):
            BarkNotifier("https://api.day.app", session=FakeSession()).send(
                Message("标题", "正文")
            )


class TelegramTests(unittest.TestCase):
    def test_sends_message_to_chat(self):
        session = FakeSession([FakeResponse({"ok": True})])
        TelegramNotifier("bot-token", "12345", session=session).send(
            Message("夸克自动签到", "✅ 签到成功")
        )
        _, url, kwargs = session.calls[0]
        self.assertEqual(url, "https://api.telegram.org/botbot-token/sendMessage")
        self.assertEqual(kwargs["json"]["chat_id"], "12345")
        self.assertTrue(kwargs["json"]["disable_web_page_preview"])

    def test_not_ok_response_raises(self):
        session = FakeSession([FakeResponse({"ok": False, "description": "chat not found"})])
        with self.assertRaisesRegex(NotifyError, "chat not found"):
            TelegramNotifier("bot-token", "12345", session=session).send(Message("标题", "正文"))


class NtfyTests(unittest.TestCase):
    def test_posts_body_with_encoded_title_and_token(self):
        session = FakeSession([FakeResponse({"id": "abc"})])
        NtfyNotifier("https://ntfy.sh", "quark-topic", token="tk", session=session).send(
            Message("夸克自动签到", "✅ 签到成功")
        )
        _, url, kwargs = session.calls[0]
        self.assertEqual(url, "https://ntfy.sh/quark-topic")
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer tk")
        self.assertTrue(kwargs["headers"]["Title"].startswith("=?UTF-8?B?"))
        self.assertIn("✅ 签到成功", kwargs["data"].decode("utf-8"))

    def test_ascii_title_is_kept_readable(self):
        session = FakeSession([FakeResponse({"id": "abc"})])
        NtfyNotifier("https://ntfy.sh", "topic", session=session).send(
            Message("Quark", "done")
        )
        self.assertEqual(session.calls[0][2]["headers"]["Title"], "Quark")

    def test_error_payload_raises(self):
        session = FakeSession([FakeResponse({"error": "topic not found"})])
        with self.assertRaisesRegex(NotifyError, "topic not found"):
            NtfyNotifier("https://ntfy.sh", "topic", session=session).send(
                Message("标题", "正文")
            )


class GotifyTests(unittest.TestCase):
    def test_posts_message_with_token(self):
        session = FakeSession([FakeResponse({"id": 7})])
        GotifyNotifier("https://gotify.example.com/", "app-token", session=session).send(
            Message("夸克自动签到", "✅ 签到成功")
        )
        _, url, kwargs = session.calls[0]
        self.assertEqual(url, "https://gotify.example.com/message?token=app-token")
        self.assertEqual(kwargs["json"]["title"], "夸克自动签到")

    def test_error_payload_raises(self):
        session = FakeSession([FakeResponse({"error": "unauthorized"})])
        with self.assertRaisesRegex(NotifyError, "unauthorized"):
            GotifyNotifier("https://gotify.example.com", "app-token", session=session).send(
                Message("标题", "正文")
            )


class GenericWebhookTests(unittest.TestCase):
    def test_posts_json_with_custom_headers(self):
        session = FakeSession([FakeResponse({})])
        GenericWebhookNotifier(
            "https://example.test/hook",
            headers='{"Authorization": "Bearer secret"}',
            session=session,
        ).send(Message("夸克自动签到", "✅ 签到成功"))

        _, url, kwargs = session.calls[0]
        self.assertEqual(url, "https://example.test/hook")
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer secret")
        self.assertEqual(kwargs["json"]["title"], "夸克自动签到")
        self.assertEqual(kwargs["json"]["source"], notify.NOTIFY_GROUP)

    def test_invalid_headers_json_raises(self):
        with self.assertRaisesRegex(NotifyError, "JSON"):
            GenericWebhookNotifier(
                "https://example.test/hook", headers="not-json", session=FakeSession()
            ).send(Message("标题", "正文"))


class ConfigTests(unittest.TestCase):
    def test_no_configuration_builds_nothing(self):
        self.assertEqual(build_notifiers({}), [])
        self.assertEqual(channel_labels({}), [])

    def test_multiple_endpoints_create_multiple_notifiers(self):
        env = {
            "WECOM_WEBHOOK": "https://a.test/hook\n\nhttps://b.test/hook\n",
            "FEISHU_WEBHOOK": "https://c.test/hook",
        }
        notifiers = build_notifiers(env, session_factory=FakeSession)
        self.assertEqual([item.name for item in notifiers], ["wecom", "wecom", "feishu"])
        self.assertEqual(channel_labels(env), ["企业微信", "飞书"])

    def test_notify_enabled_false_disables_everything(self):
        env = {"WECOM_WEBHOOK": "https://a.test/hook", "NOTIFY_ENABLED": "false"}
        self.assertEqual(build_notifiers(env, session_factory=FakeSession), [])
        self.assertEqual(channel_labels(env), [])

    def test_partial_configuration_does_not_enable_channel(self):
        env = {"TELEGRAM_BOT_TOKEN": "bot-token"}
        self.assertEqual(channel_labels(env), [])
        env = {"BARK_KEY": "deviceKey"}
        self.assertEqual(channel_labels(env), ["Bark"])

    def test_timeout_comes_from_environment(self):
        env = {"WECOM_WEBHOOK": "https://a.test/hook", "NOTIFY_TIMEOUT": "3"}
        notifier = build_notifiers(env, session_factory=FakeSession)[0]
        self.assertEqual(notifier.timeout, 3.0)

    def test_all_channels_are_listed(self):
        env = {
            "WECOM_WEBHOOK": "https://a.test/1",
            "FEISHU_WEBHOOK": "https://a.test/2",
            "DINGTALK_WEBHOOK": "https://a.test/3",
            "SYNOLOGY_CHAT_URL": "https://a.test/4",
            "SERVERCHAN_SENDKEY": "SCT1",
            "PUSHPLUS_TOKEN": "tok",
            "BARK_URL": "https://api.day.app/key",
            "TELEGRAM_BOT_TOKEN": "bot",
            "TELEGRAM_CHAT_ID": "1",
            "NTFY_TOPIC": "topic",
            "GOTIFY_URL": "https://a.test/5",
            "GOTIFY_TOKEN": "tok",
            "GENERIC_WEBHOOK": "https://a.test/6",
        }
        self.assertEqual(
            channel_labels(env),
            [
                "企业微信",
                "飞书",
                "钉钉",
                "群晖 Chat",
                "Server酱",
                "PushPlus",
                "Bark",
                "Telegram",
                "ntfy",
                "Gotify",
                "通用Webhook",
            ],
        )
        self.assertEqual(len(build_notifiers(env, session_factory=FakeSession)), 11)


class PushTests(unittest.TestCase):
    def test_push_without_channels_reports_nothing(self):
        self.assertEqual(push("标题", "正文", env={}), PushResult())

    def test_one_failing_channel_does_not_block_others(self):
        sessions = [
            FakeSession([FakeResponse({"errcode": 0, "errmsg": "ok"})]),
            FakeSession([FakeResponse({"errcode": 93000, "errmsg": "boom"})]),
        ]
        env = {"WECOM_WEBHOOK": "https://ok.test/hook\nhttps://bad.test/hook"}

        result = push("标题", "正文", env=env, session_factory=lambda: sessions.pop(0))

        self.assertEqual(result.sent, ("企业微信",))
        self.assertEqual(len(result.failures), 1)
        self.assertIn("boom", result.failures[0])
        self.assertIn("bad.test", result.failures[0])

    def test_unexpected_error_is_captured(self):
        class BrokenNotifier(notify.Notifier):
            label = "坏渠道"

            def send(self, message):
                raise RuntimeError("boom")

        with mock.patch.object(
            notify, "build_notifiers", return_value=[BrokenNotifier()]
        ):
            result = push("标题", "正文", env={})
        self.assertEqual(result.sent, ())
        self.assertIn("坏渠道 推送异常：RuntimeError", result.failures[0])


class CommandLineTests(unittest.TestCase):
    def test_list_channels(self):
        with clean_env(WECOM_WEBHOOK="https://a.test/hook"):
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                code = notify.main(["--list-channels"])
        self.assertEqual(code, 0)
        self.assertIn("企业微信", buffer.getvalue())

    def test_test_message_reports_success(self):
        with clean_env(WECOM_WEBHOOK="https://a.test/hook"):
            with mock.patch.object(
                notify, "push", return_value=PushResult(sent=("企业微信",))
            ):
                buffer = io.StringIO()
                with redirect_stdout(buffer):
                    code = notify.main(["--title", "测试", "--content", "正文"])
        self.assertEqual(code, 0)
        self.assertIn("推送成功：企业微信", buffer.getvalue())

    def test_no_channel_returns_failure(self):
        with clean_env():
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                code = notify.main(["--title", "测试"])
        self.assertEqual(code, 1)
        self.assertIn("没有成功推送任何渠道", buffer.getvalue())

    def test_content_can_be_read_from_stdin(self):
        with clean_env():
            with mock.patch.object(
                notify, "push", return_value=PushResult(sent=("飞书",))
            ) as push_mock, mock.patch("sys.stdin", io.StringIO("来自标准输入")):
                with redirect_stdout(io.StringIO()):
                    code = notify.main(["--content", "-"])
        self.assertEqual(code, 0)
        self.assertEqual(push_mock.call_args.args[1], "来自标准输入")


if __name__ == "__main__":
    unittest.main()
