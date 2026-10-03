"""回归测试：GitHub Actions 日志与推送内容中不得出现任何凭据。

覆盖三类泄露路径：

1. 账号凭据被拼接进错误信息（例如条目格式写错时）；
2. 渠道地址 / Token 被第三方接口回显在错误信息里；
3. 推送内容本身包含了凭据。
"""

import importlib.util
import io
import json
import os
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs

import notify
from checkIn_Quark import QuarkAPIError, main


ROOT = Path(__file__).resolve().parents[1]

KPS = "KPS-SECRET-1234567890"
SIGN = "SIGN-SECRET-0987654321"
VCODE = "112233445566"
CREDENTIALS = (KPS, SIGN, VCODE)

COOKIE = f"user=张三;kps={KPS};sign={SIGN};vcode={VCODE};"
WECOM_WEBHOOK = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=WECOM-KEY-abcdef123456"
FEISHU_WEBHOOK = "https://open.feishu.cn/open-apis/bot/v2/hook/abcdef12-3456-7890-abcd-ef1234567890"
SYNOLOGY_URL = "https://nas.example.com:5001/webapi/chatbot/v1/incoming?token=SYNO-TOKEN-abcdef"
GOTIFY_TOKEN = "GOTIFY-TOKEN-abcdef"
HEADER_SECRET = "HEADER-SECRET-abcdef"


