import io
import os
import unittest
from contextlib import redirect_stdout
from unittest import mock

import notify
from checkIn_Quark import (
    ConfigError,
    Quark,
    QuarkAPIError,
    account_label,
    extract_params,
    extract_user_note,
    main,
    parse_account,
    split_account_entries,
)


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self.payload = payload
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise QuarkAPIError(f"HTTP {self.status_code}")

    def json(self):
        return self.payload


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return self.responses.pop(0)


class ParsingTests(unittest.TestCase):
    def test_split_accounts_supports_newlines_crlf_and_double_ampersand(self):
        entries = split_account_entries(" one \r\n\r\n two && three ")
        self.assertEqual(entries, ["one", "two", "three"])

    def test_extract_params_from_captured_url(self):
        params = extract_params(
            "https://drive-m.quark.cn/path?foo=1&kps=a%2Bb&sign=s&vcode=v"
        )
        self.assertEqual(params, {"kps": "a+b", "sign": "s", "vcode": "v"})


    def test_extract_params_preserves_literal_plus_in_new_captured_url(self):
        params = extract_params(
            "https://drive-m.quark.cn/1/clouddrive/act/growth/reward"
            "?mt=token+part&kps=AASx+abc%2Bencoded%3D"
            "&sign=AAQH+sig%2Bencoded%3D&vcode=1790667254217&app=clouddrive"
        )
        self.assertEqual(params["kps"], "AASx+abc+encoded=")
        self.assertEqual(params["sign"], "AAQH+sig+encoded=")
        self.assertEqual(params["vcode"], "1790667254217")

    def test_parse_account_supports_legacy_format(self):
        account = parse_account("user=张三; kps=k; sign=s; vcode=v;", 1)
        self.assertEqual(account["user"], "张三")
        self.assertEqual(account["kps"], "k")

    def test_parse_account_supports_captured_url_format(self):
        account = parse_account(
            "user=李四; url=https://example.test/reward?kps=k&sign=s&vcode=v;",
            1,
        )
        self.assertEqual(account["sign"], "s")

    def test_parse_account_reports_missing_parameters(self):
        with self.assertRaisesRegex(ConfigError, "sign, vcode"):
            parse_account("user=张三; kps=k;", 1)

    def test_extract_user_note_reads_legacy_and_url_entries(self):
        self.assertEqual(extract_user_note("user=张三; kps=k; sign=s;"), "张三")
        self.assertEqual(
            extract_user_note("user=李四; url=https://e.test/?kps=k;"), "李四"
        )
        self.assertEqual(extract_user_note("  USER = 王五 ; kps=k;"), "王五")

    def test_extract_user_note_returns_empty_without_note(self):
        self.assertEqual(extract_user_note("kps=k; sign=s; vcode=v;"), "")
        self.assertEqual(extract_user_note("user=; kps=k;"), "")

    def test_account_label_prefers_parsed_user(self):
        account = {"user": "张三"}
        self.assertEqual(
            account_label("user=李四; kps=k;", 2, account), "第 2 个账号（张三）"
        )
        self.assertEqual(
            account_label("user=李四; kps=k;", 2), "第 2 个账号（李四）"
        )
        self.assertEqual(account_label("kps=k;", 3), "第 3 个账号（账号3）")


class ClientTests(unittest.TestCase):
    def setUp(self):
        self.account = {"user": "测试", "kps": "k", "sign": "s", "vcode": "v"}

    def test_already_signed_is_successful(self):
        session = FakeSession(
            [
                FakeResponse(
                    {
                        "data": {
                            "88VIP": False,
                            "total_capacity": 1024,
                            "cap_composition": {"sign_reward": 512},
                            "cap_sign": {
                                "sign_daily": True,
                                "sign_daily_reward": 256,
                                "sign_progress": 2,
                                "sign_target": 7,
                            },
                        }
                    }
                )
            ]
        )
        result = Quark(self.account, session=session).do_sign()
        self.assertIn("今日已签到", result)
        self.assertEqual(len(session.calls), 1)

    def test_unsigned_account_posts_sign_request(self):
        session = FakeSession(
            [
                FakeResponse(
                    {
                        "data": {
                            "88VIP": True,
                            "total_capacity": 2048,
                            "cap_composition": {},
                            "cap_sign": {
                                "sign_daily": False,
                                "sign_progress": 2,
                                "sign_target": 7,
                            },
                        }
                    }
                ),
                FakeResponse({"data": {"sign_daily_reward": 1024}}),
            ]
        )
        result = Quark(self.account, session=session).do_sign()
        self.assertIn("签到成功", result)
        self.assertEqual([call[0] for call in session.calls], ["GET", "POST"])

    def test_api_error_is_not_treated_as_success(self):
        session = FakeSession([FakeResponse({"code": 401, "message": "凭证失效"})])
        with self.assertRaisesRegex(QuarkAPIError, "凭证失效"):
            Quark(self.account, session=session).do_sign()


