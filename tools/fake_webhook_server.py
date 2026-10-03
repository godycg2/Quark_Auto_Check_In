"""本地假 Webhook 服务器，用来在不打扰真实群机器人的前提下验证推送报文。

用法：

1) 只起服务，手动把渠道变量指向它：

    python tools/fake_webhook_server.py --port 8808

   PowerShell 示例：

    $env:WECOM_WEBHOOK='http://127.0.0.1:8808/wecom'
    $env:SYNOLOGY_CHAT_URL='http://127.0.0.1:8808/synology?token=demo'
    python notify.py --title 测试 --content 你好

   服务会把每个渠道收到的请求头、请求体原样打印出来，
   于是可以在不配置任何真实机器人的情况下确认报文格式。

2) 一键自检（自动起服务 + 推送 + 校验 + 退出）：

    python tools/fake_webhook_server.py --selftest
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import notify  # noqa: E402

RECEIVED: list[dict] = []
SHOW_SECRETS = False
SENSITIVE_HEADERS = ("authorization", "token", "secret", "key", "cookie")


def mask_header(name: str, value: str) -> str:
    """默认隐藏认证类请求头的值，需要时用 --show-secrets 显示。"""

    if SHOW_SECRETS or not value:
        return value
    lowered = name.lower()
    if any(hint in lowered for hint in SENSITIVE_HEADERS):
        return f"***（已隐藏，共 {len(value)} 字符，--show-secrets 可显示）"
    return value


OK_BODY = json.dumps(
    {
        "errcode": 0,
        "errmsg": "ok",
        "code": 0,
        "msg": "success",
        "StatusCode": 0,
        "StatusMessage": "success",
        "id": 1,
        "message": "success",
        "success": True,
        "ok": True,
    },
    ensure_ascii=False,
).encode("utf-8")


def format_body(body: str) -> str:
    """尽量把请求体格式化成人能读的样子。"""

    if not body:
        return "(空请求体)"
    try:
        return json.dumps(json.loads(body), ensure_ascii=False, indent=2)
    except ValueError:
        pass
    fields = parse_qs(body, keep_blank_values=True)
    if "payload" in fields:
        try:
            pretty = json.dumps(
                json.loads(fields["payload"][0]), ensure_ascii=False, indent=2
            )
        except ValueError:
            pretty = fields["payload"][0]
        return f"payload=\n{pretty}"
    return body


class Handler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802 - http.server 约定
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length).decode("utf-8", "replace")
        RECEIVED.append(
            {
                "path": self.path,
                "content_type": self.headers.get("Content-Type", ""),
                "body": body,
            }
        )
        print(f"\n📥 {self.command} {self.path}")
        print(f"   Content-Type: {self.headers.get('Content-Type', '')}")
        authorization = self.headers.get("Authorization", "")
        print(f"   Authorization: {mask_header('Authorization', authorization) or '(无)'}")
        print(f"   Title: {self.headers.get('Title', '(无)')}")
        for line in format_body(body).splitlines():
            print(f"   | {line}")
        sys.stdout.flush()

        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(OK_BODY)))
        self.end_headers()
        self.wfile.write(OK_BODY)

    def log_message(self, *args) -> None:  # 关掉默认的访问日志
        pass


def make_server(host: str, port: int) -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), Handler)


def start_server(host: str, port: int) -> ThreadingHTTPServer:
    """在后台线程里启动服务，返回服务器对象。"""

    # 请求处理线程也在打印，先保证 GBK 控制台不会因为 emoji 抛异常。
    notify.relax_console_encoding()
    server = make_server(host, port)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def run_selftest() -> int:
    """起服务、推送一条消息、逐项校验报文，结束返回退出码。"""

    notify.relax_console_encoding()
    os.environ["NO_PROXY"] = "127.0.0.1,localhost"
    os.environ["no_proxy"] = "127.0.0.1,localhost"

    server = start_server("127.0.0.1", 0)
    base = f"http://127.0.0.1:{server.server_address[1]}"
    env = {
        "WECOM_WEBHOOK": f"{base}/wecom?key=demo",
        "FEISHU_WEBHOOK": f"{base}/feishu/hook-id",
        "DINGTALK_WEBHOOK": f"{base}/dingtalk?access_token=demo",
        "SYNOLOGY_CHAT_URL": f"{base}/synology?token=demo",
        "GENERIC_WEBHOOK": f"{base}/generic",
    }
    print(f"假服务器已启动：{base}")
    print("推送一条测试消息，检查各渠道收到的报文：")

    result = notify.push(
        "夸克自动签到",
        "共 2 个账号，成功 2，失败 0\n\n🙍🏻‍♂️ 第 1 个账号\n✅ 签到成功 +1.00 GB",
        env=env,
    )

    by_path = {item["path"].split("?")[0]: item for item in RECEIVED}
    checks = [
        ("5 个渠道各收到 1 次请求", len(RECEIVED) == 5),
        (
            "企业微信：markdown + 标题",
            json.loads(by_path["/wecom"]["body"])["msgtype"] == "markdown",
        ),
        (
            "飞书：text 消息",
            json.loads(by_path["/feishu/hook-id"]["body"])["msg_type"] == "text",
        ),
        (
            "钉钉：markdown + title 字段",
            json.loads(by_path["/dingtalk"]["body"])["markdown"]["title"]
            == "夸克自动签到",
        ),
        (
            "群晖 Chat：表单 payload 里的 text",
            "payload=" in by_path["/synology"]["body"]
            and "签到成功"
            in json.loads(
                parse_qs(by_path["/synology"]["body"])["payload"][0]
            )["text"],
        ),
        (
            "通用 Webhook：JSON 结构",
            sorted(json.loads(by_path["/generic"]["body"]))
            == ["content", "source", "title"],
        ),
        ("推送结果无失败", not result.failures),
    ]

    print("\n---------- 自检结果 ----------")
    failed = False
    for name, ok in checks:
        print(f"[{'PASS' if ok else 'FAIL'}] {name}")
        failed = failed or not ok
    server.shutdown()
    server.server_close()
    if failed:
        print("\n自检失败：报文格式与预期不一致。")
        return 1
    print("\n自检通过：推送报文格式正确。")
    return 0


def main(argv: list[str] | None = None) -> int:
    notify.relax_console_encoding()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1", help="监听地址")
    parser.add_argument("--port", type=int, default=8808, help="监听端口")
    parser.add_argument(
        "--selftest", action="store_true", help="自动起服务并校验一次推送"
    )
    parser.add_argument(
        "--show-secrets",
        action="store_true",
        help="打印认证类请求头的原文（默认隐藏）",
    )
    args = parser.parse_args(argv)

    global SHOW_SECRETS
    SHOW_SECRETS = args.show_secrets

    if args.selftest:
        return run_selftest()

    os.environ.setdefault("NO_PROXY", "127.0.0.1,localhost")
    os.environ.setdefault("no_proxy", "127.0.0.1,localhost")
    server = make_server(args.host, args.port)
    base = f"http://{args.host}:{server.server_address[1]}"
    print(f"假 Webhook 服务器已启动：{base}")
    print("把渠道变量指向它，例如：")
    print(f"  $env:WECOM_WEBHOOK='{base}/wecom'")
    print(f"  $env:SYNOLOGY_CHAT_URL='{base}/synology?token=demo'")
    print("然后运行：python notify.py --title 测试 --content 你好")
    print("按 Ctrl+C 退出。")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已退出。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
