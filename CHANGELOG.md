# 更新日志

格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，版本号按 [语义化版本](https://semver.org/lang/zh-CN/)。

## [Unreleased]

## [1.0.0] - 2026-10-26

首个公开版本。合并了 `dsh-bili-plus` 与 `bili-autopilot`。

### 新增

- **三层解耦**：宿主半端（`index.js`）／B 站访问层（`python/mcp_server.py`）／本地模型引擎（`python/autopilot/`），各自能单独跑、单独换。
- **非阻塞入口** `bilisee_tick`：启动一轮立即返回 `run_id`，节奏交给宿主的 `dsh-schedule`。
- **双层自动续跑**：引擎内压缩记忆后重发「继续」，外层检测 `needs_continue` 后以 `--resume` 重新拉起。
- **上下文预算管理**：token、字符数、只输出散文、被 `num_predict` 截断，任一命中即压缩。
- **可视化设置页**：模型、上下文、温度、日限、配额、质量门槛都在页面改，下一轮生效。
- **五档内容安全闸**：`allow` / `warn` / `rewrite` / `block` / `no_reply`，取最严的一档。
- **写权限物理隔离**：未开启写权限时 Python 侧不注册写工具。
- **配置单一事实源**：页面、引擎、B 站层全从生效值派生；`GET /status` 逐项对账，不一致弹黄条并可 `POST /sync` 重下发。
- **全程可审计**：每轮一个 run 目录，含 `state.json` / `memory.md` / `thinking.md` / `trace.jsonl` / `digest.md` / `report.md`。
- 扫码登录、关键帧抽取、去重数据库、风控自动刹车。

### 移除

- `bilisee_run` / `bilisee_wait`：前者阻塞等结果，后者依赖前者。自动化驱动之后既没必要也有害。
- 设置页的「立即跑一轮」按钮：触发权归宿主。
- `POST /run` HTTP 路由：同上。

### 修复

- **引擎与页面配置不一致**：以前 `see-config.json` 只在保存时写，且写的是「本地旧值 + 本次改动」，宿主侧的键进不去，本地缺的键让引擎退回默认值——页面写「一天 6 条」，引擎按 12 条跑。现在引擎那份永远由生效值投影。
- **代理导致的假故障**：页面探测走 Node 正常，引擎走 Python httpx 继承了系统代理，回环请求变成 `502`。子进程注入 `NO_PROXY`，Ollama 客户端 `trust_env=False`。
- 旧草稿可被重发：发布前校验 `draft_id` 属于本轮。

[Unreleased]: https://github.com/aba3345/dsh-bili-see/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/aba3345/dsh-bili-see/releases/tag/v1.0.0
