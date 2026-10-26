/**
 * tests/live-task.mjs —— 真跑一段固定时长的巡航（会真发评论）
 * ============================================================================
 * 它在给定时间窗内反复调引擎的 run，直到：
 *   · 时间用完
 *   · 或发够 --max-comments 条
 *   · 或账号额度用完
 *
 * 结束后把每一轮看过的视频、发出的评论、rpid、耗时汇总成 JSON 打到 stdout。
 *
 * 用法：
 *   node tests/live-task.mjs --minutes 10 --max-comments 5 --max-posts-per-day 10
 * ============================================================================
 */
import { join } from 'node:path'
import { existsSync, readFileSync } from 'node:fs'
import { McpClient } from '../lib/mcp-client.mjs'
import { homeDir, dshHome, ensureDirs, syncPythonFiles, readJson, writeJson } from '../lib/state.mjs'
import { findPython, installHint } from '../lib/python-env.mjs'

const argv = process.argv.slice(2)
const arg = (k, d) => {
  const i = argv.indexOf(`--${k}`)
  return i >= 0 && argv[i + 1] !== undefined ? argv[i + 1] : d
}
const MINUTES = Number(arg('minutes', 10))
const MAX_COMMENTS = Number(arg('max-comments', 5))
const MAX_POSTS_PER_DAY = Number(arg('max-posts-per-day', 10))
const MAX_VIDEOS = Number(arg('max-videos', 2))
const TASK = arg('task', '自由刷 B站：挑你感兴趣的视频看，理解内容后，在合适的视频下发一条评论。不用请示，直接发。')

const ctxStub = { get: () => undefined }
const HOME = homeDir(ctxStub)
ensureDirs(HOME)
syncPythonFiles(HOME)

const py = findPython({ dshHome: dshHome(ctxStub), log: () => {} })
if (!py?.hasDeps) { console.error(installHint(py?.path)); process.exit(1) }

/* 本轮生效的配置：写权限打开、不预演、日上限临时提高、不冷却 */
const engineCfg = {
  model: String(arg('model', readJson(join(HOME, 'config.json'), {}).model || 'qwen3.5:4b')),
  ollama_url: 'http://127.0.0.1:11434',
  num_ctx: Number(arg('num-ctx', 131072)),
  num_predict: 1200,
  temperature: 0.35,
  max_steps: 40,
  max_minutes: 5,
  auto_continue: true,
  auto_continue_max: 2,
  max_videos: MAX_VIDEOS,
  write: true,
  dry_run: false,
  max_posts_per_day: MAX_POSTS_PER_DAY,
  min_interval_min: 0,
}
writeJson(join(HOME, 'see-config.json'), engineCfg)

const env = {
  BILI_AUTOPILOT_PYTHON: py.path,
  BILI_AUTOPILOT_HOME: join(HOME, 'autopilot-home'),
  BILI_AUTOPILOT_CONFIG_FILE: join(HOME, 'see-config.json'),
  BILI_AUTOPILOT_ALLOW_WRITE: '1',
  BILI_AUTOPILOT_MODEL: engineCfg.model,
  OLLAMA_HOST: 'http://127.0.0.1:11434',
  BILI_PLUS_HOME: HOME,
  DSH_HOME: dshHome(ctxStub),
  PYTHONPATH: join(HOME, 'autopilot'),
}

const client = new McpClient({
  command: py.path,
  args: ['-m', 'bili_autopilot.server'],
  cwd: join(HOME, 'autopilot'),
  env,
  log: () => {},
})

const startedAt = Date.now()
const rounds = []
let posted = 0

await client.start()

// 先看一眼额度，别白跑
const health = await client.call('health', {}, 60_000)
const st0 = health.json || {}
console.log('[start]', JSON.stringify({ ok: st0.ok, checks: st0.checks }))

const RUNS = join(HOME, 'autopilot-home', 'runs')
const readRun = (id) => {
  try { return JSON.parse(readFileSync(join(RUNS, id, 'memory.json'), 'utf8')) } catch { return null }
}

while (true) {
  const elapsedMin = (Date.now() - startedAt) / 60_000
  const leftMin = MINUTES - elapsedMin
  if (leftMin < 1.2) break
  if (posted >= MAX_COMMENTS) break

  const waitSec = Math.max(90, Math.min(Math.round(leftMin * 60) + 30, 420))
  const t0 = Date.now()
  let res = null
  try {
    const r = await client.call('run', {
      task: TASK,
      write: true,
      dry_run: false,
      max_videos: MAX_VIDEOS,
      max_comments: 1,
      max_minutes: Math.max(2, Math.min(Math.ceil(leftMin), 5)),
      wait_seconds: waitSec,
    }, (waitSec + 120) * 1000)
    res = r.json || {}
  } catch (e) {
    res = { ok: false, status: 'client_error', last_error: String(e?.message || e) }
  }

  const secs = Math.round((Date.now() - t0) / 1000)
  const mem = res.run_id ? readRun(res.run_id) : null
  const videos = (mem?.videos || []).map((v) => ({
    bvid: v.bvid,
    title: v.title,
    author: v.author,
    action: v.comment_action,
    comment: v.comment_text || '',
    status: v.comment_status,
    rpid: (v.post_result || {}).rpid || '',
    verify: (v.verify_result || {}).status || '',
    reason: v.reason || '',
  }))
  const newPosted = videos.filter((v) => v.status === 'posted').length
  posted += newPosted

  rounds.push({
    run_id: res.run_id || '',
    status: res.status || '',
    seconds: secs,
    steps: res.steps || 0,
    videos_seen: res.videos_seen || 0,
    posted: newPosted,
    auto_continues: res.auto_continues || 0,
    last_error: res.last_error || '',
    videos,
    usage: mem?.usage || {},
  })
  console.log(`[round ${rounds.length}] ${secs}s status=${res.status} videos=${res.videos_seen} posted=${newPosted} total=${posted} elapsed=${((Date.now() - startedAt) / 1000).toFixed(0)}s`)

  if (res.status === 'failed' && String(res.last_error || '').includes('额度')) break
}

/* 结束时查一次额度 */
let endStatus = {}
try {
  const c2 = await client.call('status', {}, 30_000)
  endStatus = c2.json || {}
} catch { /* 忽略 */ }

client.kill()

const out = {
  minutes_planned: MINUTES,
  minutes_actual: Number(((Date.now() - startedAt) / 60_000).toFixed(2)),
  rounds,
  totals: {
    rounds: rounds.length,
    videos_seen: rounds.reduce((s, r) => s + r.videos_seen, 0),
    comments_posted: posted,
    prompt_tokens: rounds.reduce((s, r) => s + (r.usage.prompt_eval_count || 0), 0),
    eval_tokens: rounds.reduce((s, r) => s + (r.usage.eval_count || 0), 0),
    model_calls: rounds.reduce((s, r) => s + (r.usage.calls || 0), 0),
  },
  account: {
    posts_24h: endStatus.posts_24h,
    quota_left: endStatus.quota_left,
    limits: endStatus.limits,
  },
}
console.log('\n===RESULT===')
console.log(JSON.stringify(out, null, 2))
