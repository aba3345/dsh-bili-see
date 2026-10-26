/**
 * tests/run-engine.mjs —— 不开 DSH，直接用插件的 McpClient 拉一轮本地模型
 * ============================================================================
 * 它复刻的是 index.js 里 `apEnv()` 的那套环境变量，所以跑通就等于
 * 「宿主 → 引擎 MCP → B站 MCP → B站」整条链路是通的。
 *
 * 用法：
 *   node tests/run-engine.mjs              # 只读（不发评论）
 *   node tests/run-engine.mjs --rehearse   # 写入 + 预演（建草稿但自动清理）
 *   node tests/run-engine.mjs --write      # 真发（会消耗账号额度）
 * ============================================================================
 */
import { join } from 'node:path'
import { existsSync } from 'node:fs'
import { McpClient } from '../lib/mcp-client.mjs'
import { PKG_ROOT, homeDir, dshHome, ensureDirs, syncPythonFiles, readJson, writeJson } from '../lib/state.mjs'
import { findPython, installHint } from '../lib/python-env.mjs'

const args = process.argv.slice(2)
const write = args.includes('--write') || args.includes('--rehearse')
const dryRun = !args.includes('--write')

const ctxStub = { get: () => undefined }
const HOME = homeDir(ctxStub)
const DIRS = ensureDirs(HOME)
syncPythonFiles(HOME)

const py = findPython({ dshHome: dshHome(ctxStub), log: (m) => console.log(`· ${m}`) })
if (!py?.hasDeps) {
  console.error(`\n没有可用的 Python。\n${installHint(py?.path)}\n`)
  process.exit(1)
}

const cfgFile = join(HOME, 'config.json')
const cfg = { ...{ model: 'qwen3.5:4b-32k', ollamaUrl: 'http://127.0.0.1:11434', numCtx: 32768, temperature: 0.35, maxSteps: 40, numMinutes: 12, videosPerRun: 1, autoContinue: true, autoContinueMax: 2 }, ...readJson(cfgFile, {}) }

// 复刻 index.js#engineConfig：设置页的驼峰 → 引擎的 snake_case
writeJson(join(HOME, 'see-config.json'), {
  model: cfg.model,
  ollama_url: String(cfg.ollamaUrl).replace(/\/+$/, ''),
  num_ctx: Number(cfg.numCtx) || 32768,
  num_predict: Number(cfg.numPredict) || 1200,
  temperature: Number(cfg.temperature) || 0.35,
  max_steps: Number(cfg.maxSteps) || 40,
  max_minutes: Number(cfg.numMinutes) || 20,
  auto_continue: Boolean(cfg.autoContinue),
  auto_continue_max: Number(cfg.autoContinueMax) || 2,
  max_videos: Number(cfg.videosPerRun) || 1,
  write,
  dry_run: dryRun,
})

const env = {
  BILI_AUTOPILOT_PYTHON: py.path,
  BILI_AUTOPILOT_HOME: join(HOME, 'autopilot-home'),
  BILI_AUTOPILOT_CONFIG_FILE: join(HOME, 'see-config.json'),
  BILI_AUTOPILOT_ALLOW_WRITE: write ? '1' : '0',
  BILI_AUTOPILOT_MODEL: cfg.model,
  OLLAMA_HOST: cfg.ollamaUrl,
  BILI_PLUS_HOME: HOME,
  DSH_HOME: dshHome(ctxStub),
  PYTHONPATH: join(HOME, 'autopilot'),
}

console.log(`\n=== run-engine（write=${write} dry_run=${dryRun}）===`)
console.log(`home=${HOME}\nmodel=${cfg.model}\n`)

const client = new McpClient({
  command: py.path,
  args: ['-m', 'bili_autopilot.server'],
  cwd: join(HOME, 'autopilot'),
  env,
  log: (m) => console.log(`  [engine] ${m}`),
})

try {
  await client.start()
  const tools = await client.listTools()
  console.log(`引擎工具：${tools.map((t) => t.name).join(', ')}\n`)

  const health = await client.call('health', {}, 60_000)
  console.log('health:', JSON.stringify(health.json?.checks || health.json || health.text).slice(0, 300), '\n')

  console.log('开始跑一轮（阻塞，等本地模型跑完）…\n')
  const started = Date.now()
  const r = await client.call('run', {
    write,
    dry_run: dryRun,
    max_videos: Number(cfg.videosPerRun) || 1,
    max_comments: write ? 1 : 0,
    max_minutes: Number(cfg.numMinutes) || 12,
    wait_seconds: 1200,
  }, 1_320_000)

  const j = r.json || {}
  console.log(`耗时 ${Math.round((Date.now() - started) / 1000)}s`)
  console.log(`status=${j.status} steps=${j.steps} videos=${j.videos_seen} posted=${j.comments_posted} auto=${j.auto_continues}`)
  if (j.last_error) console.log(`last_error=${j.last_error}`)
  console.log('\n--- digest ---')
  console.log((j.digest || j.raw || '').slice(0, 3000))
  console.log('--- /digest ---\n')
  console.log('files:', JSON.stringify(j.files || {}, null, 1))
  process.exitCode = j.status === 'completed' ? 0 : 2
} catch (e) {
  console.error('失败：', e?.message || e)
  process.exitCode = 1
} finally {
  client.kill()
}
