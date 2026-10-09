# ⭐️ 夸克网盘自动签到

![GitHub stars](https://img.shields.io/github/stars/Liu8Can/Quark_Auot_Check_In) ![GitHub forks](https://img.shields.io/github/forks/Liu8Can/Quark_Auot_Check_In) ![License](https://img.shields.io/github/license/Liu8Can/Quark_Auot_Check_In) ![Last Commit](https://img.shields.io/github/last-commit/Liu8Can/Quark_Auot_Check_In) ![GitHub Actions](https://github.com/Liu8Can/Quark_Auot_Check_In/actions/workflows/quark_signin.yml/badge.svg) ![CI](https://github.com/Liu8Can/Quark_Auot_Check_In/actions/workflows/ci.yml/badge.svg) ![认可linux.do](https://ld.xh.do/ld-badge.svg)

通过 GitHub Actions 自动完成夸克网盘每日签到并领取空间奖励，支持单账号和多账号。

## 🚀 功能

- 北京时间每天 **01:00** 执行签到，**05:00** 进行失败兜底（工作流使用 UTC cron：`0 17 * * *` 与 `0 21 * * *`）。
- 运行日志会打印当时的北京时间、计划槽位以及「今日第几次运行」，方便确认这是首次执行还是兜底重试。
- 当天所有账号成功后写入日期缓存，第二次任务自动跳过。
- 多账号逐个执行；某个账号失败不会阻断其他账号。
- 任何账号失败时工作流返回失败，并保留当天第二次重试机会。
- 签到结束后把汇总结果推送到企业微信、飞书、钉钉、群晖 Chat 等 11 种渠道。
- 签到失败（含部分账号失败、配置错误）时，企业微信、飞书、钉钉的推送消息自动 **@全员**；全部成功时消息不 @ 任何人。
- 每月在独立的 `heartbeat` 分支生成保活提交，不污染 `main` 历史。

## 📋 使用方法

### 1. Fork 并启用 Actions

Fork 本仓库后进入 **Actions** 页面。如果 GitHub 显示工作流尚未启用，请点击 **I understand my workflows, go ahead and enable them**。

工作流已经声明所需的最小权限：签到工作流仅使用 `contents: read`，保活工作流使用 `contents: write`。通常不需要手动把整个仓库的 Workflow permissions 改成读写权限；如果组织策略禁止保活分支写入，请联系组织管理员调整。

### 2. 获取签到参数

> **新版夸克 App 抓包说明**
>
> 近期版本的夸克 App 在部分 Android 环境下对 HTTPS 证书校验较严格。仅安装普通“用户 CA 证书”时，可能无法正常解密夸克的 HTTPS 请求。实体手机通常需要 Root 后将抓包 CA 安装为**系统证书**，配置相对麻烦。
>
> 因此目前更推荐使用 **MuMu 模拟器 + Root + ProxyPin 系统证书**完成抓包，不需要 Root 自己的实体手机。

#### 推荐方案：MuMu 模拟器 + ProxyPin

- [MuMu 模拟器官网](https://mumu.163.com/)
- [ProxyPin 官方 GitHub](https://github.com/wanghongenpin/proxypin)

操作步骤：

1. 安装并启动 **MuMu 模拟器**。
2. 在 MuMu 设置中开启 **Root 权限**。
3. 在模拟器中安装 **夸克 App** 和 **ProxyPin**。
4. 打开 ProxyPin，按照提示安装 HTTPS 抓包证书。
5. 将 ProxyPin 的 CA 证书安装为 Android **系统证书**，而不仅是普通用户证书。
6. 启动 ProxyPin 的 HTTPS 抓包，然后打开夸克 App。
7. 进入夸克网盘的**签到 / 领空间**页面。
8. 回到 ProxyPin，搜索请求域名：

```text
drive-m.quark.cn
```

重点找到类似下面的请求：

```text
https://drive-m.quark.cn/1/clouddrive/act/growth/reward?...
```

9. 复制该请求的**完整 Request URL**。

新版 URL 通常会包含较多参数，例如 `device_model`、`mt`、`ut`、`ds`、`xs`、`kps`、`sign`、`vcode` 等。本项目会从完整 URL 中自动解析签到所需的核心参数，因此**不需要手动拆解新版 URL**。

> **请原样复制完整 URL。** 不要手动修改其中的 `+`、`=`、`%xx` 等字符。新版参数中可能包含字面量 `+`，当前版本已经兼容这种格式。

推荐配置：

```text
user=张三; url=https://drive-m.quark.cn/1/clouddrive/act/growth/reward?...;
```

#### 旧格式仍然兼容

升级后**不会影响原有用户配置**。以下方式均继续支持：

**方式一：新版或旧版完整 URL**

只要 URL 中包含签到需要的 `kps`、`sign`、`vcode` 参数，都可以继续直接填写：

```text
user=张三; url=https://drive-m.quark.cn/...&kps=abcdefg&sign=hijklmn&vcode=111111111;
```

以前已经保存的旧链接无需为了格式变化重新改写；如果凭证本身仍然有效，可以继续使用。

**方式二：手动填写旧版参数**

原来的手填格式同样完全兼容：

```text
user=张三; kps=abcdefg; sign=hijklmn; vcode=111111111;
```

也就是说，已有用户可以保持原来的 Secret 不变；新用户则更推荐直接保存完整抓包 URL，减少复制和拆分参数时出错的可能。

#### 如果抓不到夸克请求

如果出现以下情况：

- ProxyPin 能抓到其他 App，但看不到夸克请求；
- 打开夸克后出现网络异常；
- ProxyPin 显示 SSL / TLS / Certificate 相关错误；
- 安装普通用户证书后仍然无法解密夸克 HTTPS 请求；

优先检查 ProxyPin CA 是否已经安装为**系统证书**。如果实体手机没有 Root，建议直接使用上面的 **MuMu + Root + ProxyPin** 方案。

> `kps`、`sign`、`vcode` 以及完整抓包 URL 都属于敏感账号凭证。不要提交到代码、Issue 或公开截图中，只应保存到自己仓库的 GitHub Actions Secret `COOKIE_QUARK`。如果怀疑凭证已经泄露，请重新获取参数并及时更新 Secret。

### 3. 配置 GitHub Secret

进入 Fork 仓库的 **Settings → Secrets and variables → Actions → New repository secret**：

- Name：`COOKIE_QUARK`
- Secret：粘贴上一步整理的账号配置

多账号可以用换行分隔：

```text
user=账号一; url=https://...;
user=账号二; url=https://...;
```

也可以使用 `&&` 分隔：

```text
user=账号一; url=https://...; && user=账号二; url=https://...;
```

### 4. 配置消息推送（可选）

签到结束后脚本会把每个账号的结果汇总成一条消息，推送到所有已配置的渠道。渠道由环境变量启用，**只配置需要的变量即可**，未配置的渠道不会发起任何请求。

这些变量同样填在 **Settings → Secrets and variables → Actions** 中（地址和密钥属于敏感信息，请使用 Secret；`NOTIFY_ENABLED`、`NOTIFY_TIMEOUT` 这类开关可以放在 Variable 里）。

| 渠道 | 变量 | 说明 |
| --- | --- | --- |
| 企业微信 | `WECOM_WEBHOOK` | 群机器人 Webhook 完整地址，发送**纯文本**消息（超长自动按 2048 字节截断） |
| 飞书 | `FEISHU_WEBHOOK`，可选 `FEISHU_SECRET` | 自定义机器人地址；开启“签名校验”时填写密钥 |
| 钉钉 | `DINGTALK_WEBHOOK`，可选 `DINGTALK_SECRET` | 自定义机器人地址；使用“加签”时填写密钥 |
| 群晖 Chat | `SYNOLOGY_CHAT_URL` | 群晖 Chat 的“传入 Webhook”完整地址（含 `token`） |
| Server 酱 | `SERVERCHAN_SENDKEY` | 兼容 `SCT` 与 `sctp` 开头的两种密钥 |
| PushPlus | `PUSHPLUS_TOKEN` | 使用 markdown 模板推送 |
| Bark | `BARK_URL`，或 `BARK_KEY` + 可选 `BARK_SERVER` | 例如 `https://api.day.app/设备Key` |
| Telegram | `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID` | |
| ntfy | `NTFY_TOPIC`，可选 `NTFY_SERVER`、`NTFY_TOKEN` | 默认服务器 `https://ntfy.sh` |
| Gotify | `GOTIFY_URL` + `GOTIFY_TOKEN` | |
| 通用 Webhook | `GENERIC_WEBHOOK`，可选 `GENERIC_WEBHOOK_HEADERS` | 以 `{"title", "content", "source"}` 形式 POST，签到失败时额外带 `mention_all: true`；认证头用 JSON 对象填写 |

两个通用开关：

- `NOTIFY_ENABLED`：填 `false` 可临时关闭全部推送。
- `NOTIFY_TIMEOUT`：单次推送请求的超时秒数，默认 `10`。

企业微信、飞书、钉钉、群晖 Chat、通用 Webhook 这几类变量支持**多行**，每行一个地址即可同时推送到多个群：

```text
https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxx
https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=yyy
```

推送行为说明：

- 推送内容按账号逐条列出，标题行使用 `COOKIE_QUARK` 中的 `user` 备注名，并用 ✅/❌ 标出每个账号的成败；没写 `user` 时回退为「账号1」「账号2」。
- 有账号失败时，消息标题会变成「夸克自动签到（N 个账号失败）」，方便在通知栏直接看到整体结果。
- 签到失败时（部分账号失败或 `COOKIE_QUARK` 配置错误），企业微信、飞书、钉钉的消息会自动 **@全员**；全部账号成功时消息保持安静、不 @ 任何人。个人推送类渠道（Server 酱、PushPlus、Bark、Telegram、ntfy、Gotify）没有 @全员概念，报文不变。
- 推送发生在所有账号处理完之后；签到成功、部分失败、以及 `COOKIE_QUARK` 配置错误时都会推送。
- 单个渠道失败只在日志里输出 `⚠️ 推送失败：…`，**不会**改变签到任务的成败，也不会因为推送失败而触发签到重试。
- 日志只显示渠道域名（如 `qyapi.weixin.qq.com`），不会打印完整 Webhook 地址或密钥。
- 同一天里“已签到跳过”的那次运行不会重复推送，避免收到两条相同结果。
- 消息超过渠道长度上限时会自动截断。

推送内容示例：

```text
夸克自动签到
共 2 个账号，成功 1，失败 1

✅ 第 1 个账号（张三）
88VIP
💾 网盘总容量：10.00 GB，签到累计容量：2.00 GB
✅ 签到成功 +1.00 GB，连签进度（3/7）

❌ 第 2 个账号（李四）
❌ 凭证失效
```

想先验证推送配置，可以在本地直接运行：

```bash
# Linux / macOS
WECOM_WEBHOOK='https://...' python notify.py --title 测试 --content 你好

# Windows PowerShell
$env:WECOM_WEBHOOK='https://...'; python notify.py --title 测试 --content 你好

# 只查看当前识别到了哪些渠道
python notify.py --list-channels
```

不想打扰真实群、只想看报文长什么样时，可以用仓库自带的假服务器（收到请求后会把请求头和请求体打印出来）：

```bash
python tools/fake_webhook_server.py --selftest   # 起服务 → 推送 → 校验报文 → 退出
python tools/fake_webhook_server.py --port 8808  # 只起服务，手动把渠道变量指向它
```

### 5. 手动测试

进入 **Actions → 夸克网盘每日签到 → Run workflow**。第一次运行会真实请求签到接口；当天全部账号已经成功后，再次运行将显示“今日已全部签到成功，跳过重复执行”。

**想忽略“今日已签到”标记、强制再跑一次**（例如验证推送是否配好），有两种方式：

1. 手动触发时把 **force** 勾选为 `true` —— 不删缓存，直接重跑签到步骤：

   **Actions → 夸克网盘每日签到 → Run workflow → force 选 true → Run workflow**

2. 删掉当天的缓存标记，再正常触发一次：

   **Settings → Actions → Caches**（或左侧 **Actions → Caches**）→ 找到 `quark-signed-YYYY-MM-DD`（北京时间当天日期）→ 删除 → 回到 Actions 手动运行。

   ```bash
   # 用 GitHub CLI 也可以（先把 owner/repo 换成你自己的）
   gh cache list -R godycg2/Quark_Auto_Check_In
   gh cache delete "quark-signed-2026-10-03" -R godycg2/Quark_Auto_Check_In

   # 或者用 API（需要带 actions:write 权限的 token）
   curl -X DELETE -H "Authorization: Bearer $GITHUB_TOKEN" \
     "https://api.github.com/repos/godycg2/Quark_Auto_Check_In/actions/caches?key=quark-signed-2026-10-03"
   ```

> 不用担心重复领奖：夸克接口本身是幂等的，当天已经签过会返回“✅ 今日已签到 +X”，脚本把它当作成功处理，不会重复领取，也不会报错。强制重跑只是让工作流再走一遍签到、推送流程。

## 🔁 执行与重试逻辑

1. 工作流按北京时间生成当天的缓存键，并在运行日志中打印本次运行对应的北京时间。
2. 同时打印「计划槽位」（01:00 / 05:00，取自触发本次运行的 cron）和「今日运行次数」。
3. 如果缓存命中（且手动触发时没有勾选 `force`），签到相关步骤全部跳过。
4. 如果没有命中，逐个处理 `COOKIE_QUARK` 中的账号。
5. 全部账号处理完成后，把汇总结果推送到所有已配置的渠道。
6. 所有账号成功或已签到时，保存当天成功标记。
7. 任一账号配置错误、凭证失效或接口异常时，工作流失败且不保存标记，05:00 会再次尝试；推送失败不影响这一判断。

> **今日运行次数**怎么来的：工作流把计数写在 `.cache/quark-run-count/count`，并用缓存 `quark-runs-<北京时间日期>-…` 保存，每次运行恢复上一次的计数再 +1。
> - 计数按**北京时间当天**累计，覆盖定时触发和手动 `workflow_dispatch` 的所有运行；手动强制重跑也会 +1。
> - 计数在“检出代码”之前保存，因为 `actions/checkout` 默认会 `git clean -ffdx`，否则工作区里的计数文件会被清掉。
> - 如果某次运行的缓存保存失败，下一次运行会退回从 1 开始计数，这只影响显示，不影响签到与去重逻辑（去重靠的是 `quark-signed-<日期>` 成功标记）。

## ❓ 常见问题

### 提示“缺少必要参数”

检查每个账号是否都包含 `kps`、`sign`、`vcode`，或者完整 URL 是否确实带有这三个查询参数。空行会自动忽略。

### 单账号正常，多账号失败

请确认账号之间使用换行或 `&&` 分隔，并且每个账号都是一套完整参数。新版脚本会继续处理后续账号，并在日志中明确指出失败的是第几个账号。

### 提示“获取成长信息失败”或“凭证失效”

通常表示抓取的参数已经过期或不完整，请重新抓取并更新 `COOKIE_QUARK`。接口临时异常也会让工作流失败，但不会写入当日成功缓存，因此仍会保留第二次重试机会。

### 定时任务没有准点运行

GitHub Actions 的计划任务可能延迟数分钟到数十分钟，这是平台调度机制导致的正常现象。工作流还会加入最多 60 秒的随机延迟。

### 收不到推送消息

先确认变量名拼写正确，并且配置在**实际运行工作流的那个仓库**里（Fork 之后需要在 Fork 仓库里配置）。然后可以在本地用 `python notify.py --list-channels` 查看脚本识别到了哪些渠道，再用 `python notify.py --title 测试 --content 你好` 推送一条测试消息，日志会给出具体原因（签名错误、Webhook 失效、网络超时等）。由于推送失败不会让签到任务失败，Actions 里需要查看日志中的 `⚠️ 推送失败` 行。

### 保活分支为什么每月被强制更新

`heartbeat` 是专门的孤儿分支，每月仅保留最新一条空提交，用于避免长期无活动的 Fork 被 GitHub 自动停用定时任务；它不会改动 `main`。

## 🔐 日志与密钥安全

GitHub Actions 的运行日志是公开可见的（Fork 仓库尤其如此），因此脚本对输出做了三层处理：

1. **不打印凭据**：日志里只会出现账号备注名、容量数字和渠道域名（如 `qyapi.weixin.qq.com`），不会出现 `kps`/`sign`/`vcode`、完整 Webhook 地址或 Token。
2. **备注名清洗**：`user=` 只用于显示，最长 32 字符；若其中出现 `kps=`、`token=`、`http://` 等字样（例如把 `;` 误写成 `&`，导致整串参数落进 `user` 字段），就直接放弃展示，显示为「账号1」，避免把凭据当成名字打印出来。
3. **统一脱敏**：脚本会把账号凭据、各推送渠道的地址与 Token 登记为敏感串，任何要输出的文本——包括第三方接口回显在错误信息里的内容——在打印和推送前都会把命中的敏感串替换成 `***`。

工作流本身只在 `env:` 中引用 Secrets，没有任何 `echo` 输出；GitHub 也会自动屏蔽 Secrets 的原文。

自查方法：

```bash
# 单元测试：覆盖 15 个脱敏 / 泄露场景（日志、推送报文、接口回显、URL 条目等）
python -m unittest discover -s tests -p "test_log_safety.py" -v

# 本地假服务器：默认隐藏 Authorization 头，需要查看原文时加 --show-secrets
python tools/fake_webhook_server.py --selftest
```

即便如此，仍建议：不要把凭据写进 `user=`，不要在 Issue 或截图里贴出 `COOKIE_QUARK`、Webhook 地址；一旦怀疑泄露，立即在夸克 App 退出登录并重新抓取，同时在对应平台重建机器人。

## ⚠️ 注意事项

- 本项目仅供学习交流，请勿用于非法用途。
- 推送 Webhook 地址和密钥同样具有操作权限，请只保存在仓库 Secrets 中，不要提交到代码、Issue 或公开截图。
- 夸克接口和参数可能随官方更新而变化；出现集中失效时请先查看 Issues。
- 频繁手动触发可能被服务限制，请谨慎操作。
- 本项目采用 MIT License。复制和分发时请保留原作者版权及许可声明。

本项目基于 [BNDou/Auto_Check_In](https://github.com/BNDou/Auto_Check_In) 的夸克签到功能修改而来。

## 🙏 贡献者

感谢以下贡献者对项目的改进：

- [@Spectrollay](https://github.com/Spectrollay) — 优化签到工作流与自动化逻辑（[#1](https://github.com/Liu8Can/Quark_Auot_Check_In/pull/1)）
- [@haozihong](https://github.com/haozihong) — 将保活提交迁移至独立分支，保持主分支历史整洁（[#4](https://github.com/Liu8Can/Quark_Auot_Check_In/pull/4)）
- [@HSSkyBoy](https://github.com/HSSkyBoy) — 优化签到流程结构与按日期缓存机制（[#16](https://github.com/Liu8Can/Quark_Auot_Check_In/pull/16)）

📧 联系邮箱：[liucan01234@gmail.com](mailto:liucan01234@gmail.com)

欢迎提交 Issue、PR 和 Star 支持项目发展。
