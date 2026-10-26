/**
 * lib/state.mjs —— 插件家目录、原子 JSON、Python 文件铺设、时间窗口
 * ============================================================================
 * 家里的东西（`$DSH_HOME/bili-see/`）：
 *
 *   config.json            用户设置（设置页写，和宿主 settings 双写）
 *   see-config.json        **给本地模型引擎看的**那份（模型 / 上下文 / 温度 …）
 *   state.json             运行时状态：今天跑过几轮、上次报告在哪
 *   bili_credential.json   B站 凭证（扫码后落这里）
 *   bili_plus.db           去重 / 草稿 / 审计（B站 侧的）
 *   autopilot-home/        本地模型引擎的 run 目录（memory / digest / trace）
 *   shots/                 关键帧图
 *   out/<日期>.md          每轮报告
 *   duty.log               定时器与子进程日志
 *   *.py                   从包内 python/ 铺过来（MCP 服务端裸 import 兄弟模块靠这个）
 *   autopilot/             从包内 python/autopilot/ 铺过来（本地模型引擎）
 *
 * 🔴 一条硬规矩：**包目录只读，所有可变东西都在家目录**。
 *    上一代插件把凭证写进包内，结果升级一次全丢。
 * ============================================================================
 */
import {
  existsSync, mkdirSync, readFileSync, writeFileSync, renameSync,
  copyFileSync, readdirSync, statSync, unlinkSync, appendFileSync,
} from 'node:fs'
import { join, dirname } from 'node:path'
import { homedir } from 'node:os'
import { fileURLToPath } from 'node:url'

/** 包根目录（本文件在 <pkg>/lib/ 下） */
export const PKG_ROOT = join(dirname(fileURLToPath(import.meta.url)), '..')

/** 家目录名（也是 DSH 设置页里的命名空间、cordis patch 的 id） */
export const HOME_NAME = 'bili-see'

/** $DSH_HOME（宿主给的那个，拿不到就退到 ~/.dsh） */
export function dshHome(ctx) {
  // ⚠️ ctx.get('dshHomePath') 是**函数**（(...seg) => path），不是字符串。
  try {
    const getter = ctx?.get?.('dshHomePath')
    if (typeof getter === 'function') {
      const p = getter()
      if (typeof p === 'string' && p) return p
    } else if (typeof getter === 'string' && getter) {
      return getter
    }
  } catch { /* 拿不到就走下面 */ }
  return process.env.DSH_HOME || join(homedir(), '.dsh')
}

/** 插件家目录 */
export function homeDir(ctx) {
  const h = join(dshHome(ctx), HOME_NAME)
  mkdirSync(h, { recursive: true })
  return h
}

/** 逐个取家在哪儿（顺序拼接，保证目录都在） */
export function ensureDirs(home) {
  const dirs = {
    home,
    shots: join(home, 'shots'),
    out: join(home, 'out'),
    tmp: join(home, 'tmp'),
    runs: join(home, 'autopilot-home', 'runs'),
  }
  for (const d of Object.values(dirs)) mkdirSync(d, { recursive: true })
  return dirs
}

/* ─────────────────────────── JSON 读写 ─────────────────────────── */

export function readJson(file, fallback) {
  try {
    return JSON.parse(readFileSync(file, 'utf8')) ?? fallback
  } catch {
    return fallback
  }
}

/**
 * 原子写 JSON。
 * ⚠️ 直接 writeFileSync 到目标文件，写到一半断电/被杀 = 文件损坏，
 *    下次启动整个配置读不出来（凭证文件写坏过一次，所有工具一起挂）。
 */
export function writeJson(file, value) {
  const tmp = `${file}.tmp-${process.pid}`
  writeFileSync(tmp, JSON.stringify(value, null, 2), 'utf8')
  try {
    renameSync(tmp, file)
  } catch {
    try { writeFileSync(file, JSON.stringify(value, null, 2), 'utf8') } catch { /* 尽力了 */ }
    try { if (existsSync(tmp)) unlinkSync(tmp) } catch { /* 忽略 */ }
  }
  return value
}

/* ─────────────────────── 包内文件铺到家里 ─────────────────────── */

/**
 * 把 `python/*.py`、`config/persona.yaml`、`python/autopilot/` 铺进家目录。
 *
 * 为什么要铺：包升级时 node_modules 会被整个替换，但用户的凭证/数据库不能跟着动；
 * 反过来 MCP 服务端 import 的兄弟模块必须同目录（`import api` 这类裸 import），
 * 本地模型引擎也必须是一个能 `-m bili_autopilot.server` 的完整包。
 *
 * 覆盖策略：只在内容变了（size/mtime）时覆盖，避免每次启动把用户手改的东西冲掉。
 * 例外：`persona.yaml` 只有家目录里没有时才铺（用户改过就不动）。
 */