def load_fake_server():
    spec = importlib.util.spec_from_file_location(
        "fake_webhook_server", ROOT / "tools" / "fake_webhook_server.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class StubQuark:
    """签到成功，不发起任何真实请求。"""

    def __init__(self, account):
        self.account = account

    def do_sign(self):
        return "88VIP\n💾 网盘总容量：10.00 GB\n✅ 签到成功 +1.00 GB，连签进度（3/7）"


class EchoingStubQuark:
    """模拟接口把请求参数原样回显在错误信息里的最坏情况。"""

    def __init__(self, account):
        self.account = account

    def do_sign(self):
        raise QuarkAPIError(
            f"凭证失效：kps={self.account['kps']}&sign={self.account['sign']}"
            f"&vcode={self.account['vcode']}"
        )


def run_main(cookie, factory, env=None):
    """运行一次签到并返回（退出码, 控制台输出）。"""

    buffer = io.StringIO()
    with mock.patch.dict(os.environ, env or {}, clear=False):
        with redirect_stdout(buffer):
            exit_code = main(cookie, quark_factory=factory)
    return exit_code, buffer.getvalue()


class RedactTests(unittest.TestCase):
    def setUp(self):
        notify.reset_secrets()
        self.addCleanup(notify.reset_secrets)

    def test_registered_secret_is_replaced(self):
        notify.register_secret("ECHOED-SECRET-TOKEN")
        self.assertEqual(
            notify.redact("错误信息 ECHOED-SECRET-TOKEN 结束"),
            "错误信息 *** 结束",
        )
        self.assertEqual(notify.registered_secret_count(), 1)

    def test_short_values_are_not_registered(self):
        notify.register_secret("abc")
        notify.register_secret("")
        notify.register_secret(None)
        self.assertEqual(notify.registered_secret_count(), 0)
        self.assertEqual(notify.redact("abc"), "abc")

    def test_registering_twice_keeps_one_entry(self):
        notify.register_secret("SAME-SECRET-VALUE")
        notify.register_secret("SAME-SECRET-VALUE")
        self.assertEqual(notify.registered_secret_count(), 1)

    def test_env_secrets_cover_query_path_and_headers(self):
        notify.register_env_secrets(
            {
                "WECOM_WEBHOOK": WECOM_WEBHOOK,
                "GOTIFY_URL": "https://push.example.com",
                "GOTIFY_TOKEN": GOTIFY_TOKEN,
                "SYNOLOGY_CHAT_URL": SYNOLOGY_URL,
                "BARK_URL": "https://api.day.app/BARKKEYabcdef123456",
                "GENERIC_WEBHOOK_HEADERS": (
                    '{"Authorization": "Bearer ' + HEADER_SECRET + '"}'
                ),
            }
        )
        text = (
            f"{WECOM_WEBHOOK} {GOTIFY_TOKEN} {SYNOLOGY_URL} "
            f"BARKKEYabcdef123456 Bearer {HEADER_SECRET}"
        )
        scrubbed = notify.redact(text)
        for secret in (
            WECOM_WEBHOOK,
            GOTIFY_TOKEN,
            SYNOLOGY_URL,
            "BARKKEYabcdef123456",
            HEADER_SECRET,
        ):
            self.assertNotIn(secret, scrubbed)
        self.assertIn("***", scrubbed)

    def test_push_redacts_remote_error_message(self):
        class EchoNotifier(notify.Notifier):
            label = "回显渠道"

            def send(self, message):
                raise notify.NotifyError(
                    f"返回错误：invalid url {WECOM_WEBHOOK} token={GOTIFY_TOKEN}"
                )

        notify.register_env_secrets({"WECOM_WEBHOOK": WECOM_WEBHOOK})
        notify.register_secret(GOTIFY_TOKEN)
        with mock.patch.object(notify, "build_notifiers", return_value=[EchoNotifier()]):
            result = notify.push("标题", "正文", env={})

        self.assertEqual(len(result.failures), 1)
        self.assertNotIn(WECOM_WEBHOOK, result.failures[0])
        self.assertNotIn(GOTIFY_TOKEN, result.failures[0])

    def test_push_redacts_message_content(self):
        captured = {}

        class CapturingNotifier(notify.Notifier):
            label = "捕获渠道"

            def send(self, message):
                captured["text"] = message.text()

        notify.register_secret(KPS)
        with mock.patch.object(
            notify, "build_notifiers", return_value=[CapturingNotifier()]
        ):
            notify.push("标题", f"正文包含 {KPS}", env={})

        self.assertNotIn(KPS, captured["text"])
        self.assertIn("***", captured["text"])


class ConsoleLogTests(unittest.TestCase):
    def setUp(self):
        notify.reset_secrets()
        self.addCleanup(notify.reset_secrets)

    def assertNoCredentials(self, text):
        for secret in CREDENTIALS:
            self.assertNotIn(secret, text, f"日志中泄露了凭据：{secret}")

    def test_successful_run_keeps_log_clean_and_shows_account_name(self):
        exit_code, output = run_main(COOKIE, StubQuark)

        self.assertEqual(exit_code, 0)
        self.assertNoCredentials(output)
        self.assertIn("张三", output)

    def test_api_error_that_echoes_credentials_is_scrubbed(self):
        exit_code, output = run_main(COOKIE, EchoingStubQuark)

        self.assertEqual(exit_code, 1)
        self.assertNoCredentials(output)
        self.assertIn("凭证失效", output)
        self.assertIn("***", output)

    def test_ampersand_entry_does_not_leak_credentials(self):
        """把 `;` 写成 `&` 时，整串凭据曾被当作账号备注名输出。"""

        entry = f"user=张三&kps={KPS}&sign={SIGN}&vcode={VCODE}"
        exit_code, output = run_main(entry, StubQuark)

        self.assertEqual(exit_code, 1)
        self.assertNoCredentials(output)
        self.assertIn("缺少必要参数", output)
        self.assertIn("第 1 个账号", output)

    def test_invalid_field_does_not_echo_entry_text(self):
        entry = f"user=张三;kps={KPS};{SIGN};vcode={VCODE};"
        exit_code, output = run_main(entry, StubQuark)

        self.assertEqual(exit_code, 1)
        self.assertNoCredentials(output)
        self.assertIn("格式错误", output)

    def test_url_only_entry_without_separators_does_not_leak(self):
        entry = (
            f"user=张三;url=https://drive-m.quark.cn/reward?kps={KPS}"
            f"&sign={SIGN}&vcode={VCODE};extra=value"
        )
        exit_code, output = run_main(entry, StubQuark)
        self.assertNoCredentials(output)

    def test_url_entry_whose_credentials_are_echoed_does_not_leak(self):
        """URL 条目里的 kps 只以查询参数形式出现，也必须被登记脱敏。"""

        entry = (
            f"user=张三;url=https://drive-m.quark.cn/reward?kps={KPS}"
            f"&sign={SIGN}&vcode={VCODE};extra=value"
        )
        exit_code, output = run_main(entry, EchoingStubQuark)

        self.assertEqual(exit_code, 1)
        self.assertNoCredentials(output)
        self.assertIn("***", output)
        self.assertIn("张三", output)

    def test_missing_cookie_log_is_clean(self):
        exit_code, output = run_main("", StubQuark)
        self.assertEqual(exit_code, 2)
        self.assertNoCredentials(output)

    def test_channel_configuration_is_never_echoed(self):
        env = {
            "WECOM_WEBHOOK": WECOM_WEBHOOK,
            "SYNOLOGY_CHAT_URL": SYNOLOGY_URL,
            "GOTIFY_URL": "https://push.example.com",
            "GOTIFY_TOKEN": GOTIFY_TOKEN,
            "SERVERCHAN_SENDKEY": "SCT-SECRET-abcdef",
        }
        buffer = io.StringIO()
        with mock.patch.dict(os.environ, env, clear=False):
            with mock.patch(
                "checkIn_Quark.notify.push", return_value=notify.PushResult()
            ):
                with redirect_stdout(buffer):
                    main(COOKIE, quark_factory=StubQuark)
        output = buffer.getvalue()

        for secret in (
            WECOM_WEBHOOK,
            SYNOLOGY_URL,
            GOTIFY_TOKEN,
            "SCT-SECRET-abcdef",
        ):
            self.assertNotIn(secret, output)
        self.assertIn("已启用推送渠道", output)


class PushPayloadTests(unittest.TestCase):
    """用本地假服务器抓取真实报文，确认推送到群里的内容同样没有凭据。"""

    def setUp(self):
        notify.reset_secrets()
        self.addCleanup(notify.reset_secrets)
        self.fake = load_fake_server()
        self.fake.RECEIVED.clear()
        self.server = self.fake.start_server("127.0.0.1", 0)
        self.addCleanup(self.stop_server)
        base = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.base = base

    def stop_server(self):
        self.server.shutdown()
        self.server.server_close()

    def captured_bodies(self):
        return [item["body"] for item in self.fake.RECEIVED]

    def decoded_texts(self):
        """把各渠道的报文还原成实际会展示的文本。"""

        texts = []
        for item in self.fake.RECEIVED:
            path = item["path"].split("?")[0]
            if path == "/synology":
                payload = parse_qs(item["body"])["payload"][0]
                texts.append(json.loads(payload)["text"])
            else:
                payload = json.loads(item["body"])
                texts.append(
                    payload.get("content")
                    or payload.get("markdown", {}).get("content", "")
                )
        return texts

    def test_pushed_payload_has_no_credentials(self):
        env = {
            "NO_PROXY": "127.0.0.1,localhost",
            "no_proxy": "127.0.0.1,localhost",
            "WECOM_WEBHOOK": f"{self.base}/wecom",
            "SYNOLOGY_CHAT_URL": f"{self.base}/synology?token=demo",
            "GENERIC_WEBHOOK": f"{self.base}/generic",
        }
        buffer = io.StringIO()
        with mock.patch.dict(os.environ, env, clear=False):
            with redirect_stdout(buffer):
                exit_code = main(COOKIE, EchoingStubQuark)

        self.assertEqual(exit_code, 1)
        self.assertEqual(len(self.fake.RECEIVED), 3)
        for body in self.captured_bodies():
            for secret in CREDENTIALS:
                self.assertNotIn(secret, body, "推送报文里出现了凭据")

        shown = "\n".join(self.decoded_texts())
        for secret in CREDENTIALS:
            self.assertNotIn(secret, shown, "推送内容里出现了凭据")
        self.assertIn("***", shown)
        self.assertIn("张三", shown)


if __name__ == "__main__":
    unittest.main()