class MainTests(unittest.TestCase):
    def setUp(self):
        """屏蔽本机可能存在的推送配置，避免测试真的发起网络请求。"""

        patcher = mock.patch.dict(
            os.environ, {key: "" for key in notify.ENV_KEYS}, clear=False
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_multi_account_continues_after_one_failure_and_returns_nonzero(self):
        seen = []

        class StubQuark:
            def __init__(self, account):
                self.account = account

            def do_sign(self):
                seen.append(self.account["user"])
                if self.account["user"] == "坏账号":
                    raise QuarkAPIError("凭证失效")
                return "✅ 签到成功"

        raw = (
            "user=坏账号;kps=1;sign=1;vcode=1;\n"
            "user=好账号;kps=2;sign=2;vcode=2;"
        )
        with redirect_stdout(io.StringIO()):
            exit_code = main(raw, quark_factory=StubQuark)

        self.assertEqual(exit_code, 1)
        self.assertEqual(seen, ["坏账号", "好账号"])

    def test_all_accounts_success_returns_zero(self):
        class StubQuark:
            def __init__(self, account):
                self.account = account

            def do_sign(self):
                return "✅ 签到成功"

        with redirect_stdout(io.StringIO()):
            exit_code = main(
                "user=账号;kps=1;sign=1;vcode=1;", quark_factory=StubQuark
            )
        self.assertEqual(exit_code, 0)

    def test_missing_environment_returns_configuration_error(self):
        with redirect_stdout(io.StringIO()):
            exit_code = main("")
        self.assertEqual(exit_code, 2)


class MainNotificationTests(unittest.TestCase):
    """签到结果推送与退出码之间的关系。"""

    RAW = "user=账号;kps=1;sign=1;vcode=1;"

    def setUp(self):
        patcher = mock.patch.dict(
            os.environ, {key: "" for key in notify.ENV_KEYS}, clear=False
        )
        patcher.start()
        self.addCleanup(patcher.stop)

        class StubQuark:
            def __init__(self, account):
                self.account = account

            def do_sign(self):
                return "✅ 签到成功"

        self.quark_factory = StubQuark

    def test_summary_is_pushed_after_signing(self):
        with mock.patch(
            "checkIn_Quark.notify.push",
            return_value=notify.PushResult(sent=("企业微信",)),
        ) as push_mock:
            with redirect_stdout(io.StringIO()):
                exit_code = main(self.RAW, quark_factory=self.quark_factory)

        self.assertEqual(exit_code, 0)
        push_mock.assert_called_once()
        title, content = push_mock.call_args.args
        self.assertEqual(title, "夸克自动签到")
        self.assertIn("共 1 个账号，成功 1，失败 0", content)
        self.assertIn("✅ 签到成功", content)

    def test_configuration_error_is_pushed_too(self):
        with mock.patch(
            "checkIn_Quark.notify.push", return_value=notify.PushResult()
        ) as push_mock:
            with redirect_stdout(io.StringIO()):
                exit_code = main("", quark_factory=self.quark_factory)

        self.assertEqual(exit_code, 2)
        title, content = push_mock.call_args.args
        self.assertEqual(title, "夸克自动签到（配置错误）")
        self.assertIn("COOKIE_QUARK", content)

    def test_push_failure_does_not_change_exit_code(self):
        failure = "企业微信（example.test）请求失败（HTTP 500）"
        with mock.patch(
            "checkIn_Quark.notify.push",
            return_value=notify.PushResult(sent=(), failures=(failure,)),
        ):
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                exit_code = main(self.RAW, quark_factory=self.quark_factory)

        self.assertEqual(exit_code, 0)
        self.assertIn(f"⚠️ 推送失败：{failure}", buffer.getvalue())

    def test_configured_channels_are_reported(self):
        with mock.patch(
            "checkIn_Quark.notify.channel_labels", return_value=["企业微信", "飞书"]
        ):
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                main(self.RAW, quark_factory=self.quark_factory)

        self.assertIn("已启用推送渠道：企业微信、飞书", buffer.getvalue())

    def test_no_channels_keeps_console_only_output(self):
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            exit_code = main(self.RAW, quark_factory=self.quark_factory)

        self.assertEqual(exit_code, 0)
        self.assertIn("未配置推送渠道", buffer.getvalue())

    def test_push_message_names_each_account_with_status(self):
        class StubQuark:
            def __init__(self, account):
                self.account = account

            def do_sign(self):
                if self.account["user"] == "坏账号":
                    raise QuarkAPIError("凭证失效")
                return "✅ 签到成功"

        raw = (
            "user=好账号;kps=1;sign=1;vcode=1;\n"
            "user=坏账号;kps=2;sign=2;vcode=2;"
        )
        with mock.patch(
            "checkIn_Quark.notify.push",
            return_value=notify.PushResult(sent=("企业微信",)),
        ) as push_mock:
            with redirect_stdout(io.StringIO()):
                exit_code = main(raw, quark_factory=StubQuark)

        self.assertEqual(exit_code, 1)
        title, content = push_mock.call_args.args
        self.assertEqual(title, "夸克自动签到（1 个账号失败）")
        self.assertIn("✅ 第 1 个账号（好账号）", content)
        self.assertIn("❌ 第 2 个账号（坏账号）", content)
        self.assertIn("共 2 个账号，成功 1，失败 1", content)
        self.assertIn("凭证失效", content)

    def test_invalid_entry_is_still_reported_by_user_note(self):
        with mock.patch(
            "checkIn_Quark.notify.push", return_value=notify.PushResult()
        ) as push_mock:
            with redirect_stdout(io.StringIO()):
                exit_code = main("user=坏配置;kps=1;", quark_factory=self.quark_factory)

        self.assertEqual(exit_code, 1)
        _, content = push_mock.call_args.args
        self.assertIn("❌ 第 1 个账号（坏配置）", content)
        self.assertIn("缺少必要参数", content)

    def test_account_without_user_note_falls_back_to_index(self):
        with mock.patch(
            "checkIn_Quark.notify.push", return_value=notify.PushResult()
        ) as push_mock:
            with redirect_stdout(io.StringIO()):
                main("kps=1;sign=1;vcode=1;", quark_factory=self.quark_factory)

        _, content = push_mock.call_args.args
        self.assertIn("✅ 第 1 个账号（账号1）", content)


if __name__ == "__main__":
    unittest.main()
