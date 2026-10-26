# 贡献指南

这是个小项目，规矩也少：能跑、别夹带凭证、说清楚改了什么。

## 本地跑起来

```bash
git clone https://github.com/aba3345/dsh-bili-see.git
cd dsh-bili-see
python -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements.txt   # macOS/Linux 用 .venv/bin/python
node selfcheck.mjs
```

没有 `npm install` 这一步——本插件零 npm 依赖，`index.js` 是宿主直接 import 的原生 ESM。

## 提交前

```bash
node selfcheck.mjs                                            # 依赖链自检
node tests/test-apply.mjs                                     # 工具 + HTTP 路由
node tests/test-client.mjs                                    # 设置页渲染
PYTHONPATH=python/autopilot python tests/test_engine_v2.py     # 引擎单测
```

前四条里至少跑通你能跑的那些。改到引擎逻辑就补一条断言，别只改代码。

## 代码风格

- 纯 ESM，不加构建步骤，不引入 npm 运行时依赖。
- Python 侧保持 `async`，网络调用走 `httpx`，不要 `requests`。
- 中文注释没问题（这个仓库就是中文写的），但**注释解释「为什么」，不复述「做了什么」**。
- 改行为就顺手改 README 里对应的表；README 是这个项目的说明书，不是装饰。

## 绝对不要提交

`config.json`、`see-config.json`、`bili_credential.json`、`*.db`、`*.log`、`autopilot-home/`、`.venv/`、任何真实账号名或 UID。
`.gitignore` 已经挡住大部分，但提交前请自己 `git diff --cached` 看一眼——凭证泄露是补不回来的。

## PR

- 一个 PR 一件事。顺手格式化整个文件会让 review 变成猜谜。
- 描述里写清楚：**改了什么、为什么改、怎么验证的**。
- 带 UI 改动请附设置页截图。
- 新增内容安全规则请在 PR 里写出触发它的真实样例。

## 报告问题

用 [Issue 模板](https://github.com/aba3345/dsh-bili-see/issues/new/choose)。
贴 `duty.log` 或 `trace.jsonl` 之前**先删掉 cookie、UID、房间号、评论原文里的个人信息**。

**安全问题不要开公开 Issue**，用 GitHub 的 [私密漏洞报告](https://github.com/aba3345/dsh-bili-see/security/advisories/new)。
凭证泄露、任意代码执行、绕过写权限闸门、提示词注入导致越权发布——都算安全问题。

## 行为准则

参与即视为同意 [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md)。
