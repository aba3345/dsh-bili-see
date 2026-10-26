/**
 * tests/workbuddy-live.mjs —— 真实跑一整轮工作流（WorkBuddy 侧，只用本地模型）
 * ============================================================================
 * 走的是**插件自己的工具**，和线上链路的差别只剩「谁来叫醒它」：
 *
 *   假宿主 → bilisee_tick（非阻塞，拿 run_id）→ 轮询 bilisee_status → 收尾读 summary
 *
 * 宿主的定时任务到点做的事，就是脚本里这一下 `bilisee_tick`。
 * 全程只用本机 Ollama，不碰云端模型额度。
 *
 * 默认**只读**（write=false）——自动化驱动之后更不该让测试顺手发评论。
 * 真要验证写入链路用 `--write`，并自己想清楚这是在用真实账号。
 *
 * 用法：
 *   node tests/workbuddy-live.mjs                  # 只读，最多 2 条
 *   node tests/workbuddy-live.mjs --videos 3
 *   node tests/workbuddy-live.mjs --write          # 允许真发（谨慎）
 *   node tests/workbuddy-live.mjs --timeout 25     # 最多等多少分钟
 * ============================================================================
 */
import { readFileSync, readdirSync } from 'node:fs'
import { join } from 'node:path'

const argv = process.argv.slice(2)
const argOf = (name, dflt) => {
  const i = argv.indexOf(`--${name}`)
  return i >= 0 && argv[i + 1] ? Number(argv[i + 1]) : dflt
}
const allowWrite = argv.includes('--write')
const taskIdx = argv.indexOf('--task')
const task = taskIdx >= 0 ? argv[taskIdx + 1] : ''
const timeoutMin = argOf('timeout', 20)

/** 引擎的终态（与 python/autopilot/.../state.py 的 TERMINAL 一致） */
const TERMINAL = new Set(['completed', 'failed', 'cancelled', 'needs_continue'])

/* ── 假宿主 ── */
const tools = []
const ctx = {
  logger: { info: () => {} },
  get: () => undefined,
  settings: { get: () => [], update: async () => true },
  tools: { register: (spec) => { tools.push(spec); return spec } },
  webServer: { register: () => ({ dispose() {} }) },
  effect: () => ({ dispose() {} }),
}

const mod = await import('../index.js')
await mod.apply(ctx, {})
const byName = (n) => tools.find((t) => t.name === n)

const health = await byName('bilisee_health').execute({}, {})
console.log('=== 环境 ===')
console.log('checks  :', JSON.stringify(health.checks))
console.log('模型    :', health.model)
console.log('Python  :', health.python?.version || '未知')
console.log('B站     :', health.bili?.uname || '未登录', '| 今日已发', health.bili?.posts_24h, '| 剩余', health.bili?.quota_left)
if (!health.ok) {
  console.log('\n环境没准备好，不跑了。', health.engine_error || health.python_error || '')
  process.exit(1)
}

const videos = argOf('videos', 2)
const home = health.home
const runsDir = join(home, 'autopilot-home', 'runs')

console.log('\n=== 本轮设定 ===')
console.log(`最多看 ${videos} 条；评论上限 1 条；写入=${allowWrite ? '开（会真发）' : '关（只读）'}`)
console.log(`最长等 ${timeoutMin} 分钟\n`)

/* ── 1. 触发（宿主定时任务到点做的就是这一下） ── */
const t0 = Date.now()
const startNames = new Set(readdirSync(runsDir).filter((x) => !x.startsWith('.')))

// 参数必须是「无损 JSON」：undefined 会被宿主直接拒掉。
const tickArgs = { max_videos: videos, max_comments: 1, write: allowWrite }
if (task) tickArgs.task = task

const tick = await byName('bilisee_tick').execute(tickArgs, {})
console.log('=== 触发 ===')
console.log(`run_id  : ${tick.run_id || '(无)'}`)
console.log(`status  : ${tick.status}`)
console.log(`triggered: ${tick.triggered}${tick.already_running ? '（复用了正在跑的那轮）' : ''}`)
if (!tick.run_id) {
  console.log('\n没启动起来：', tick.error || tick.hint || '未知')
  process.exit(1)
}
const runId = tick.run_id

