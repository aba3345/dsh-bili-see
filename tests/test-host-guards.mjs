/**
 * tests/test-host-guards.mjs —— 宿主层（index.js）防守边的回归测试
 *
 * 覆盖这次修的点：
 *   ① 跨源 + text/plain 的配置写入必须被拒（403）
 *   ② "false" 字符串不能把写权限打开
 *   ③ 宿主保存失败 → 409 且本地不留半截（不再 ok:true + 三份分叉）
 *   ④ run_id 路径穿越必须失败
 *   ⑤ 死进程占着的 run 不能永久挡住新 round（active 判空）
 *
 * 全程假宿主，不连真 DSH、不碰真家目录。
 * 用法：DSH_HOME=<临时目录> node tests/test-host-guards.mjs
 */
import { join } from 'node:path'
import { mkdirSync, writeFileSync, readFileSync } from 'node:fs'

const DSH_HOME = process.env.DSH_HOME
if (!DSH_HOME) {
  console.error('需要 DSH_HOME 指向一个临时目录')
  process.exit(2)
}
const HOME = join(DSH_HOME, 'bili-see')
mkdirSync(HOME, { recursive: true })

let PASS = 0
let FAIL = 0
const check = (name, ok, detail = '') => {
  if (ok) { PASS += 1; console.log(`PASS  ${name}`) }
  else { FAIL += 1; console.log(`FAIL  ${name}  — ${detail}`) }
}

const tools = []
const routes = []
const effects = []
let updateThrows = false
let hostRow = null

const ctx = {
  logger: { info: () => {} },
  get: () => undefined,
  settings: {
    get: () => (hostRow ? [hostRow] : []),
    update: async () => {
      if (updateThrows) throw new Error('宿主更新失败（测试模拟）')
      return true
    },
  },
  tools: { register: (spec) => { tools.push(spec); return spec } },
  webServer: { register: (r) => { routes.push(r); return { dispose() {} } } },
  effect: (fn) => {
    try { const d = fn(); effects.push(typeof d === 'function' ? d : () => {}) } catch { /* 忽略 */ }
    return { dispose() {} }
  },
}

const mod = await import('../index.js')
await mod.apply(ctx, {})
const route = routes.find((r) => r.path === '/api/bili-see')

const http = async (method, path, { body, headers = {} } = {}) => {
  const payload = body === undefined ? null
    : Buffer.from(typeof body === 'string' ? body : JSON.stringify(body), 'utf8')
  const chunks = payload ? [payload] : []
  const req = {
    method,
    url: path,
    headers: { 'content-type': 'application/json', ...headers },
    async *[Symbol.asyncIterator]() { for (const c of chunks) yield c },
  }
  let text = ''
  const res = {
    statusCode: 0,
    headers: {},
    setHeader(k, v) { this.headers[k.toLowerCase()] = v },
    end: (s) => { text = s },
  }
  await route.handler(req, res)
  let json = null
  try { json = JSON.parse(text) } catch { /* 非 JSON */ }
  return { status: res.statusCode, json }
}

const readJson = (p, fb = {}) => { try { return JSON.parse(readFileSync(p, 'utf8')) } catch { return fb } }

/* ① 跨源 + text/plain */
{
  const before = (await http('GET', '/api/bili-see/config')).json?.config?.writeEnabled
  const r = await http('POST', '/api/bili-see/config', {
    body: { writeEnabled: true },
    headers: { origin: 'https://evil.example', 'content-type': 'text/plain' },
  })
  const after = (await http('GET', '/api/bili-see/config')).json?.config?.writeEnabled
  check('异源 + text/plain 被拒（403）', r.status === 403, `status=${r.status}`)
  check('被拒后开关没变', before === after, `${before} → ${after}`)
}

/* ② "false" 字符串 */
{
  await http('POST', '/api/bili-see/config', { body: { writeEnabled: 'false' } })
  const cfg = (await http('GET', '/api/bili-see/config')).json?.config
  const see = readJson(join(HOME, 'see-config.json'))
  check('字符串 "false" 落成布尔 false', cfg?.writeEnabled === false, String(cfg?.writeEnabled))
  check('引擎拿到 write=false', see.write === false, String(see.write))
  const bad = await http('POST', '/api/bili-see/config', { body: { writeEnabled: 'maybe' } })
  check('非法布尔被 400 拒绝', bad.status === 400, `status=${bad.status}`)
}

/* ③ 宿主保存失败 */
{
  hostRow = { ns: 'bili-see', revision: 3, config: { writeEnabled: true, maxPostsPerDay: 3 } }
  // 先让宿主和本地一致（true）
  await http('POST', '/api/bili-see/config', { body: { writeEnabled: true } })
  updateThrows = true
  const r = await http('POST', '/api/bili-see/config', { body: { writeEnabled: false } })
  const local = readJson(join(HOME, 'config.json'))
  const see = readJson(join(HOME, 'see-config.json'))
  check('宿主失败返回 409（不再假成功）', r.status === 409 && r.json?.ok === false, `status=${r.status}`)
  check('本地没留下半截（仍是 true）', local.writeEnabled === true, String(local.writeEnabled))
  check('引擎没被写成另一个值', see.write === true, String(see.write))
  updateThrows = false
  hostRow = null
}

/* ④ run_id 路径穿越 */
{
  const outside = join(HOME, 'autopilot-home', 'outside')
  mkdirSync(outside, { recursive: true })
  writeFileSync(join(outside, 'state.json'), JSON.stringify({ status: 'completed' }), 'utf8')
  writeFileSync(join(outside, 'digest.md'), '这是 runs 之外的 digest（不该被读到）', 'utf8')
  const r = await http('GET', '/api/bili-see/run?run_id=../outside')
  check('穿越 run_id 读不到东西', r.json?.ok === false, JSON.stringify(r.json).slice(0, 120))
  check('没有把 runs 外的 digest 回给前端',
    !String(r.json?.digest || '').includes('不该被读到'), String(r.json?.digest || ''))
}

/* ⑤ 死进程占着的 run */
{
  const runDir = join(HOME, 'autopilot-home', 'runs', '20261027-000009-dead')
  mkdirSync(runDir, { recursive: true })
  writeFileSync(join(runDir, 'state.json'), JSON.stringify({
    run_id: '20261027-000009-dead', status: 'running', phase: 'thinking',
    pid: 999999, pid_alive: false, created_at: '2026-10-27T10:00:00+08:00',
    steps: 3, videos_seen: 1,
  }), 'utf8')
  const st = (await http('GET', '/api/bili-see/status')).json
  check('死进程的 run 不算 active', st?.active === null, JSON.stringify(st?.active))
  const tick = tools.find((t) => t.name === 'bilisee_tick')
  // 只验证 activeRun 的判定：真的调 tick 会拉起引擎，这里用 runs 列表侧证
  const runs = (await http('GET', '/api/bili-see/runs')).json?.runs || []
  const dead = runs.find((r) => r.run_id === '20261027-000009-dead')
  check('runs 列表里能看到它', Boolean(dead), JSON.stringify(runs.map((r) => r.run_id)))
  check('列表带 pid 字段（前端可判断）', dead?.pid === 999999, String(dead?.pid))
  check('tick 工具存在（不实际触发）', typeof tick?.execute === 'function')
}

for (const d of effects.reverse()) { try { d() } catch { /* 忽略 */ } }

console.log(`\nPASS ${PASS} / FAIL ${FAIL}`)
process.exit(FAIL ? 1 : 0)
