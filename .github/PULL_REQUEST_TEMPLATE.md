## 改了什么

<!-- 一两句话说清楚。 -->

## 为什么

<!-- 解决了什么处境。如果是 bug，说清触发条件。 -->

## 怎么验证的

- [ ] `node selfcheck.mjs`
- [ ] `node tests/test-apply.mjs`
- [ ] `PYTHONPATH=python/autopilot python tests/test_engine_v2.py`
- [ ] 真跑了一轮（`node tests/test-tool-run.mjs`，只读模式）
- [ ] 真发过评论（罕见，请说明用的是测试号）

## 提交前自查

- [ ] 没夹带 `config.json` / `see-config.json` / `bili_credential.json` / `*.db` / `*.log`
- [ ] 没夹带真实账号名、UID、cookie
- [ ] 改了行为，README 里对应的表也改了
- [ ] 新增安全规则的同时补了样例