/* ── 2. 轮询进度（每条视频打出来；读磁盘不花额度） ── */
const seen = new Set()
const poll = setInterval(() => {
  let latest = ''
  try {
    const names = readdirSync(runsDir).filter((x) => !x.startsWith('.')).sort()
    latest = names[names.length - 1] || ''
  } catch { return }
  if (!latest || startNames.has(latest)) return
  try {
    const mem = JSON.parse(readFileSync(join(runsDir, latest, 'memory.json'), 'utf8'))
    for (const v of mem.videos || []) {
      if (seen.has(v.bvid)) continue
      seen.add(v.bvid)
      const secs = Math.round((Date.now() - t0) / 1000)
      const play = v.play ? `${(v.play / 10000).toFixed(1)}万` : '?'
      const fans = v.fans ? `${(v.fans / 10000).toFixed(1)}万` : '?'
      console.log(`[${String(secs).padStart(3)}s] 第 ${seen.size} 条 · ${(v.title || '').slice(0, 30)}`)
      console.log(`        ${v.bucket || '?'} | 播放${play} 粉丝${fans} | ${v.comment_action}` +
        (v.comment_text ? ` | 评论：${v.comment_text}` : ''))
    }
  } catch { /* 文件正在写，跳过这一拍 */ }
}, 4000)

let st = tick
let lastPrintAt = 0
const deadline = Date.now() + timeoutMin * 60_000
try {
  while (true) {
    if (TERMINAL.has(st.status)) break
    if (Date.now() > deadline) {
      console.log(`\n⚠️ 等满 ${timeoutMin} 分钟还没收尾，按超时退出（run 还在后台跑）`)
      break
    }
    await new Promise((r) => setTimeout(r, 5000))
    try {
      st = await byName('bilisee_status').execute({ run_id: runId }, {})
    } catch (e) {
      console.log(`  状态读失败：${e?.message || e}`)
      continue
    }
    const secs = Math.round((Date.now() - t0) / 1000)
    if (secs - lastPrintAt >= 30) {
      lastPrintAt = secs
      console.log(`[${String(secs).padStart(3)}s] ${st.status} · ${st.phase || '-'} · ${st.steps || 0} 步`
        + ` · 看 ${st.videos_seen || 0} 条 · 续 ${st.auto_continues || 0} 次`)
    }
  }
} finally {
  clearInterval(poll)
}

const secs = Math.round((Date.now() - t0) / 1000)
console.log('\n=== 结果 ===')
console.log(`run_id : ${runId}`)
console.log(`状态   : ${st.status}（${secs}s，${st.steps || 0} 步，自动续跑 ${st.auto_continues || 0} 次）`)
console.log(`看过   : ${st.videos_seen || 0} 条　发出评论：${st.comments_posted || 0} 条`)
if (st.last_error) console.log(`错误   : ${st.last_error}`)

/* ── 3. 结论 ── */
const sm = await byName('bilisee_summary').execute({ run_id: runId }, {})
console.log('\n=== 视频总结 ===')
console.log(sm.text || '(无)')
if (sm.chars) {
  console.log(`\n原记忆 ${sm.raw_chars} 字符 → 总结 ${sm.chars} 字符，省 ${Math.round((sm.saved_ratio || 0) * 100)}%`)
}

const dg = await byName('bilisee_digest').execute({ run_id: runId, max_chars: 20000 }, {})
const text = String(dg.digest || '')
const body = text.split('## 视频记录')[1]?.split('## 跨视频发现')[0] || ''
console.log('\n=== 逐条记录 ===')
console.log(body.trim().slice(0, 3000))

const ok = st.status === 'completed'
console.log(`\n${ok ? '✅ 链路跑通' : '⚠️ 有异常，看上面的状态与错误'}`)
process.exit(ok ? 0 : 2)
