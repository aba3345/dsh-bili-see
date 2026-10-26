<div align="center">

# dsh-bili-see

**把本地小模型接进 B 站：让它自己挑视频、读字幕、看热评、判断值不值得说一句，再把评论发出去。**

[![license](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![node](https://img.shields.io/badge/node-%E2%89%A520-339933?logo=node.js&logoColor=white)](package.json)
[![python](https://img.shields.io/badge/python-3.9%2B-3776AB?logo=python&logoColor=white)](requirements.txt)
[![deps](https://img.shields.io/badge/npm%20deps-0-brightgreen)](package.json)
[![for fun](https://img.shields.io/badge/purpose-%E5%A8%B1%E4%B9%90-ff69b4)](#免责声明)

一个跑在 [DeepSeek Harness](https://github.com/deepseek-ai) 上的插件。

</div>

![dsh-bili-see 漫画：50M 预算、4B 小模型干活、最后余额 49.5M](docs/assets/comic-bili-see.jpg)

四格漫画就是这个项目的结构：**云端模型只负责说话，本地模型负责跑腿。**

---

## 快速开始

**1. 你需要**：DSH（支持 `dsh.bundle` 与 `settings`）、Node ≥ 20、Python ≥ 3.9、[Ollama](https://ollama.com) 和一个能干活的工具调用模型（4B 起，7B 更稳）。

```bash
git clone https://github.com/aba3345/dsh-bili-see.git
cd dsh-bili-see

# 1. Python 依赖（mcp 必须 < 2，2.x 把 FastMCP 改名了，装上会直接 import 失败）
python -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements.txt   # macOS/Linux: .venv/bin/python
```

**2. 挂进 profile**：编辑 `$DSH_HOME/profiles/desktop/package.json`，加一条 `link:` 依赖并列进 bundles。

```json
{
  "dependencies": { "dsh-bili-see": "link:E:/tools/dsh-bili-see" },
  "dsh": { "profile": { "bundles": ["...", "dsh-bili-see"] } }
}
```

**3. 打开定时能力**：在 profile 的 `cordis.patch.yml` 末尾加一条（包就在 DSH 依赖树里，不用 npm install）：

```yaml
- insert:
    - id: schedule
      name: "@deepseek-ai/dsh-schedule"
```

**4. 重启 DSH**，左侧出现「B站视频评论设置」。先跑 `node selfcheck.mjs`，它按真实依赖链依次检查：包内文件 → Python 依赖 → 家目录铺设 → Ollama → 模型 → 凭证。

**5.** 设置页四个状态点全绿后扫码登录。**第一次跑请保持「允许发评论」关闭**，确认它会读会判断，再决定开不开写权限。

让宿主建定时任务，只需要对模型说一句：

> 帮我建一个定时任务：每 24 小时调用 `bilisee_tick` 启动一轮 B 站巡航，不要等它跑完；
> 稍后用 `bilisee_summary` 取结论向我简短汇报。

---

## 配置

最常改的几个（其余都在设置页里）：

| 项 | 默认 | 说明 |
| --- | --- | --- |
| 模型 | `qwen3.5:4b` | 换成你 `ollama list` 里任意支持工具调用的模型 |
| 温度 | `0.35` | 写评论 0.3~0.5 最像一个正常人 |
| 一天最多浏览 | `6` | 跨轮累计的硬闸，用完当天不开新视频 |
| 每轮看几条 | `2` | 每条 40~90 秒，2 条比较舒服 |
| 只看最近几天 | `2` | 填 `0` 表示不限新旧 |
| 每日发布上限 / 最小间隔 | `3` / `60` 分钟 | 与冷却、去重共同生效 |
| 允许发评论 | 关 | 总开关，关闭时写工具不注册 |
| 内容配额 | `AI科技 / AI二创 / 其他` | 一行一条；没填满的额度变成「自主浏览」，从热门里挑非 AI 视频补位 |

人设模板在 `config/persona.yaml`。**自定义请放 `$DSH_HOME/bili-see/persona.yaml`**，优先级更高，也不会跟着代码提交出去。

---

## 安全与边界

- **两道写闸**：设置页开关 → 环境变量 `BILI_PLUS_WRITE`。任一为假，写工具不注册。
- **发布前重跑** `safety_check` + `style_check`（文案可能被人改过）；`draft_id` 必须属于本轮。
- **限流**：每日上限 + 最小间隔 + 同视频同楼去重 + 同 UP 主 7 天去重。
- 政治、经济、社会站队、色情、血腥 → `no_reply`；辱骂、外链、冒充真人经历 → `block`。

> ⚠️ **自动化互动可能违反 B 站用户协议与社区规则。** 限流、折叠、禁言、封号的风险由使用者自行承担。
> 项目面向个人学习：研究小模型如何在有限上下文里做多步决策、如何被安全策略约束。
> 请勿用于刷量、营销或任何形式的虚假互动；每一条发出去的评论都由你负责。

数据都在 `$DSH_HOME/bili-see/`（`config.json`、`see-config.json`、`bili_credential.json`、`bili_plus.db`、`autopilot-home/runs/<run_id>/`）。升级不丢凭证和数据库，删掉等于恢复出厂设置。

---


## 开发

```bash
node selfcheck.mjs                   # 不启动宿主也能跑的自检
node tests/test-apply.mjs            # 假宿主跑 apply()：工具 + HTTP 路由
node tests/test-client.mjs           # 真渲染一遍设置页，检查文案
node tests/test-tool-run.mjs         # 全链路：tick → 轮询 → summary
PYTHONPATH=python/autopilot python tests/test_engine_v2.py   # 引擎单测（84 项，不联网不跑模型）
```

`tests/workbuddy-live.mjs` 只用本机 Ollama，加 `--write` 才会真发。欢迎 PR，见 [CONTRIBUTING.md](CONTRIBUTING.md)。

---

## 已知限制

4B 级别模型会犯错（重复调工具、把工具返回值当用户说话、写坏 JSON），代码里有一批兜底但**不能保证每轮成功**；只支持单账号；关键帧依赖视频可访问性，无 `cid` 或已下架时自动降级；Windows 验证最充分，macOS / Linux 的 Python 探测路径已覆盖但未经长时间实跑。

---

## 免责声明

本项目与哔哩哔哩（bilibili）及 DeepSeek 官方无任何关联，未获授权或背书。所有接口调用基于公开 Web 端行为，可能随平台变更失效。
它是一个**娱乐 / 学习性质**的作品：作者不承诺可用性，也不对使用本软件产生的任何后果负责。请自行遵守所在地法律法规与平台用户协议。

[MIT](LICENSE) © 2026 dsh-bili-see contributors
