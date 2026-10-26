# bili-autopilot

让本地 `qwen3.5:4b` 自己去刷 B 站、读懂视频、判断要不要评论、写并发评论；
DeepSeek / OpenCode 只负责调用一次 MCP 工具，然后读结果写汇报。

```
DeepSeek (贵, 少说话)
   │  bili-autopilot_run()          ← 一次阻塞调用，返回 digest
   ▼
MCP facade (server.py)              ← 只做编排，不思考
   │  spawn detached runner
   ▼
runner + agent (本地进程)            ← 静默运行，DeepSeek 完全不知情
   │  上下文爆了 / 进程死了 → 自动"继续"
   ▼
qwen3.5:4b (Ollama)  ──MCP stdio──▶  dsh-bili-plus  ──▶  B站
```

## 为什么长这样

三个约束决定的：

1. **4B 模型上下文很短**（32k，实际可用约 22k 字符）。它必须能在上下文耗尽时
   带着记忆重启，而不是从头再来 —— 所以有 `memory.json` + `continue_prompt`。
2. **DeepSeek 的 token 很贵**。所以它只在两个时刻出现：发起调用、写汇报。
   中间的几十次模型调用、工具调用、失败重试全部在本地完成，DeepSeek 看不见。
3. **发评论是不可逆的对外动作**。所以有三道闸：`BILI_AUTOPILOT_ALLOW_WRITE`
   环境变量、`write=true` 参数、`dry_run` 预演 + 插件自己的安全审/风格审/额度限制。

## 安装

```bash
cd bili-autopilot
python -m venv .venv
.venv/Scripts/python.exe -m pip install -e .
```

> 依赖固定 `mcp>=1.30,<2`。mcp 2.x 把 `FastMCP` 改名成 `MCPServer`，dsh-bili-plus
> 自己用的也是 1.x，两边必须一致。

## 配置（OpenCode）

`~/.config/opencode/opencode.json`：

```json
{
  "mcp": {
    "bili-autopilot": {
      "type": "local",
      "command": [
        "C:/Users/<you>/Documents/默认项目/bili-autopilot/.venv/Scripts/python.exe",
        "-m", "bili_autopilot.server"
      ],
      "environment": {
        "BILI_AUTOPILOT_PYTHON": "C:/Users/<you>/Documents/默认项目/bili-autopilot/.venv/Scripts/python.exe",
        "BILI_PLUS_HOME": "C:/Users/<you>/.dsh/bili-plus",
        "BILI_AUTOPILOT_ALLOW_WRITE": "1",
        "PYTHONIOENCODING": "utf-8",
        "PYTHONUTF8": "1"
      },
      "enabled": true
    }
  }
}
```

`BILI_AUTOPILOT_ALLOW_WRITE=1` 只是把写权限**打开到可以传 `write=true`**；不传
`write=true` 依然只读。想彻底禁写就设为 `0`。

## 配置（WorkBuddy）

`~/.workbuddy/mcp.json`，格式同上（`mcpServers` 键）。改完需要在连接器管理页
右上角的自定义连接器里点「信任」，重启会话后生效。

## 用法

DeepSeek 侧只需要一次调用：

```
bili-autopilot_run(write=false)                          # 只读
bili-autopilot_run(write=true, dry_run=false, max_videos=1)  # 真发
```

CLI 侧可以直接跑：

```bash
.venv/Scripts/python.exe -m bili_autopilot health
.venv/Scripts/python.exe -m bili_autopilot run --max-videos 1
.venv/Scripts/python.exe -m bili_autopilot run --write --max-videos 1
.venv/Scripts/python.exe -m bili_autopilot list
.venv/Scripts/python.exe -m bili_autopilot digest <run_id>
```

## 工具

| 工具 | 什么时候用 |
| --- | --- |
| `run` | **正常流程只用这个**。阻塞到结束，返回 digest。 |
| `wait` | 上一次 `run` 超时还没结束时续等。 |
| `status` | 只在排查问题时用。 |
| `digest` | 重新读某轮结果。 |
| `trace` | digest 不够用时看事件流。 |
| `cancel` | 取消。 |
| `list_runs` | 列历史。 |
| `continue_run` | 自动续跑用尽后手动再试。 |
| `health` | 环境自检。 |

