/**
 * tests/test-apply.mjs —— 不开 DSH，用假宿主把 apply() 跑一遍
 * ============================================================================
 * 它验证的是「插件被加载时不炸」这一类问题：
 *
 *   ① apply() 能不能正常返回（工具注册、路由注册、定时器、effect 全走一遍）
 *   ② 工具名都注册上了没有
 *   ③ HTTP 路由真能应答（/config /status /models）
 *
 * 这些错误在真实 DSH 里表现为「插件不激活」或「设置页白屏」，日志很难读，
 * 所以单独拉出来测。
 *
 * 用法：node tests/test-apply.mjs
 * ============================================================================
 */
import { join } from 'node:path'

const failures = []
const check = (name, ok, detail = '') => {
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${name}${detail && !ok ? `  — ${detail}` : ''}`)
  if (!ok) failures.push(name)
}

/* ── 假宿主 ── */
const tools = []
const routes = []
const effects = []
const logs = []

const ctx = {
  logger: { info: (m) => logs.push(m) },
  get: () => undefined,
  settings: {
    get: () => [],
    update: async () => true,
  },
  tools: {
    register: (spec) => { tools.push(spec); return spec },
  },
  webServer: {
    register: (route) => { routes.push(route); return { dispose() {} } },
  },
  // cordis 的 effect：给一个「返回 disposer 的工厂」，立刻执行并记住 disposer
  effect: (fn) => {
    try {
      const d = fn()
      effects.push(typeof d === 'function' ? d : () => {})
      return { dispose() { if (typeof d === 'function') d() } }
    } catch (e) {
      check('effect 回调执行', false, e?.message || String(e))
      return { dispose() {} }
    }
  },
}

/* ── 加载插件 ── */
let mod
try {
  mod = await import('../index.js')
  check('插件模块导入', true)
} catch (e) {
  check('插件模块导入', false, e?.message || String(e))
  console.log(`\n${failures.length} 项失败`)
  process.exit(1)
}

check('导出 name = bili-see', mod.name === 'bili-see', String(mod.name))
check('导出 apply 函数', typeof mod.apply === 'function')
check('导出 Config（设置页表单）', Boolean(mod.Config))

/* ── 跑 apply ── */
let applyErr = null
try {
  await mod.apply(ctx, {})
} catch (e) {
  applyErr = e
  console.error(e)
}
check('apply() 执行不抛异常', !applyErr, applyErr?.message || '')

/* ── 工具 ── */
const names = tools.map((t) => t.name)
const expected = [
  'bilisee_tick', 'bilisee_status', 'bilisee_digest', 'bilisee_runs',
  'bilisee_cancel', 'bilisee_health', 'bilisee_models', 'bilisee_summary',
  'bilisee_me', 'bilisee_search', 'bilisee_video', 'bilisee_subtitle',
  'bilisee_comments', 'bilisee_popular', 'bilisee_audit',
]
const missingTools = expected.filter((n) => !names.includes(n))
check(`注册了 ${expected.length} 个工具`, missingTools.length === 0, `缺 ${missingTools.join(', ')}`)

// 「跑一轮」的人工入口必须已经删掉（阻塞 run + wait）
const goneTools = ['bilisee_run', 'bilisee_wait'].filter((n) => names.includes(n))
check('阻塞式 run / wait 工具已移除', goneTools.length === 0, `还在：${goneTools.join(', ')}`)

// 每个工具都要有 description + execute + output（宿主只吃这个形状）
const badTools = tools.filter((t) => !t.name || typeof t.execute !== 'function' || !t.description || !t.output)
check('每个工具都有 description/execute/output', badTools.length === 0,
  badTools.map((t) => t.name).join(', '))

/* ── 路由 ── */
check('注册了 HTTP 路由', routes.length > 0, `count=${routes.length}`)
const apiRoute = routes.find((r) => r.path === '/api/bili-see')
check('路由前缀是 /api/bili-see', Boolean(apiRoute), routes.map((r) => r.path).join(', '))

/* ── 真打几个 GET 路由 ── */
const call = async (method, path, body) => {
  const chunks = body ? [Buffer.from(JSON.stringify(body), 'utf8')] : []
  const req = {
    method,
    url: path,
    headers: { 'content-type': 'application/json' },
    async *[Symbol.asyncIterator]() { for (const c of chunks) yield c },
  }
  let payload = ''
  const res = {
    statusCode: 0,
    headers: {},
    setHeader(k, v) { this.headers[k.toLowerCase()] = v },
    end(s) { payload = s },
  }
  await apiRoute.handler(req, res)
  let json = null
  try { json = JSON.parse(payload) } catch { /* 不是 JSON */ }
  return { status: res.statusCode, json, raw: payload }
}

if (apiRoute) {
  const cfg = await call('GET', '/api/bili-see/config')
  check('GET /config 返回 200 + config', cfg.status === 200 && Boolean(cfg.json?.config),
    `status=${cfg.status} ${cfg.raw.slice(0, 160)}`)
  check('config 里有模型名', typeof cfg.json?.config?.model === 'string' && cfg.json.config.model.length > 0, String(cfg.json?.config?.model))
  check('numCtx 默认 = 131072（最大值）', Number(cfg.json?.config?.numCtx) === 131072, String(cfg.json?.config?.numCtx))
  const removedKeys = ['enabled', 'roundsPerDay', 'noon', 'night', 'testMode', 'testBvid',
    'dryRunDefault', 'keywords']
  const stillThere = removedKeys.filter((k) => k in (cfg.json?.config || {}))
  check('已删的功能不再出现在 config 里', stillThere.length === 0, stillThere.join(', '))
  check('新配置：质量门槛在', Number(cfg.json?.config?.minUpFans) === 10000 && Number(cfg.json?.config?.minPlay) === 10000,
    `${cfg.json?.config?.minUpFans}/${cfg.json?.config?.minPlay}`)
  check('新配置：一天最多浏览在', Number(cfg.json?.config?.dailyVideoMax) > 0, String(cfg.json?.config?.dailyVideoMax))
  check('新配置：三块配额在',
    Number(cfg.json?.config?.aiTechQuota) >= 0 && Number(cfg.json?.config?.aiFunQuota) >= 0
    && typeof cfg.json?.config?.otherPrefs === 'string',
    JSON.stringify({ t: cfg.json?.config?.aiTechQuota, f: cfg.json?.config?.aiFunQuota }))
  check('新配置：视频总结默认开', cfg.json?.config?.summaryEnabled === true, String(cfg.json?.config?.summaryEnabled))
  check('config 返回配额核算', Boolean(cfg.json?.quota), JSON.stringify(cfg.json?.quota || {}))
  check('配额核算能算出合计', Number(cfg.json?.quota?.wanted) >= 0, String(cfg.json?.quota?.wanted))
  check('预演字段彻底消失（dryRunDefault）', !('dryRunDefault' in (cfg.json?.config || {})))

  const models = await call('GET', '/api/bili-see/models')
  check('GET /models 返回 Ollama 探测结果', models.status === 200 && Array.isArray(models.json?.models),
    `status=${models.status} ${models.raw.slice(0, 200)}`)
  if (models.json?.ok) {
    console.log(`     · Ollama 探测到 ${models.json.models.length} 个模型：${models.json.models.map((m) => m.name).slice(0, 4).join(', ')}`)
  } else {
    console.log(`     · Ollama 未连通：${models.json?.error || '未知'}`)
  }

  const st = await call('GET', '/api/bili-see/status')
  check('GET /status 返回 200', st.status === 200 && st.json?.ok === true,
    `status=${st.status} ${st.raw.slice(0, 240)}`)
  check('/status 带 engine 自检', Boolean(st.json?.engine), JSON.stringify(st.json?.engine || {}).slice(0, 200))
  check('/status 不再有定时相关字段', !('enabled' in (st.json || {})) && !('currentWindow' in (st.json || {})))
  check('/status 带 sync 对比结果', Boolean(st.json?.sync) && Array.isArray(st.json?.sync?.diffs),
    JSON.stringify(st.json?.sync || {}).slice(0, 240))
  check('/status 的 sync 拿引擎配额当基准',
    st.json?.sync?.engine == null || typeof st.json?.sync?.engine?.daily_video_max === 'number',
    JSON.stringify(st.json?.sync?.engine || null).slice(0, 160))

  const syncRes = await call('POST', '/api/bili-see/sync', {})
  check('POST /sync 能重新下发配置', syncRes.status === 200 && syncRes.json?.ok === true,
    syncRes.raw.slice(0, 200))

  const runs = await call('GET', '/api/bili-see/runs')
  check('GET /runs 返回列表', runs.status === 200 && Array.isArray(runs.json?.runs), runs.raw.slice(0, 160))

  const auto = await call('GET', '/api/bili-see/automation')
  check('GET /automation 返回定时任务指令', auto.status === 200 && typeof auto.json?.sample === 'string',
    `status=${auto.status} ${auto.raw.slice(0, 200)}`)
  check('/automation 指向 harness 的 schedule_create',
    auto.json?.tool === 'schedule_create' && auto.json?.mode === 'harness-schedule',
    JSON.stringify({ tool: auto.json?.tool, mode: auto.json?.mode }))
  check('/automation 指令里点名 bilisee_tick', String(auto.json?.prompt || '').includes('bilisee_tick'),
    String(auto.json?.prompt || '').slice(0, 120))

  // 删掉的功能：路由必须真的没了
  const postRun = await call('POST', '/api/bili-see/run', {})
  check('POST /run 已删除（404，不能再人工触发）', postRun.status === 404, `status=${postRun.status}`)
  const drafts = await call('GET', '/api/bili-see/drafts')
  check('草稿列表路由已删除（404）', drafts.status === 404, `status=${drafts.status}`)
  const delDraft = await call('POST', '/api/bili-see/drafts/delete', { draft_id: 1 })
  check('删草稿路由已删除（404）', delDraft.status === 404, `status=${delDraft.status}`)

  const bad = await call('GET', '/api/bili-see/nope')
  check('未知路由返回 404 而不是崩', bad.status === 404, `status=${bad.status}`)
}

/* ── 收摊 ── */
for (const d of effects.reverse()) { try { d() } catch { /* 忽略 */ } }

console.log(`\n日志 ${logs.length} 条，最后 3 条：`)
console.log(logs.slice(-3).map((l) => `  ${l}`).join('\n'))

console.log('')
if (failures.length) {
  console.log(`❌ ${failures.length} 项失败：${failures.join(' / ')}`)
  process.exit(1)
}
console.log('✅ apply() 全绿 —— 工具和设置页路由都对')
process.exit(0)