export function syncPythonFiles(home) {
  const out = { copied: [], skipped: [], dir: home }

  const src = join(PKG_ROOT, 'python')
  let names = []
  try {
    names = readdirSync(src).filter((n) => n.endsWith('.py') && !n.startsWith('_'))
  } catch (e) {
    return { ...out, error: `包内 python/ 读不到：${e?.message || e}` }
  }
  for (const name of names) copyIfChanged(join(src, name), join(home, name), out)

  /* 本地模型引擎：整棵子树铺过去（要保持 bili_autopilot/ 这个包名） */
  const apSrc = join(src, 'autopilot')
  const apDst = join(home, 'autopilot')
  if (existsSync(apSrc)) {
    copyTreeIfChanged(apSrc, apDst, out)
  } else {
    out.skipped.push('autopilot/(包内没有)')
  }

  /* config/persona.yaml → <home>/persona.yaml（persona._persona_path() 找这个） */
  const personaSrc = join(PKG_ROOT, 'config', 'persona.yaml')
  const personaDst = join(home, 'persona.yaml')
  if (existsSync(personaSrc)) {
    if (existsSync(personaDst)) out.skipped.push('persona.yaml(家目录已有，保留用户的)')
    else copyIfChanged(personaSrc, personaDst, out)
  } else {
    out.skipped.push('persona.yaml(包内 config/ 里没有)')
  }

  return out
}

/** 内容没变就跳过，变了才拷。单文件失败只记 skipped，不拖垮整次启动。 */
function copyIfChanged(from, to, out) {
  const name = from.split(/[\\/]/).pop()
  try {
    const a = statSync(from)
    const b = existsSync(to) ? statSync(to) : null
    if (b && b.size === a.size && b.mtimeMs >= a.mtimeMs) {
      out.skipped.push(name)
      return
    }
    copyFileSync(from, to)
    out.copied.push(name)
  } catch (e) {
    out.skipped.push(`${name}(失败:${e?.message || e})`)
  }
}

/** 递归铺一棵树，跳过 __pycache__ / .pyc */
function copyTreeIfChanged(from, to, out) {
  try { mkdirSync(to, { recursive: true }) } catch { /* 已存在 */ }
  let entries = []
  try { entries = readdirSync(from, { withFileTypes: true }) } catch { return }
  for (const ent of entries) {
    if (ent.name === '__pycache__' || ent.name.endsWith('.pyc')) continue
    const s = join(from, ent.name)
    const d = join(to, ent.name)
    if (ent.isDirectory()) copyTreeIfChanged(s, d, out)
    else copyIfChanged(s, d, out)
  }
}

/* ─────────────────────────── 时间窗口 ─────────────────────────── */

/**
 * 谷时窗口（本地时间）。
 *   noon  12:00–14:00   午休
 *   night 00:30–08:30   过夜（B站 这个时段风控更松，也不打扰人）
 * `[start, end)`，支持跨午夜。
 */
export const WINDOWS = {
  noon: { start: [12, 0], end: [14, 0], label: '午休 12:00–14:00' },
  night: { start: [0, 30], end: [8, 30], label: '夜间 00:30–08:30' },
}

/** 当前在不在某个窗口里 → 返回窗口名或 null */
export function currentWindow(now = new Date()) {
  const mins = now.getHours() * 60 + now.getMinutes()
  for (const [name, w] of Object.entries(WINDOWS)) {
    const s = w.start[0] * 60 + w.start[1]
    const e = w.end[0] * 60 + w.end[1]
    const inside = s <= e ? (mins >= s && mins < e) : (mins >= s || mins < e)
    if (inside) return name
  }
  return null
}

/** 距离下一个窗口开始还有多少毫秒（用于定时器对齐） */
export function msToNextWindow(now = new Date()) {
  const mins = now.getHours() * 60 + now.getMinutes()
  let best = Infinity
  for (const w of Object.values(WINDOWS)) {
    const s = w.start[0] * 60 + w.start[1]
    let diff = s - mins
    if (diff <= 0) diff += 24 * 60
    best = Math.min(best, diff)
  }
  return best * 60_000
}

/** 今天（本地）的日期串，YYYY-MM-DD */
export function dayKey(d = new Date()) {
  const p = (n) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`
}

/* ─────────────────────────── 运行时状态 ─────────────────────────── */

const EMPTY_STATE = {
  day: '',
  runs: 0,
  picked: 0,
  posted: 0,
  lastRunAt: 0,
  lastRunOk: null,
  lastRunId: '',
  lastReport: '',
  lastError: '',
}

export function readState(home) {
  const s = readJson(join(home, 'state.json'), {})
  const today = dayKey()
  if (s.day !== today) {
    return {
      ...EMPTY_STATE, day: today,
      lastReport: s.lastReport || '', lastRunAt: s.lastRunAt || 0,
      lastRunId: s.lastRunId || '', lastRunOk: s.lastRunOk ?? null,
    }
  }
  return { ...EMPTY_STATE, ...s }
}

export function writeState(home, patch) {
  const next = { ...readState(home), ...patch }
  writeJson(join(home, 'state.json'), next)
  return next
}

/* ─────────────────────────── 日志 ─────────────────────────── */

export function appendLog(home, line) {
  try {
    const file = join(home, 'duty.log')
    try {
      if (existsSync(file) && statSync(file).size > 2 * 1024 * 1024) writeFileSync(file, '', 'utf8')
    } catch { /* 忽略 */ }
    appendFileSync(file, `[${new Date().toISOString()}] ${line}\n`, 'utf8')
  } catch { /* 日志失败不能影响主流程 */ }
}

/** 读日志尾巴（设置页「最近日志」用） */
export function tailLog(home, maxLines = 40) {
  try {
    const file = join(home, 'duty.log')
    if (!existsSync(file)) return []
    const text = readFileSync(file, 'utf8')
    return text.split(/\r?\n/).filter(Boolean).slice(-maxLines)
  } catch {
    return []
  }
}