## 自动"继续"的两层

**第一层 —— 本地模型自己续**（`agent._reset_context`）

触发条件：prompt token 超过 `num_ctx * 0.72`、字符数超过 `num_ctx * 0.68`、
模型只输出散文不调工具、输出被 `num_predict` 截断、Ollama 报错。

动作：`memory.compact()` 压出结构化记忆（已看过哪些视频、判断、评论、发现、
异常），重建成 `system + 继续指令 + 记忆`，最多 `max_continues=8` 次。

**第二层 —— MCP 外层续**（`server._wait_and_continue`）

触发条件：run 结束时状态是 `needs_continue`（第一层用完了）或 `failed`（进程死了）。

动作：重新 spawn `runner --resume`，复用已有 candidates 和 memory，跳过已 note 的
视频，最多 `auto_continue_max=2` 次。同一个 `last_error` 连续出现两次就停 ——
那说明是环境问题，再重启也没用。

**两层都是自动的**，DeepSeek 看不到也管不着。这是「DeepSeek 保持静默」的关键。

## 数据落点

```
~/.bili-autopilot/runs/<run_id>/
  state.json        运行状态机
  config.json       本轮实际生效配置
  memory.json       结构化记忆（机器读）
  memory.md         记忆（人读 / 给 DeepSeek 看）
  thinking.md       本地模型思维链全文
  trace.jsonl       每个事件（模型输出、工具调用、参数、结果）
  digest.md         给 DeepSeek 的紧凑摘要 ← MCP 返回这个
  report.md         给人看的完整报告
  runner.log        runner 进程日志
```

## 幂等与安全

- 候选只来自 `watch_mids` / `keywords` / `popular`，模型不能编 BV 号。
- 一条视频只能 note 一次；`seen=true` 的视频可以看但不能评论。
- 评论必须 20~50 字；先过 `safety_check` 和 `style_check` 才允许落草稿。
- 发送必须 `dry_run=true` 预演成功后才允许 `dry_run=false`。
- `draft_id` 必须是**本轮**创建的，防止拿旧草稿重发。
- 同一条视频本轮只建一次草稿（重复调用返回 `reused=true`），防止 4B 模型
  反复重规划时堆出 draft 33/34/35…。
- 预演结束后自动软删草稿，不污染插件的待批列表。
- 同一工具 + 同一参数重复 5 次自动收尾；预演模式下反复尝试真发 2 次自动收尾。
  这两条是给 4B 模型防死循环的保险，不是可选项。
- 插件报风控/暂停时立即关闭写权限，不再尝试发送。

## 测试

```bash
# 1. 桥接层：能不能连上 dsh-bili-plus、工具白名单对不对
.venv/Scripts/python.exe tests/smoke_bridge.py

# 2. MCP 层：像 DeepSeek 一样列工具 + 自检
.venv/Scripts/python.exe tests/mcp_client_test.py

# 3. MCP 层：完整只读跑一轮（不碰账号）
.venv/Scripts/python.exe tests/mcp_client_test.py --run --videos 1

# 4. MCP 层：完整预演（建草稿 + dry_run 预演 + 自动清理，不真发）
.venv/Scripts/python.exe tests/mcp_client_test.py --run --rehearse --videos 1

# 5. 自动续跑逻辑的单元测试
.venv/Scripts/python.exe tests/test_auto_continue.py

# 6. 真发（会消耗账号额度，慎用）
.venv/Scripts/python.exe tests/mcp_client_test.py --run --write --videos 1
```

实测（qwen3.5:4b-32k / 单条视频）：
- 只读：5~6 步、42 秒、本地 token `prompt≈25k eval≈1.3k`
- 预演：7 步、47 秒、1 条草稿（自动清理）、本地 token `prompt≈5k eval≈0.9k`
- **真发**：12 步、92 秒、`rpid=321852656112` 发送成功、24h 计数 3→4、额度 2→1。
  其中 5 步花在把评论压到 20~50 字（4B 数不准字数）。`verify_posted` 返回
  `not_found`（疑似压审），这与「没发出去」不是一回事。
- DeepSeek 侧：始终 1 次工具调用 + 1 段汇报
