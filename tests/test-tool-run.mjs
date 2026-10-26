/**
 * tests/test-tool-run.mjs —— 通过**插件注册的工具**跑一轮本地模型
 * ============================================================================
 * 这是最接近真实使用的一条链路：
 *
 *   假宿主 → ctx.tools.register 收集到的 bilisee_run.execute()
 *          → ensureAp() 起引擎 MCP 子进程（用插件家目录那套环境）
 *          → 引擎自己再起 B站 MCP
 *          → 本地模型跑完 → 返回 digest
 *
 * 它跟 tests/run-engine.mjs 的区别：那个直接连引擎，这个走**插件自己的代码**，
 * 所以能抓到 index.js 里环境变量拼错、路径拼错这类问题。
 *
 * 用法：
 *   node tests/test-tool-run.mjs                 # 只读
 *   node tests/test-tool-run.mjs --rehearse      # 写入 + 预演
 * ============================================================================
 */
const args = process.argv.slice(2)
const rehearse = args.includes('--rehearse')

const tools = []
const logs = []
const effects = []

const ctx = {
  logger: { info: (m) => logs.push(m) },
  get: () => undefined,
  settings: { get: () => [], update: async () => true },
  tools: { register: (spec) => { tools.push(spec); return spec } },
  webServer: { register: (r) => { return { dispose() {} } } },
  effect: (fn) => {
    try { const d = fn(); effects.push(typeof d === 'function' ? d : () => {}) } catch { /* 忽略 */ }
    return { dispose() {} }
  },
}

const mod = await import('../index.js')
await mod.apply(ctx, {})

const byName = (n) => tools.find((t) => t.name === n)
const fail = []
const check = (name, ok, detail = '') => {
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${name}${detail && !ok ? `  — ${detail}` : ''}`)
  if (!ok) fail.push(name)
}

console.log(`\n=== 通过 bilisee_tick 跑一轮（${rehearse ? '写入（真发）' : '只读'}）===\n`)

/* ① health */
const health = await byName('bilisee_health').execute({}, {})
console.log('health checks:', JSON.stringify(health.checks))
console.log('ollama models:', (health.ollama_models || []).join(', '))
check('health.ok', health.ok === true, JSON.stringify(health.checks))
check('本地模型在位', health.checks?.model === true, health.model)

if (!health.ok) {
  console.log('\n环境没准备好，后面的 run 不跑了。')
  for (const d of effects.reverse()) { try { d() } catch { /* 忽略 */ } }
  process.exit(1)
}

/* ② 触发（tick 必须不等结果）+ 轮询到终态 */
const TERMINAL = new Set(['completed', 'failed', 'cancelled', 'needs_continue'])
const sleep = (ms) => new Promise((r) => setTimeout(r, ms))

const t0 = Date.now()
let res
try {
  res = await byName('bilisee_tick').execute({ write: rehearse, max_videos: 1 }, {})
} catch (e) {
  check('bilisee_tick 执行', false, e?.message || String(e))
  console.log(e)
  process.exit(1)
}
const tickMs = Date.now() - t0
check('拿到 run_id', Boolean(res.run_id), JSON.stringify(res).slice(0, 300))
check('tick 立即返回（非阻塞）', tickMs < 60_000, `等了 ${tickMs}ms`)
check('tick 标了 triggered', res.triggered === true, String(res.triggered))

let st = res
while (!TERMINAL.has(st.status) && Date.now() - t0 < 20 * 60_000) {
  await sleep(5000)
  st = await byName('bilisee_status').execute({ run_id: res.run_id }, {})
}
const secs = Math.round((Date.now() - t0) / 1000)

console.log(`\n耗时 ${secs}s（触发只用 ${tickMs}ms）`)
console.log(`status=${st.status} steps=${st.steps} videos=${st.videos_seen} posted=${st.comments_posted} auto=${st.auto_continues}`)
if (st.last_error) console.log(`last_error=${st.last_error}`)

check('状态是 completed', st.status === 'completed', String(st.status))
check('看过至少 1 条视频', Number(st.videos_seen) >= 1, String(st.videos_seen))

/* ③ 结论从独立工具拿（不再由 run 一次性返回） */
const sm0 = await byName('bilisee_summary').execute({ run_id: res.run_id }, {})
check('bilisee_summary 能返回总结', sm0.ok !== false && String(sm0.text || '').length > 20, String(sm0.error || ''))
check('总结里带 BV 号', /BV[0-9A-Za-z]{10}/.test(String(sm0.text || '')), String(sm0.text || '').slice(0, 120))
check('总结标记了是否降级', typeof sm0.degraded === 'boolean', String(sm0.degraded))
check('总结带了节省比例', Number(sm0.saved_ratio) >= 0, String(sm0.saved_ratio))

console.log('\n--- 视频总结 ---')
console.log(String(sm0.text || '(无)'))
console.log(`原文 ${sm0.raw_chars || 0} 字符 → 总结 ${sm0.chars || 0} 字符，省 ${Math.round((sm0.saved_ratio || 0) * 100)}%`)
console.log('--- 完 ---')

const dg0 = await byName('bilisee_digest').execute({ run_id: res.run_id, max_chars: 6000 }, {})
const dig = String(dg0.digest || '')
check('返回了 digest', dig.length > 400, `${dig.length} chars`)
check('digest 里有思维链要点', dig.includes('思维链'))
check('digest 里有视频记录', dig.includes('视频记录'))
check('digest 里有选片配额段', dig.includes('选片配额'))
check('返回里没有 dry_run（预演已删）', !('dry_run' in res))

console.log('\n--- digest 摘要 ---')
console.log(dig.slice(0, 1400))
console.log('--- 完 ---\n')

/* ④ runs / digest / status 三个读取工具 */
const runs = await byName('bilisee_runs').execute({ limit: 5 }, {})
check('bilisee_runs 能列出这一轮', (runs.runs || []).some((r) => r.run_id === res.run_id), `${(runs.runs || []).length} 条`)

const dg = await byName('bilisee_digest').execute({ run_id: res.run_id, max_chars: 2000 }, {})
check('bilisee_digest 能重读', dg.ok !== false && String(dg.digest || '').length > 200, String(dg.error || ''))

const stt = await byName('bilisee_status').execute({ run_id: res.run_id }, {})
check('bilisee_status 能读状态', stt.ok !== false && stt.status === 'completed', String(stt.status))

/* ④ 视频总结工具（缓存命中，不额外跑模型） */
const sm = await byName('bilisee_summary').execute({ run_id: res.run_id }, {})
check('bilisee_summary 能返回总结', sm.ok !== false && String(sm.text || '').length > 20, String(sm.error || ''))
check('bilisee_summary 走的是缓存', sm.cached === true, String(sm.cached))
check('bilisee_summary 带每日额度', Boolean(sm.daily), JSON.stringify(sm.daily || {}))

for (const d of effects.reverse()) { try { d() } catch { /* 忽略 */ } }

console.log('')
if (fail.length) {
  console.log(`❌ ${fail.length} 项失败：${fail.join(' / ')}`)
  process.exit(1)
}
console.log('✅ 插件 → 引擎 → B站 全链路通')
process.exit(0)
