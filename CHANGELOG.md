# 更新日志

格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，版本号按 [语义化版本](https://semver.org/lang/zh-CN/)。

## [Unreleased]

### 修复

- `bilisee_health` 不返回额度：登录后一并带出「今日已发 / 剩余条数」，调用方不用再问一次 `status`。
- 仓库根的 `.venv` 不在探测链里：按 README 在仓库根装好依赖，插件却找不到，只能报「Python 未就绪」。
- 人设保存可能落进包内：家目录还没有 `persona.yaml` 时，保存会写进包内的模板文件，升级即丢。
- 读字幕的额度断言太松：测试里 `before` 取到后从未使用，额度回归不会被发现。

### 变更

- 默认人设换成完整可编辑版本，`account_name` 留空，不含任何真实账号信息。
- 删除 `python/autopilot/README.md`（旧版内部文档，与主 README 的说法冲突）。
- 清掉 11 处未使用导入和 1 行死代码；Python 探测链不再包含作者本机路径。
- Python 下限修正为 3.10（`mcp` 1.x 的要求），README 与 badge 同步。

## [1.0.0] - 2026-10-26

首个公开版本

[Unreleased]: https://github.com/aba3345/dsh-bili-see/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/aba3345/dsh-bili-see/releases/tag/v1.0.0
