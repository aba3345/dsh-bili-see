/**
 * tests/test-client.mjs —— 在不开 DSH 的情况下把设置页真渲染一遍
 * ============================================================================
 * 它解决的问题：`node --check` 只查语法，查不到「删了 state 但 JSX 还在引用」
 * 这种错。这里用一个极小的 React 运行时把 client.js 的 Panel 渲染成元素树，
 * 再走一遍树把文字抓出来断言。
 *
 * 能验到：
 *   ① 注册的侧边栏 label / id / order
 *   ② 页面上有哪些区块、标题对不对
 *   ③ 被删掉的功能（草稿箱 / 测试模式 / 定时自动跑）确实不在页面上
 *   ④ 上下文窗口默认值
 *   ⑤ 小字文案的长度（防 AI 味长句）
 *
 * 用法：node tests/test-client.mjs
 * ============================================================================
 */
import { readFileSync, writeFileSync } from 'node:fs'
import { join, dirname } from 'node:path'
import { fileURLToPath } from 'node:url'
import vm from 'node:vm'

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..')
const failures = []
const check = (name, ok, detail = '') => {
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${name}${detail && !ok ? `  — ${detail}` : ''}`)
  if (!ok) failures.push(name)
}

/* ───────────── 迷你 React ───────────── */

let states = []
let hookIdx = 0
let ranEffects = new Set()
const pendingEffects = []
let dirty = false

const React = {
  createElement(type, props, ...children) {
    return { type, props: props || {}, children: children.flat(Infinity).filter((c) => c !== null && c !== undefined && c !== false) }
  },
  useState(init) {
    const i = hookIdx++
    if (!(i in states)) states[i] = typeof init === 'function' ? init() : init
    const set = (v) => {
      const next = typeof v === 'function' ? v(states[i]) : v
      if (next !== states[i]) { states[i] = next; dirty = true }
    }
    return [states[i], set]
  },
  useEffect(fn) {
    const i = hookIdx++
    if (!ranEffects.has(i)) pendingEffects.push({ i, fn })
  },
  useMemo(fn) { return fn() },
  useCallback(fn) { return fn },
  useRef(v) { const i = hookIdx++; if (!(i in states)) states[i] = { current: v }; return states[i] },
}

/* ───────────── 假宿主 ───────────── */

const styleEl = { setAttribute() {}, remove() {}, textContent: '' }
const sandbox = {
  console,
  setTimeout: () => 0,
  clearTimeout: () => {},
  setInterval: () => 0,
  clearInterval: () => {},
  encodeURIComponent,
  document: {
    createElement: () => ({ ...styleEl }),
    head: { appendChild() {} },
  },
  window: {},
}
sandbox.globalThis = sandbox

/* ───────────── 假接口 ───────────── */

const CFG = {
  videosPerRun: 2,
  model: 'qwen3.5:4b-32k',
  ollamaUrl: 'http://127.0.0.1:11434',
  numCtx: 131072,
  numPredict: 1200,
  temperature: 0.35,
  maxSteps: 40,
  numMinutes: 20,
  autoContinue: true,
  autoContinueMax: 2,
  waitSec: 600,
  writeEnabled: true,
  maxPostsPerDay: 5,
  minIntervalMin: 30,
  freshDays: 2,
  watchMids: '285286947',
  minUpFans: 10000,
  minPlay: 10000,
  dailyVideoMax: 6,
  aiTechQuota: 2,
  aiTechKeywords: 'AI 大模型,开源模型',
  aiFunQuota: 2,
  aiFunKeywords: 'AI 绘画,AI 整活',
  otherPrefs: '数码评测 | 手机,相机 | 1',
  summaryEnabled: true,
  personaPath: '',
  pythonPath: '',
  reportLevel: 'full',
}

const FIXTURES = {
  '/config': { ok: true, config: CFG },
  '/status': {
    ok: true,
    writeEnabled: true,
    model: CFG.model,
    ollamaUrl: CFG.ollamaUrl,
    today: { runs: 2, picked: 3, posted: 1 },
    python: { path: 'py.exe', version: '3.12.10', hasDeps: true },
    pythonError: '',
    home: 'C:\\Users\\x\\.dsh\\bili-see',
    engine: { ok: true, checks: { python: true, ollama: true } },
    bili: { ok: true, logged_in: true, uname: '测试号', quota_left: 4 },
    active: null,
    daily: { date: '2026-10-25', daily_max: 6, viewed: 2, remaining: 4 },
    sync: {
      ok: false,
      diffs: [{ key: 'daily_video_max', ui: 6, engine: 12 }],
      ui: { daily_video_max: 6, ai_tech: 3, ai_fun: 2, max_posts: 5, min_interval: 30, write: false, model: 'qwen3.5:4b-32k' },
      engine: { daily_video_max: 12, ai_tech: 3, ai_fun: 9, max_posts: null, min_interval: null, write: true, model: 'qwen3.5:4b-32k' },
    },
  },
  '/runs': {
    ok: true,
    runs: [{ run_id: '20261025-200000-aaaa', status: 'completed', steps: 8, videos_seen: 2, comments_posted: 1, created_at: '2026-10-25T20:00:00+08:00' }],
  },
  '/automation': {
    ok: true,
    mode: 'harness-schedule',
    tool: 'schedule_create',
    every_seconds: 86400,
    prompt: '调用 bilisee_tick 把一轮 B 站巡航启动起来，不要等它跑完、也不要重复调用；稍后用 bilisee_summary 取结论并向用户简短汇报。',
    sample: '帮我建一个定时任务：每 24 小时跑一次。调用 bilisee_tick 把一轮 B 站巡航启动起来，不要等它跑完、也不要重复调用；稍后用 bilisee_summary 取结论并向用户简短汇报。',
    active: null,
    last_run: { run_id: '20261025-200000-aaaa', status: 'completed', created_at: '2026-10-25T20:00:00+08:00' },
    notes: [
      '插件里没有启动按钮了：什么时候跑由 harness 的定时任务决定。',
      'harness 需要加载 @deepseek-ai/dsh-schedule 才有 schedule_create（最小间隔 5 分钟）。',
    ],
  },
  '/run': {
    ok: true,
    run_id: '20261025-200000-aaaa',
    state: { status: 'completed', steps: 8, videos_seen: 2, comments_posted: 1, continues: 0, auto_continues: 0 },
    summary: {
      text: '1. BV1tYHV6CEUb《Switch 移植手机》UP 肥鱼AI笔记｜播放 12.3万｜已评论：这套方案太酷了',
      degraded: false,
      chars: 78,
      raw_chars: 12480,
      saved_ratio: 0.994,
      usage: { prompt_eval_count: 3200, eval_count: 210, calls: 1 },
    },
    digest: '# digest\n（略）',
  },
}

sandbox.fetch = async (url) => {
  const path = String(url).replace('/api/bili-see', '').split('?')[0]
  const body = FIXTURES[path] ?? { ok: false, error: `no fixture ${path}` }
  return { ok: true, status: 200, json: async () => body }
}

sandbox.window.__ModuleLoader__ = { load: (def) => { sandbox.__def = def } }

/* ───────────── 加载 client.js ───────────── */

const code = readFileSync(join(ROOT, 'client.js'), 'utf8')
try {
  vm.runInNewContext(code, sandbox, { filename: 'client.js' })
  check('client.js 能在浏览器式环境里求值', true)
} catch (e) {
  check('client.js 能在浏览器式环境里求值', false, e?.message || String(e))
  process.exit(1)
}

const def = sandbox.__def
check('注册了 __ModuleLoader__ 定义', Boolean(def))
check('id 与包名一致', def?.id === 'dsh-bili-see', String(def?.id))

const bundle = def.factory((name) => {
  if (name === 'react') return React
  throw new Error(`未知模块 ${name}`)
})
check('导出 inject/apply', Array.isArray(bundle.inject) && typeof bundle.apply === 'function')

/* ───────────── 抓 slot 注册 ───────────── */

let slot = null
const ctx = {
  get: () => undefined,
  slots: {
    inject: (_name, cb) => cb(),
    register: (meta, render) => { slot = { meta, render } },
  },
  effect: (fn) => { try { fn() } catch { /* 忽略 */ } return { dispose() {} } },
}
bundle.apply(ctx)

check('注册了设置页 section', Boolean(slot))
check('侧边栏名称 = B站视频评论设置', slot?.meta?.label === 'B站视频评论设置', String(slot?.meta?.label))
check('section id = bili-see', slot?.meta?.id === 'bili-see', String(slot?.meta?.id))

/* ───────────── 渲染 ───────────── */

const resolved = new Set()
const inputValues = []
const renderOnce = async () => {
  hookIdx = 0
  pendingEffects.length = 0
  dirty = false
  const tree = slot.render()
  // 递归展开函数组件，顺便收集 hook
  let text = []
  const walk = (node) => {
    if (node === null || node === undefined || node === false) return
    if (Array.isArray(node)) { node.forEach(walk); return }
    if (typeof node === 'string' || typeof node === 'number') { text.push(String(node)); return }
    if (typeof node === 'object' && node.type) {
      if (node.type === 'input' || node.type === 'textarea' || node.type === 'select') {
        if (node.props?.value !== undefined) inputValues.push(String(node.props.value))
      }
      if (typeof node.type === 'function') {
        const props = { ...node.props, children: node.children?.length === 1 ? node.children[0] : node.children }
        walk(node.type(props))
      } else {
        node.children?.forEach(walk)
      }
    }
  }
  walk(tree)
  // 跑 effect（每个 hook 位置只跑一次），然后等微任务把 setState 都落下来
  const settle = () => new Promise((r) => setImmediate(r))
  for (const { i, fn } of pendingEffects) {
    if (resolved.has(i)) continue
    resolved.add(i)
    try {
      const r = fn()
      if (r && typeof r.then === 'function') await r
    } catch { /* 忽略 */ }
  }
  // effect 里通常是 async IIFE，setState 在微任务里；多冲几次
  for (let k = 0; k < 30; k++) await settle()
  return text
}

let allText = []
for (let round = 0; round < 6; round++) {
  allText = await renderOnce()
  if (!dirty) break
}

const page = allText.join('\n')

/* ───────────── 可选：把这一页导成静态 HTML，用来肉眼比对样式 ─────────────
 * node tests/test-client.mjs --html preview.html
 */
const htmlIdx = process.argv.indexOf('--html')
if (htmlIdx >= 0) {
  const outPath = process.argv[htmlIdx + 1] || join(ROOT, 'tests', 'preview.html')
  // --engine-bad：把引擎伪装成没就绪，用来看这种情况下的提示长什么样
  if (process.argv.includes('--engine-bad')) {
    FIXTURES['/status'].engine = {
      ok: false,
      checks: { python: true, ollama: false, model: false, bili_plus_server: true, bili_plus_db: true, credential: true },
    }
    states = []
    resolved.clear()
    for (let round = 0; round < 6; round++) {
      await renderOnce()
      if (!dirty) break
    }
  }
  hookIdx = 0
  const tree = slot.render()
  const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]))
  const VOID = new Set(['input', 'img', 'br', 'hr'])
  const toHtml = (node) => {
    if (node === null || node === undefined || node === false) return ''
    if (Array.isArray(node)) return node.map(toHtml).join('')
    if (typeof node === 'string' || typeof node === 'number') return esc(node)
    if (typeof node !== 'object' || !node.type) return ''
    if (typeof node.type === 'function') {
      const props = { ...node.props, children: node.children?.length === 1 ? node.children[0] : node.children }
      return toHtml(node.type(props))
    }
    const p = node.props || {}
    const attrs = []
    if (p.className) attrs.push(`class="${esc(p.className)}"`)
    if (p.title) attrs.push(`title="${esc(p.title)}"`)
    if (p.type) attrs.push(`type="${esc(p.type)}"`)
    if (p.placeholder) attrs.push(`placeholder="${esc(p.placeholder)}"`)
    if (p.src) attrs.push(`src="${esc(p.src)}"`)
    if (p.style && typeof p.style === 'object') {
      attrs.push(`style="${Object.entries(p.style).map(([k, v]) => `${k}:${v}`).join(';')}"`)
    }
    if (p.value !== undefined && node.type !== 'textarea') attrs.push(`value="${esc(p.value)}"`)
    const tag = node.type
    const open = `<${tag}${attrs.length ? ' ' + attrs.join(' ') : ''}`
    if (VOID.has(tag)) return `${open}>`
    const inner = node.type === 'textarea' && p.value !== undefined
      ? esc(p.value)
      : (node.children || []).map(toHtml).join('')
    return `${open}>${inner}</${tag}>`
  }
  const src = readFileSync(join(ROOT, 'client.js'), 'utf8')
  const cssMatch = src.match(/const CSS = `([\s\S]*?)`\s*\r?\n/)
  const css = cssMatch ? cssMatch[1] : ''
  const html = `<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>dsh-bili-see 设置页（样式预览）</title>
<style>
  html, body { margin: 0; background: #ffffff; color: #1f2328; }
  body { font-family: -apple-system, "Segoe UI", "Microsoft YaHei", sans-serif; padding: 20px 24px 48px; }
  .wrap { max-width: 820px; margin: 0 auto; }
  .note { font-size: 12px; color: #6e7781; margin-bottom: 14px; }
${css}
</style></head>
<body><div class="wrap">
<div class="note">静态预览 · 数据来自测试桩，非真实账号 · 仅用于比对排版与配色</div>
${toHtml(tree)}
</div></body></html>`
  writeFileSync(outPath, html, 'utf8')
  console.log(`\n已导出预览：${outPath}`)
}

/* ───────────── 断言 ───────────── */

check('页面标题出现', page.includes('B站视频评论设置'), page.slice(0, 120))
check('不再显示旧的「B站 看见」', !page.includes('B站 看见'))

// 主区块
for (const t of ['本地模型', '这一轮', '账号', '看什么', '视频详细设置', '发评论', '视频总结', '人设', '历史', '运行状态']) {
  check(`区块「${t}」在`, page.includes(t))
}

// 视频详细设置：三块配额 + 质量门槛 + 总上限
check('有「一天最多浏览」', page.includes('一天最多浏览'))
check('有 UP 粉丝下限', page.includes('UP 粉丝下限'))
check('有视频播放下限', page.includes('视频播放下限'))
check('有 AI技术（科技）子项', page.includes('AI技术（科技）'))
check('有 AI二创（娱乐）子项', page.includes('AI二创（娱乐）'))
check('有「其他视频」子项', page.includes('其他视频'))
check('其他视频提示了行格式', page.includes('名称 | 关键词 | 条数'))
check('显示配额合计', /已分配 \d+ \/ \d+ 条/.test(page), page.match(/已分配[^\n]*/)?.[0] || '')
check('配额没超时不显示溢出警告', !page.includes('超过「一天最多浏览」'))
check('显示剩余给自主浏览的条数', /还剩 \d+ 条给自主浏览/.test(page), page.match(/还剩[^\n]*/)?.[0] || '')

// 预演已经删掉
check('没有「只预演」开关', !page.includes('只预演'))
check('页面没有 dry_run 字样', !/dry[_-]?run/i.test(page))

// 视频总结
check('视频总结默认开（有开关说明）', page.includes('每轮自动总结'))
check('显示了总结正文', page.includes('BV1tYHV6CEUb'))
check('显示了节省比例', /省 \d+%/.test(page), page.match(/省 \d+%/)?.[0] || '')
check('标了「模型整合」而不是规则拼装', page.includes('模型整合'))

// 删掉的功能
check('没有草稿箱区块', !page.includes('草稿（'))
check('没有草稿相关按钮', !/删除草稿|草稿 [0-9]/.test(page))
check('没有测试模式', !page.includes('测试模式'))
check('没有测试视频输入', !page.includes('测试视频'))
// 插件内不许自带调度器（定时能力归宿主，页面只递指令）
check('插件内没有自带调度器 UI', !/谷时|自动跑|每天几轮|午休|夜间窗口|开启定时/.test(page))

// 「跑一轮」入口已删除：启动交给宿主定时任务
check('「跑一轮」按钮已移除', !page.includes('跑一轮'), '页面上还留着「跑一轮」')
check('不再有启动按钮的忙碌态文案', !page.includes('启动中…'))
check('有自动化区块', page.includes('自动化'))
check('给出 harness 定时任务指令', page.includes('bilisee_tick'))
check('说明由 harness 定时任务决定节奏', page.includes('定时任务'))

// 配置不同步：页面要把差异说出来，并给一键下发
check('不同步时提示引擎在用的数', page.includes('引擎在用的数和这一页不一样'))
check('提示里点名具体项与两边取值', page.includes('一天最多浏览') && page.includes('12'))
check('给了「重新下发给引擎」按钮', page.includes('重新下发给引擎'))

// 上下文窗口默认
check('上下文窗口默认 131072', inputValues.includes('131072'), `输入框里的值：${inputValues.join(', ')}`)
check('上下文快捷档位含 128k', page.includes('128k'))

// 文案长度：抓所有 bs-hint 文本（在元素 props 里），限 26 字
const longHints = []
const scanHints = (node) => {
  if (!node || typeof node !== 'object') return
  if (Array.isArray(node)) return node.forEach(scanHints)
  const cls = node.props?.className
  if (typeof cls === 'string' && cls.includes('bs-hint')) {
    const collect = (n) => {
      if (typeof n === 'string') return n
      if (Array.isArray(n)) return n.map(collect).join('')
      if (n && typeof n === 'object') return collect(n.children)
      return ''
    }
    const t = collect(node.children).trim()
    if (t && t.length > 26) longHints.push(t)
  }
  node.children?.forEach(scanHints)
}
// 重新渲染一次拿元素树（纯结构，不跑 effect）
hookIdx = 0
const treeForCopy = slot.render()
scanHints(treeForCopy)

check('小字文案都不超过 26 字', longHints.length === 0, longHints.map((s) => `${s}(${s.length})`).join(' | '))

// AI 味标记（只看页面上真正显示的文字，代码注释不算）
check('页面文字里没有 ⚠️/🔴/✅ 之类标记', !/[⚠🔴✅❌]/.test(page), page.match(/[⚠🔴✅❌]/)?.[0] || '')
check('没有「已下线」「不再需要」「请勿」这类机器腔', !/已下线|不再需要|请勿|注意：/.test(page))

/* ───────────── 场景二：配额溢出必须弹提示 ───────────── */

CFG.dailyVideoMax = 3          // 2 + 2 + 1 = 5 > 3
states = []
resolved.clear()
let overText = []
for (let round = 0; round < 6; round++) {
  overText = await renderOnce()
  if (!dirty) break
}
const overPage = overText.join('\n')
check('配额超上限时会提示', overPage.includes('超过「一天最多浏览」'), overPage.slice(0, 100))
check('提示里给出「把上限调大」的按钮', /把上限调到 \d+/.test(overPage), overPage.match(/把上限调到[^\n]*/)?.[0] || '')
check('溢出时能看到合计条数', /已分配 5 \/ 3 条/.test(overPage), overPage.match(/已分配[^\n]*/)?.[0] || '')

/* 引擎没就绪时，必须点名是哪一环，而不是只留一个红点 */
FIXTURES['/status'].engine = {
  ok: false,
  checks: { python: true, ollama: false, model: false, bili_plus_server: true, bili_plus_db: true, credential: true },
}
states = []
resolved.clear()
let engText = []
for (let round = 0; round < 6; round++) {
  engText = await renderOnce()
  if (!dirty) break
}
const engPage = engText.join('\n')
check('引擎未就绪时点名具体检查项', engPage.includes('引擎未就绪（Ollama/模型）'),
  engPage.match(/引擎未[^\n]*/)?.[0] || '')
check('给出处理提示而不是只有一个红点', engPage.includes('引擎自检没过'), engPage.slice(0, 80))

/* ───────────── 汇总 ───────────── */

console.log('')
console.log(`渲染到 ${allText.length} 段文字，前 12 段：`)
console.log(allText.slice(0, 12).map((t) => `  · ${t}`).join('\n'))
console.log('')
if (failures.length) {
  console.log(`❌ ${failures.length} 项失败：${failures.join(' / ')}`)
  process.exit(1)
}
console.log('✅ 前端渲染与文案检查全绿')
process.exit(0)
