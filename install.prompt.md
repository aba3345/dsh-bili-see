# 安装任务说明（给 Agent 看）

你正在安装 **dsh-bili-see**：https://github.com/aba3345/dsh-bili-see
一个跑在 DeepSeek Harness（DSH）上的本地插件，作用见仓库 README。

按下面五步做完，最后给用户一份不超过十行的结果汇报。

---

## 0. 先确认环境

```bash
node -v      # 需要 ≥ 20
python -V    # 需要 ≥ 3.10
```

任一条不满足就**停下来告诉用户**，不要自己装 Node / Python。

还要确认两个路径存在，记下来备用：

- `$DSH_HOME`（Windows 一般在 `C:\Users\<你>\.dsh`，macOS/Linux 是 `~/.dsh`）
- `$DSH_HOME/profiles/desktop/`

不存在说明这台机器没装 DSH，装完也没法加载 —— 直接告诉用户。

## 1. 拿代码、装依赖

装到用户指定目录；没指定就用 `~/tools/dsh-bili-see`。

```bash
git clone https://github.com/aba3345/dsh-bili-see.git <目标目录>
cd <目标目录>
python -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements.txt   # macOS/Linux: .venv/bin/python
```

注意：

- `mcp` 必须 **< 2**（2.x 把 `FastMCP` 改名了，装上会直接 import 失败）。
- 不要把 `.venv` 建在别处 —— 插件会先找仓库根的 `.venv`，再找家目录的。
- 装完验证一句：`.venv/Scripts/python.exe -c "import mcp, httpx, PIL, qrcode"`，没报错才算完。

## 2. 挂进 profile

编辑 `$DSH_HOME/profiles/desktop/package.json`，**只加键，别重排原有内容**：

```json
{
  "dependencies": {
    "dsh-bili-see": "link:<目标目录的绝对路径，用正斜杠>"
  },
  "dsh": {
    "profile": {
      "bundles": ["...原来的那些...", "dsh-bili-see"]
    }
  }
}
```

写坏了 DSH 会起不来，改之前先备份这个文件。

## 3. 打开定时能力

插件自己不排程，节奏靠 DSH 自带的 `@deepseek-ai/dsh-schedule`。在 profile 的
`cordis.patch.yml` **末尾**追加：

```yaml
- insert:
    - id: schedule
      name: "@deepseek-ai/dsh-schedule"
```

包已经在 DSH 的依赖树里，不需要 `npm install`。

## 4. 自检

```bash
node selfcheck.mjs
```

它按真实依赖链依次查：包内文件 → Python 依赖 → 家目录铺设 → Ollama → 本地模型 → B站 凭证。

- 全绿：进第 5 步。
- 有红项：**按输出里给的建议修**，然后重跑。修不动就带着原始输出问用户。
- `Ollama 未运行` 的修法是 `ollama serve`，不是重启 DSH。

## 5. 收尾

告诉用户：重启 DSH → 左侧打开「B站视频评论设置」→ 四个状态点全绿后扫码登录 →
**第一次跑保持「允许发评论」关闭**，确认它会读会判断再考虑开写权限。

---

## 红线

- **不要**改 `$DSH_HOME/bili-see/` 下的 `config.json`、`see-config.json`、
  `bili_credential.json`、`bili_plus.db` —— 那里有用户凭证和历史数据。
- **不要**开「允许发评论」，也不要用 `--write` 跑测试。发评论是对外动作，由用户自己决定。
- **不要**擅自 `git push` 或改仓库代码；装不上就报告，不要边装边改。
- 装完不要留后台进程。

## 汇报格式

```
装好了：<目标目录>
Python：<版本>　依赖：OK
profile：已加 link 依赖 + bundles
schedule：已插入（或：用户已有，跳过）
selfcheck：N/N 通过（没过的是：...）
下一步：重启 DSH → 打开「B站视频评论设置」→ 扫码登录（先别开写权限）
```
