/**
 * selfcheck.mjs —— 不开 DSH 也能验证这个插件是不是能跑
 * ============================================================================
 * 检查顺序就是「本地模型跑一轮」的真实依赖链：
 *
 *   ① 包内文件齐不齐（index.js / client.js / python / autopilot）
 *   ② 有没有一位能 import mcp+httpx+PIL+qrcode 的 Python
 *   ③ 家目录铺得对不对（mcp_server.py 平铺、autopilot 包完整）
 *   ④ Ollama 通不通、要用的模型在不在
 *   ⑤ B站 凭证在不在（自己的或借隔壁的）
 *   ⑥ 引擎能不能被 import（python -c "import bili_autopilot.server"）
 *
 * 用法：
 *   node selfcheck.mjs
 *   node selfcheck.mjs --home /path/to/home     # 指定家目录
 * ============================================================================
 */
import { existsSync, readdirSync } from 'node:fs'
import { join } from 'node:path'
import { spawnSync } from 'node:child_process'
import {
  PKG_ROOT, HOME_NAME, homeDir, dshHome, ensureDirs, syncPythonFiles, readJson,
} from './lib/state.mjs'
import { findPython, installHint } from './lib/python-env.mjs'

const args = process.argv.slice(2)
const homeArgIdx = args.indexOf('--home')
const ctxStub = homeArgIdx >= 0 && args[homeArgIdx + 1]
  ? { get: (k) => (k === 'dshHomePath' ? () => join(args[homeArgIdx + 1], '..') : undefined) }
  : { get: () => undefined }

const results = []
const check = (name, ok, detail = '') => {
  results.push({ name, ok, detail })
  const mark = ok === true ? 'PASS' : ok === false ? 'FAIL' : 'SKIP'
  console.log(`${mark}  ${name}${detail ? `  — ${detail}` : ''}`)
}

console.log(`\n=== dsh-bili-see selfcheck ===\n包目录：${PKG_ROOT}`)

/* ① 包内文件 */
const required = [
  'index.js', 'client.js', 'package.json', 'cordis.patch.yml',
  'lib/mcp-client.mjs', 'lib/python-env.mjs', 'lib/state.mjs', 'lib/hostdeps.mjs',
  'python/mcp_server.py', 'python/api.py', 'python/db.py', 'python/safety.py',
  'python/autopilot/bili_autopilot/server.py',
  'python/autopilot/bili_autopilot/agent.py',
  'python/autopilot/bili_autopilot/config.py',
  'config/persona.yaml',
]
const missing = required.filter((f) => !existsSync(join(PKG_ROOT, f)))
check('包内文件齐全', missing.length === 0, missing.length ? `缺 ${missing.join(', ')}` : `${required.length} 个都在`)

/* ② Python */
const py = findPython({ dshHome: dshHome(ctxStub), log: (m) => console.log(`     · ${m}`) })
check('找到可用 Python（含 mcp/httpx/PIL/qrcode）', Boolean(py?.hasDeps),
  py ? `${py.path} · ${py.version}${py.hasDeps ? '' : ` · 缺 ${py.missingDeps}`}` : '一个都没找到')
if (py && !py.hasDeps) console.log(`\n${installHint(py.path)}\n`)

/* ③ 家目录铺设 */
const HOME = homeDir(ctxStub)
const DIRS = ensureDirs(HOME)
const synced = syncPythonFiles(HOME)
check('家目录铺好（mcp_server.py 平铺）', existsSync(join(HOME, 'mcp_server.py')),
  `拷 ${synced.copied.length}，跳过 ${synced.skipped.length}`)
check('本地模型引擎铺好（autopilot 包）',
  existsSync(join(HOME, 'autopilot', 'bili_autopilot', 'server.py')),
  join(HOME, 'autopilot', 'bili_autopilot'))
check('人设文件在位', existsSync(join(HOME, 'persona.yaml')))

/* ④ 引擎能 import */
if (py?.path) {
  const r = spawnSync(py.path, ['-c', 'import bili_autopilot.server as s; print("ok")'], {
    cwd: join(HOME, 'autopilot'),
    env: { ...process.env, PYTHONPATH: join(HOME, 'autopilot'), PYTHONIOENCODING: 'utf-8', PYTHONUTF8: '1' },
    encoding: 'utf8',
    timeout: 60_000,
    windowsHide: true,
  })
  const ok = (r.stdout || '').includes('ok') && r.status === 0
  check('引擎可 import（bili_autopilot.server）', ok,
    ok ? '' : `exit=${r.status} ${(r.stderr || '').slice(-400)}`)
}

/* ⑤ Ollama */
const cfg = { ...{ ollamaUrl: 'http://127.0.0.1:11434', model: 'qwen3.5:4b-32k' }, ...readJson(join(HOME, 'config.json'), {}) }
const tags = await (async () => {
  try {
    const ac = new AbortController()
    const t = setTimeout(() => ac.abort(), 4000)
    const res = await fetch(`${String(cfg.ollamaUrl).replace(/\/+$/, '')}/api/tags`, { signal: ac.signal })
    clearTimeout(t)
    if (!res.ok) return { ok: false, error: `HTTP ${res.status}` }
    const d = await res.json()
    return { ok: true, models: (d.models || []).map((m) => m.name || m.model) }
  } catch (e) {
    return { ok: false, error: String(e?.message || e) }
  }
})()
check('Ollama 在跑', tags.ok, tags.ok ? `${tags.models.length} 个模型` : tags.error)
if (tags.ok) {
  const want = String(cfg.model || '')
  const has = tags.models.some((n) => n === want || String(n).split(':')[0] === want.split(':')[0])
  check(`模型 ${want} 在位`, has, has ? '' : `现有：${tags.models.slice(0, 6).join(', ')}`)
}

/* ⑥ B站 凭证 */
const own = join(HOME, 'bili_credential.json')
const dsh = dshHome(ctxStub)
const borrowed = ['bili-plus', 'bili-duty', 'bili-scroll'].map((d) => join(dsh, d, 'bili_credential.json'))
const cred = existsSync(own) ? own : borrowed.find((p) => existsSync(p))
check('B站 凭证可用', Boolean(cred), cred ? `用这份：${cred}` : '还没有，去设置页扫码')

/* 汇总 */
const failed = results.filter((r) => r.ok === false)
console.log(`\n家目录：${HOME}`)
console.log(`配置：${join(HOME, 'config.json')}`)
console.log(`run 目录：${join(HOME, 'autopilot-home', 'runs')}`)
try {
  const runs = readdirSync(join(HOME, 'autopilot-home', 'runs'))
  console.log(`历史 run：${runs.length} 个`)
} catch { /* 还没有 */ }

console.log('')
if (failed.length) {
  console.log(`❌ ${failed.length} 项没过：${failed.map((f) => f.name).join(' / ')}`)
  process.exit(1)
}
console.log('✅ 全部通过 —— 触发一轮：让 DeepSeek 调 bilisee_tick，或等宿主的定时任务到点唤起')
