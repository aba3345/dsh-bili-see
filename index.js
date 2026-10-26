/**
 * dsh-bili-see —— host 半端
 * ============================================================================
 * 它是 dsh-bili-plus + bili-autopilot 的合并体，名字叫「B站 看见」。
 *
 * 两件事合在一起：
 *
 *   ① **看**（原 dsh-bili-plus）：读视频信息 / 字幕 / 热评 / 关键帧，
 *      带四道安全闸门的发评论能力。纯 HTTP + Pillow，不需要浏览器。
 *
 *   ② **自己看**（原 bili-autopilot）：把本地小模型（qwen3.5:4b）接上来，
 *      让它**自己**选视频、读字幕、判断值不值得评论、写评论、发出去。
 *      上下文爆了会自动压缩记忆再「继续」，进程死了会被外层重新拉起 ——
 *      上层大模型（DeepSeek）不再手工触发：一轮由**宿主自带的定时任务**到点唤起，
 *      模型只调一次 `bilisee_tick`（非阻塞）把活交出去，之后读 summary 汇报。
 *
 * ⚠️ 本插件**不提供**「跑一轮」按钮和阻塞式 run 工具：
 *     节奏归宿主（schedule_create → 唤起一轮对话 → bilisee_tick），
 *     插件只负责「被叫醒时把本地模型这一轮干起来」，以及把结果整理成摘要。
 *
 * 架构（三层，每层都能单独换）：
 *
 *     DSH 宿主 ──ctx.tools.register──> index.js（工具 / 设置页 / HTTP 路由）
 *                                        │
 *                                        ├─ stdio MCP ──> python/mcp_server.py   (B站 访问)
 *                                        └─ stdio MCP ──> python/autopilot/      (本地模型引擎)
 *                                                              │
 *                                                              └─ 自己再拉起 B站 那一层
 *
 * 为什么本地模型引擎要单独一个 MCP 子进程：它要跑十几分钟、要写自己的 run 目录、
 * 要能被「继续」重启。塞进宿主进程里会跟着 DSH 一起卡；做成子进程，
 * 关掉界面它也能跑完。
 * ============================================================================
 */
import { join } from 'node:path'
import { existsSync, readFileSync, readdirSync, statSync } from 'node:fs'
import { McpClient } from './lib/mcp-client.mjs'
import { findPython, installHint } from './lib/python-env.mjs'
import { loadHostDeps } from './lib/hostdeps.mjs'
import {
  PKG_ROOT, HOME_NAME, homeDir, dshHome, ensureDirs, readJson, writeJson, syncPythonFiles,
  readState, writeState, appendLog, tailLog,
} from './lib/state.mjs'

// ⭐ 这两样从**宿主那棵树**里取（拿不到就退到自己的依赖）。
//    裸 import 会让整条插件在 optional peer 缺失时不激活。
const hostDeps = await loadHostDeps()
const z = hostDeps.z
const hostDefineTool = hostDeps.defineTool

/** 插件名。同时是 cordis.patch.yml 里 loader 条目的 id。 */
export const name = HOME_NAME
export const inject = ['webServer', 'tools', 'settings']

/* ─────────────────────── Config（设置页的表单） ─────────────────────── */
/**
 * 每个字段都必须 `.extra('volatile', true)`，否则设置页写不进来
 * （DSH 的 settings.update 只认 volatile 字段）。
 *
 * 🔴 两个列表类字段用**逗号分隔字符串**而不是 z.array()：
 *    数组型 schema 在宿主那条 defineTool 链上更容易踩 JsonSchemaError
 *    ⇒ 整条插件不激活。字符串我们自己 split。
 */
export const Config = z?.object ? z.object({
  /* ── 每轮 ── */
  videosPerRun: z.natural().default(2).extra('volatile', true),

  /* ── 本地模型 ──
   * 这些值会写进 <home>/see-config.json，本地模型引擎每次跑都读它。
   * ⇒ 改完不用重启插件，下一轮就生效。 */
  model: z.string().default('qwen3.5:4b-32k').extra('volatile', true),
  ollamaUrl: z.string().default('http://127.0.0.1:11434').extra('volatile', true),
  numCtx: z.natural().default(131072).extra('volatile', true),
  numPredict: z.natural().default(1200).extra('volatile', true),
  temperature: z.number().default(0.35).extra('volatile', true),
  maxSteps: z.natural().default(40).extra('volatile', true),
  numMinutes: z.natural().default(20).extra('volatile', true),
  autoContinue: z.boolean().default(true).extra('volatile', true),
  autoContinueMax: z.natural().default(2).extra('volatile', true),
  /** bilisee_tick 拿到 run_id 最多等多久（秒）。**只是拿 id 的等待**，不是等跑完 —— 跑完靠 bilisee_status / bilisee_summary */
  waitSec: z.natural().default(15).extra('volatile', true),

  /* ── 发评论（危险区） ── */
  writeEnabled: z.boolean().default(false).extra('volatile', true),
  maxPostsPerDay: z.natural().default(3).extra('volatile', true),
  minIntervalMin: z.natural().default(60).extra('volatile', true),

  /* ── 看什么 ── */
  freshDays: z.natural().default(2).extra('volatile', true),
  watchMids: z.string().default('').extra('volatile', true),

  /* ── 视频质量门槛（关注 UP 不受限） ── */
  minUpFans: z.natural().default(10000).extra('volatile', true),
  minPlay: z.natural().default(10000).extra('volatile', true),

  /* ── 视频详细设置：一天最多浏览 + 三块配额 ── */
  dailyVideoMax: z.natural().default(6).extra('volatile', true),
  aiTechQuota: z.natural().default(3).extra('volatile', true),
  aiTechKeywords: z.string().default('AI 大模型,开源模型,编程工具,推理框架,AI 实测').extra('volatile', true),
  aiFunQuota: z.natural().default(2).extra('volatile', true),
  aiFunKeywords: z.string().default('AI 绘画,AI 配音,AI 整活,AI 生成,AI 玩梗').extra('volatile', true),
  /** 其他视频：一行一条 `名称 | 关键词1,关键词2 | 配额` */
  otherPrefs: z.string().default('').extra('volatile', true),

  /* ── 视频总结 ── */
  summaryEnabled: z.boolean().default(true).extra('volatile', true),

  /* ── 环境 ── */
  personaPath: z.string().default('').extra('volatile', true),
  pythonPath: z.string().default('').extra('volatile', true),
  reportLevel: z.string().default('full').extra('volatile', true),
}) : undefined

/* ─────────────────────── 默认值（没有 Config 时也有一份） ─────────────────────── */

const DEFAULTS = {
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
  waitSec: 15,
  writeEnabled: false,
  maxPostsPerDay: 3,
  minIntervalMin: 60,
  freshDays: 2,
  watchMids: '',
  minUpFans: 10000,
  minPlay: 10000,
  dailyVideoMax: 6,
  aiTechQuota: 3,
  aiTechKeywords: 'AI 大模型,开源模型,编程工具,推理框架,AI 实测',
  aiFunQuota: 2,
  aiFunKeywords: 'AI 绘画,AI 配音,AI 整活,AI 生成,AI 玩梗',
  otherPrefs: '',
  summaryEnabled: true,
  personaPath: '',
  pythonPath: '',
  reportLevel: 'full',
}

/** 哪些键变了要让 MCP 子进程重建（因为它们是起进程时注入的） */
const ENV_KEYS = ['writeEnabled', 'maxPostsPerDay', 'minIntervalMin', 'model', 'ollamaUrl']
/** 哪些键是给本地模型引擎的（写进 see-config.json） */
const ENGINE_KEYS = [
  'model', 'ollamaUrl', 'numCtx', 'numPredict', 'temperature', 'maxSteps', 'numMinutes',
  'autoContinue', 'autoContinueMax', 'videosPerRun', 'freshDays', 'watchMids',
  'writeEnabled', 'maxPostsPerDay', 'minIntervalMin',
  'minUpFans', 'minPlay', 'dailyVideoMax',
  'aiTechQuota', 'aiTechKeywords', 'aiFunQuota', 'aiFunKeywords', 'otherPrefs',
  'summaryEnabled',
]

/* ─────────────────────────── apply ─────────────────────────── */

export function apply(ctx, config) {
  const logLines = []
  const log = (msg) => {
    const line = `[bili-see] ${msg}`
    logLines.push(line)
    if (logLines.length > 300) logLines.shift()
    try { ctx?.logger?.info?.(line) } catch { /* 没 logger 就算了 */ }
    try { appendLog(HOME, `${msg}`) } catch { /* 家目录还没准备好 */ }
  }

  /* ── 家目录 ── */
  const HOME = homeDir(ctx)
  const DIRS = ensureDirs(HOME)
  const CFG_FILE = join(HOME, 'config.json')
  const SEE_CFG_FILE = join(HOME, 'see-config.json')
  const ENGINE_DIR = join(HOME, 'autopilot')
  const RUNS_DIR = join(HOME, 'autopilot-home', 'runs')

  /* ── 配置读 ──
   * 宿主设置层是权威（settings 层写进来的）；本地 config.json 是兜底 + 缓存。
   * 两个都读，宿主优先。 */
  let settingsCache = null
  let settingsCacheAt = 0
  const invalidateSettings = () => { settingsCache = null; settingsCacheAt = 0 }

  const settingsDesc = (force = false) => {
    const now = Date.now()
    if (!force && settingsCache && now - settingsCacheAt < 1500) return settingsCache
    settingsCacheAt = now
    try {
      const api = ctx?.settings
      if (api?.get && typeof api.get === 'function') {
        const all = api.get()
        const rows = Array.isArray(all) ? all : (all?.settings || all?.rows || [])
        const row = (rows || []).find((r) => r && (r.ns === name || r.namespace === name || r.id === name))
        if (row) { settingsCache = row; return row }
      }
    } catch { /* 拿不到就走本地 */ }
    settingsCache = null
    return null
  }

  /**
   * ⚠️ `cfg` 必须先声明再让 refreshCfg() 赋值 —— 反过来是 TDZ：
   *    `let cfg = refreshCfg()` 里 refreshCfg 体内 `cfg = base` 会撞
   *    "Cannot access 'cfg' before initialization"，整条插件加载失败。
   */
  let cfg = { ...DEFAULTS }
  /**
   * 只认 DEFAULTS 里有的键。
   * 老版本留在 config.json 里的键（dryRunDefault / keywords / testMode …）
   * 如果原样合并进来，会一路漏到 /config 返回值和设置页上 ——
   * 用户看到的是"删掉的功能又回来了"。这里直接丢掉。
   */
  const KNOWN_KEYS = new Set(Object.keys(DEFAULTS))
  const pick = (obj) => {
    const out = {}
    if (!obj || typeof obj !== 'object') return out
    for (const [k, v] of Object.entries(obj)) {
      if (KNOWN_KEYS.has(k) && v !== undefined && v !== null) out[k] = v
    }
    return out
  }
  /**
   * 把设置页的驼峰键翻成引擎认识的 snake_case。
   * 引擎的 config.py 会读这个文件覆盖默认值 ⇒ 前端改模型参数不用改 MCP 工具签名。
   */
  const engineConfig = (c) => {
    const num = (v, d) => (Number.isFinite(Number(v)) ? Number(v) : d)
    return {
      model: String(c.model || DEFAULTS.model),
      ollama_url: String(c.ollamaUrl || DEFAULTS.ollamaUrl).replace(/\/+$/, ''),
      num_ctx: num(c.numCtx, 32768),
      num_predict: num(c.numPredict, 1200),
      temperature: num(c.temperature, 0.35),
      max_steps: num(c.maxSteps, 40),
      max_minutes: num(c.numMinutes, 20),
      auto_continue: Boolean(c.autoContinue),
      auto_continue_max: num(c.autoContinueMax, 2),
      max_videos: num(c.videosPerRun, 2),
      fresh_days: num(c.freshDays, 2),
      watch_mids: csv(c.watchMids),
      write: Boolean(c.writeEnabled),
      max_posts_per_day: num(c.maxPostsPerDay, 3),
      min_interval_min: num(c.minIntervalMin, 60),
      // ── 质量门槛 / 三块配额 / 视频总结 ──
      min_up_fans: num(c.minUpFans, 10000),
      min_play: num(c.minPlay, 10000),
      daily_video_max: num(c.dailyVideoMax, 6),
      ai_tech_quota: num(c.aiTechQuota, 3),
      ai_tech_keywords: csv(c.aiTechKeywords),
      ai_fun_quota: num(c.aiFunQuota, 2),
      ai_fun_keywords: csv(c.aiFunKeywords),
      other_prefs: String(c.otherPrefs || ''),
      summary_enabled: c.summaryEnabled !== false,
    }
  }

  /** `"a, b ,c"` → `['a','b','c']`；空串 → `[]` */
  const csv = (v) => String(v ?? '').split(',').map((s) => s.trim()).filter(Boolean)

  /**
   * 把「生效配置」下发给引擎 —— 这是前端与引擎不同步的根。
   *
   * 以前只在用户点保存时写 see-config.json，而且写的是「本地文件 + 这次改动」，
   * 于是：宿主设置里改过的键根本进不去引擎；本地文件缺的键会让引擎退到自己的默认值。
   * 结果就是设置页写着「一天 6 条」，引擎按 12 条在跑。
   *
   * 现在：**谁都没资格单独决定**，统一从 cfg（DEFAULTS ← config.json ← 宿主设置）投影出来，
   * 内容变了才落盘，避免每次 refreshCfg 都写文件。
   */
  let lastEngineJson = ''
  const syncEngineConfig = (force = false) => {
    const next = engineConfig(cfg)
    const text = JSON.stringify(next, null, 2)
    if (!force && text === lastEngineJson) return false
    if (!force) {
      let cur = ''
      try { cur = readFileSync(SEE_CFG_FILE, 'utf8') } catch { /* 还没有这份文件 */ }
      if (cur.trim() === text.trim()) { lastEngineJson = text; return false }
    }
    writeJson(SEE_CFG_FILE, next)
    lastEngineJson = text
    log(`配置下发给引擎：daily=${next.daily_video_max} 上限=${next.max_posts_per_day} 间隔=${next.min_interval_min} write=${next.write} model=${next.model}`)
    return true
  }

  const refreshCfg = () => {
    const base = { ...DEFAULTS }
    Object.assign(base, pick(readJson(CFG_FILE, {})))
    const row = settingsDesc()
    Object.assign(base, pick(row?.config ?? row?.value ?? null))
    cfg = base
    // 读完之后立刻对齐引擎，别让第三份快照活下去
    try { syncEngineConfig() } catch { /* 写盘失败不影响读配置 */ }
    return cfg
  }
  refreshCfg()

  /**
   * 保存：以**生效值**为基准落盘。
   * 以前写的是「本地文件 + patch」，宿主侧的键会被本地旧值反覆盖 —— 现在 cfg 已经是合并结果，
   * 直接用它当快照，本地文件从此是「生效状态的镜像」，不再是一份独立的事实来源。
   */
  const saveCfgLocal = (patch) => {
    cfg = { ...cfg, ...pick(patch) }
    writeJson(CFG_FILE, pick(cfg))
    syncEngineConfig(true)
    return cfg
  }


  /* ── Python ── */
  let pyInfo = null
  let pyError = (() => {
    try {
      const r = syncPythonFiles(HOME)
      log(`铺 Python 文件：拷 ${r.copied.length}，跳过 ${r.skipped.length}${r.error ? `，${r.error}` : ''}`)
      return ''
    } catch (e) {
      return `铺 Python 文件失败：${e?.message || e}`
    }
  })()

  const ensurePython = () => {
    if (pyInfo?.path) return pyInfo
    refreshCfg()
    // 设置页里填了路径就让它排到候选第一位（python-env.mjs 会读这个环境变量）
    const forced = String(cfg.pythonPath || '').trim()
    if (forced) process.env.BILI_SEE_PYTHON = forced
    pyInfo = findPython({ dshHome: dshHome(ctx), log }) || null
    if (!pyInfo) {
      pyError = `找不到可用的 Python。\n${installHint()}`
    } else if (!pyInfo.hasDeps) {
      pyError = `${pyInfo.path} 能用但缺依赖（${pyInfo.missingDeps}）\n${installHint(pyInfo.path)}`
    } else {
      pyError = ''
    }
    return pyInfo
  }

  /* ── 两个 MCP 子进程 ── */

  /** B站 访问层（长连接，按需启动） */
  let biliMcp = null
  let biliStarting = null
  /** 本地模型引擎（按需启动，改了模型配置就重建） */
  let apMcp = null
  let apStarting = null
  let apSigAlive = ''

  /**
   * 子进程一律绕开系统代理访问本机服务。
   *
   * 宿主进程常年带着 `HTTP_PROXY`（有些是装的时候留下的、指向早就没了的本地代理），
   * Python 的 httpx 默认 trust_env=True 会把这个代理套到 `127.0.0.1:11434` 上，
   * 于是 Ollama 探测拿到 502 —— 页面就变成「引擎未就绪」，但 Ollama 明明在跑。
   * 本机地址不该经过任何代理，这里把本地回环塞进 NO_PROXY。
   */
  const noProxyLocal = () => {
    const local = '127.0.0.1,localhost,::1'
    const cur = String(process.env.NO_PROXY || process.env.no_proxy || '')
    const merged = !cur ? local : (cur.includes('127.0.0.1') ? cur : `${cur},${local}`)
    return { NO_PROXY: merged, no_proxy: merged }
  }

  const biliEnv = () => {
    refreshCfg()
    const allowed = [
      'status', 'credential_check', 'video_info', 'subtitle', 'ai_conclusion', 'comments',
      'search', 'popular', 'user_videos', 'safety_check', 'style_check', 'persona_prompt',
      'login_qr_start', 'login_qr_poll', 'draft_list', 'draft_update', 'draft_delete',
      'verify_posted', 'pause', 'resume', 'audit', 'persona_read', 'persona_save',
      'rank', 'user_info',
    ]
    if (cfg.writeEnabled) {
      allowed.push('draft_create', 'comment_post', 'comment_delete')
    }
    return {
      BILI_PLUS_HOME: HOME,
      DSH_HOME: dshHome(ctx),
      BILI_PLUS_WRITE: cfg.writeEnabled ? '1' : '0',
      BILI_PLUS_TOOLS: [...new Set(allowed)].join(','),
      BILI_PLUS_MAX_POSTS_PER_DAY: String(cfg.maxPostsPerDay),
      BILI_PLUS_MIN_INTERVAL_MIN: String(cfg.minIntervalMin),
      ...noProxyLocal(),
    }
  }

  const biliEnvSig = () => `${cfg.writeEnabled ? 1 : 0}|${cfg.maxPostsPerDay}|${cfg.minIntervalMin}`
  let biliSigAlive = ''

  const startBiliMcp = async () => {
    const py = ensurePython()
    if (!py) throw new Error(pyError || 'Python 不可用')
    const server = join(HOME, 'mcp_server.py')
    if (!existsSync(server)) throw new Error(`找不到 B站 服务端：${server}`)
    const client = new McpClient({
      command: py.path,
      args: [server],
      cwd: HOME,
      env: biliEnv(),
      log,
    })
    await client.start()
    biliSigAlive = biliEnvSig()
    log(`B站 MCP 起来了（${py.path}，写=${cfg.writeEnabled ? '开' : '关'}）`)
    return client
  }

  const ensureBili = async () => {
    refreshCfg()
    if (biliMcp?.proc && biliSigAlive !== biliEnvSig()) {
      log(`B站 MCP 环境变了，重建子进程`)
      stopBili()
    }
    if (biliMcp?.proc) return biliMcp
    if (!biliStarting) biliStarting = startBiliMcp().finally(() => { biliStarting = null })
    biliMcp = await biliStarting
    return biliMcp
  }

  const stopBili = () => {
    try { biliMcp?.kill() } catch { /* 收摊 */ }
    biliMcp = null
  }

  /**
   * 本地模型引擎的环境。
   * ⚠️ BILI_PLUS_HOME 指到**本插件家目录** —— 引擎会自己再拉起一个
   *    mcp_server.py（就是 home 里那份），所以它和上面那个 MCP 用的是同一个
   *    B站 凭证 / 同一个数据库。这是有意的：一份状态，不会对不上。
   */
  const apEnv = () => {
    refreshCfg()
    // ⚠️ 起引擎前先把当前设置落成 see-config.json（走同一个投影函数，别再写第二份）。
    //    不然「第一次跑」会用到引擎自带的旧默认值（比如 num_ctx 32768），
    //    跟设置页显示的对不上 —— 用户改了却没生效，最难查。
    try { syncEngineConfig(true) } catch { /* 写不进去就退回引擎默认 */ }
    return {
      BILI_AUTOPILOT_PYTHON: ensurePython()?.path || 'python',
      BILI_AUTOPILOT_HOME: join(HOME, 'autopilot-home'),
      BILI_AUTOPILOT_CONFIG_FILE: SEE_CFG_FILE,
      BILI_AUTOPILOT_ALLOW_WRITE: cfg.writeEnabled ? '1' : '0',
      BILI_AUTOPILOT_MODEL: cfg.model,
      OLLAMA_HOST: cfg.ollamaUrl,
      BILI_PLUS_HOME: HOME,
      DSH_HOME: dshHome(ctx),
      PYTHONPATH: ENGINE_DIR,
      ...noProxyLocal(),
    }
  }

  const apEnvSig = () => `${cfg.writeEnabled ? 1 : 0}|${cfg.model}|${cfg.ollamaUrl}`

  const startApMcp = async () => {
    const py = ensurePython()
    if (!py) throw new Error(pyError || 'Python 不可用')
    if (!existsSync(join(ENGINE_DIR, 'bili_autopilot', 'server.py'))) {
      throw new Error(`本地模型引擎没铺好：${join(ENGINE_DIR, 'bili_autopilot')}`)
    }
    const client = new McpClient({
      command: py.path,
      args: ['-m', 'bili_autopilot.server'],
      cwd: ENGINE_DIR,
      env: apEnv(),
      log: (m) => log(`[engine] ${m}`),
    })
    await client.start()
    apSigAlive = apEnvSig()
    log(`本地模型引擎起来了（model=${cfg.model}）`)
    return client
  }

  const ensureAp = async () => {
    refreshCfg()
    if (apMcp?.proc && apSigAlive !== apEnvSig()) {
      log('模型配置变了，重建引擎子进程')
      stopAp()
    }
    if (apMcp?.proc) return apMcp
    if (!apStarting) apStarting = startApMcp().finally(() => { apStarting = null })
    apMcp = await apStarting
    return apMcp
  }

  const stopAp = () => {
    try { apMcp?.kill() } catch { /* 收摊 */ }
    apMcp = null
  }

  /** 调 B站 工具 */
  const callBili = async (toolName, args = {}, opts = {}) => {
    const client = await ensureBili()
    return client.call(toolName, args, opts.timeoutMs)
  }

  /** 调本地模型引擎 */
  const callAp = async (toolName, args = {}, opts = {}) => {
    const client = await ensureAp()
    return client.call(toolName, args, opts.timeoutMs)
  }

  /** 把 Python 侧返回统一翻成一个「永远能读」的对象 */
  const fromPy = (r, extra = {}) => {
    if (r?.json) return { ...r.json, ...extra }
    return { ok: r?.ok !== false, raw: r?.text || '', ...extra }
  }

  /* ── 安全序列化 ── */
  const plain = (v) => {
    if (v === undefined || v === null) return v
    if (typeof v === 'string' || typeof v === 'number' || typeof v === 'boolean') return v
    try { return JSON.parse(JSON.stringify(v)) } catch { return String(v) }
  }
  /** 宿主只吃无损 JSON；工具执行里任何异常都变结构化返回，绝不往上抛 */
  const safe = (v) => {
    try { return plain(v) } catch (e) {
      return { ok: false, error: `返回值无法序列化: ${e?.message || e}` }
    }
  }

  /* ─────────────────────── Ollama 探测（给前端下拉框用） ─────────────────────── */

  const probeOllama = async (url, timeoutMs = 4000) => {
    const base = String(url || DEFAULTS.ollamaUrl).replace(/\/+$/, '')
    const ac = new AbortController()
    const timer = setTimeout(() => ac.abort(), timeoutMs)
    try {
      const res = await fetch(`${base}/api/tags`, { signal: ac.signal })
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      const data = await res.json()
      const models = (data?.models || []).map((m) => ({
        name: m.name || m.model,
        size: m.size,
        family: m.details?.family || '',
        params: m.details?.parameter_size || '',
        quant: m.details?.quantization_level || '',
        modified: m.modified_at || '',
      }))
      return { ok: true, url: base, models, count: models.length }
    } catch (e) {
      return {
        ok: false,
        url: base,
        models: [],
        error: ac.signal.aborted ? `连接 ${base} 超时` : `连不上 Ollama：${e?.message || e}`,
        hint: '确认 Ollama 在跑：`ollama serve`（默认 127.0.0.1:11434）',
      }
    } finally {
      clearTimeout(timer)
    }
  }

  /* ─────────────────────── run 目录读写（前端进度用） ─────────────────────── */

  const readRunState = (runId) => {
    if (!runId) return null
    return readJson(join(RUNS_DIR, runId, 'state.json'), null)
  }

  const readRunDigest = (runId, maxChars = 20000) => {
    try {
      const text = readFileSync(join(RUNS_DIR, runId, 'digest.md'), 'utf8')
      return text.length > maxChars ? `${text.slice(0, maxChars)}\n…（已截断）` : text
    } catch { return '' }
  }

  const listRuns = (limit = 12) => {
    let names = []
    try {
      names = readdirSync(RUNS_DIR, { withFileTypes: true })
        .filter((d) => d.isDirectory())
        .map((d) => d.name)
    } catch { return [] }
    const rows = []
    for (const id of names) {
      const st = readRunState(id) || {}
      let mtime = 0
      try { mtime = statSync(join(RUNS_DIR, id)).mtimeMs } catch { /* 忽略 */ }
      rows.push({
        run_id: id,
        status: st.status || 'unknown',
        phase: st.phase || '',
        steps: st.steps || 0,
        continues: st.continues || 0,
        auto_continues: st.auto_continues || 0,
        videos_seen: st.videos_seen || 0,
        comments_posted: st.comments_posted || 0,
        model: st.model || '',
        created_at: st.created_at || '',
        finished_at: st.finished_at || '',
        last_error: st.last_error || '',
        mtime,
        has_digest: existsSync(join(RUNS_DIR, id, 'digest.md')),
      })
    }
    rows.sort((a, b) => b.mtime - a.mtime)
    return rows.slice(0, Math.max(1, Math.min(Number(limit) || 12, 50)))
  }

  const activeRun = () => listRuns(50).find((r) => ['starting', 'running', 'resuming'].includes(r.status)) || null

  /** 「其他视频」一行的配额加起来（前端的溢出提示要用同一套算法） */
  const otherQuotaTotal = (text) => String(text || '')
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter((line) => line && !line.startsWith('#'))
    .reduce((sum, line) => {
      const parts = line.split('|').map((x) => x.trim())
      const quota = Number(parts[2])
      return sum + (Number.isFinite(quota) && quota > 0 ? quota : 1)
    }, 0)

  /**
   * 三块配额 vs 一天上限。
   * 超了**不改用户的值**，只把事实报给前端，由页面提示"去调大一天最多浏览"。
   */
  const quotaState = () => {
    const daily = Math.max(1, Number(cfg.dailyVideoMax) || 6)
    const tech = Math.max(0, Number(cfg.aiTechQuota) || 0)
    const fun = Math.max(0, Number(cfg.aiFunQuota) || 0)
    const other = otherQuotaTotal(cfg.otherPrefs)
    const wanted = tech + fun + other
    return {
      daily_video_max: daily,
      ai_tech: tech,
      ai_fun: fun,
      other,
      wanted,
      overflow: wanted > daily,
      free_after: Math.max(0, daily - wanted),
    }
  }

  /** 最近一轮的本地模型视频总结（设置页展示用） */  const lastSummary = () => {
    const rows = listRuns(1)
    const id = rows[0]?.run_id
    if (!id) return null
    const mem = readJson(join(RUNS_DIR, id, 'memory.json'), null)
    const s = mem?.summary
    if (!s?.text) return null
    return {
      run_id: id,
      text: s.text,
      degraded: Boolean(s.degraded),
      chars: s.summary_chars,
      raw_chars: s.raw_chars,
      saved_ratio: s.saved_ratio,
      usage: s.usage || {},
    }
  }

  /* ─────────────────────── 工具 ─────────────────────── */

  const textOutput = {
    schema: { type: 'object', properties: {}, additionalProperties: true },
    render: (_args, value) => [{
      type: 'text',
      text: typeof value === 'string' ? value : JSON.stringify(value, null, 2),
    }],
  }

  const defineTool = (spec) => (hostDefineTool ? hostDefineTool(spec) : spec)
  const tool = (spec) => ctx.tools.register(defineTool({
    ...spec,
    output: textOutput,
    async execute(args, exec) { return safe(await spec.execute(args || {}, exec)) },
  }))

  /* ── ① 本地模型：一轮（只由宿主自动化调用，不提供人工触发） ── */
  /**
   * 这里**没有**「跑一轮」按钮，也没有 POST /run：
   * 到点触发交给 harness 自带的定时任务（schedule_create 唤起一轮对话，模型在里面调这个工具）。
   * 插件的职责只剩「被叫醒时把活干起来」，节奏归宿主。
   */

  tool({
    name: 'bilisee_tick',
    description: '**只给宿主定时任务用**：把一轮本地模型巡航启动起来，立即返回 run_id，不等结果。\n'
      + '它不是给人点着玩的按钮 —— 什么时候跑由 harness 的定时任务决定，这里只负责被叫醒时开始干活。\n'
      + '返回后不要原地等：用 bilisee_status 看进度、bilisee_summary 取结论。\n'
      + '幂等：已经有一轮在跑时不会重开，直接返回那一轮的 run_id。\n'
      + '本地模型上下文爆了会自动压缩记忆接着跑，进程死了会被自动拉起 —— 这些都在插件内部完成。\n'
      + '候选已按「粉丝 > 门槛、播放 > 门槛」筛过，关注的 UP 不受限。没有预演模式，write=true 就是真发。',
    parameters: {
      write: { type: 'boolean', description: '是否允许写（建草稿/发评论）。默认跟随插件设置' },
      max_videos: { type: 'number', description: '这一轮最多看几条视频（默认跟随设置，且不超过今天剩余额度）' },
      max_comments: { type: 'number', description: '最多发几条评论（默认 1）' },
      task: { type: 'string', description: '给本地模型的任务描述（留空用默认的）' },
      force: { type: 'boolean', description: '已有 run 在跑时是否强行换新的（会杀掉旧的）' },
    },
    async execute(args) {
      refreshCfg()
      const active = activeRun()
      if (active && !args.force) {
        return {
          ok: true, triggered: false, already_running: true, run_id: active.run_id, status: active.status,
          hint: `已经有一轮在跑（${active.run_id}），这次不重开。稍后用 bilisee_status 看进度。`,
        }
      }
      /* 只等「拿到 run_id」这一小段；跑多久与我们无关，由引擎自己走完 */
      const idWaitSec = Math.max(3, Math.min(Number(cfg.waitSec) || 15, 30))
      const write = args.write === undefined ? Boolean(cfg.writeEnabled) : Boolean(args.write)
      const callArgs = {
        write,
        max_videos: Math.max(1, Math.min(Number(args.max_videos) || cfg.videosPerRun, 10)),
        max_comments: Math.max(0, Math.min(Number(args.max_comments) || 1, 5)),
        max_minutes: cfg.numMinutes,
        wait_seconds: idWaitSec,
        force: Boolean(args.force),
      }
      if (args.task) callArgs.task = String(args.task)

      const r = await callAp('run', callArgs, { timeoutMs: 60_000 })
      const out = fromPy(r)
      if (out.run_id && out.status) {
        try {
          writeState(HOME, {
            lastRunAt: Date.now(),
            lastRunOk: out.status === 'completed',
            lastRunId: out.run_id,
            lastError: out.last_error || '',
            runs: (readState(HOME).runs || 0) + 1,
            picked: out.videos_seen || 0,
            posted: out.comments_posted || 0,
          })
        } catch { /* 状态写失败不影响结果 */ }
      }
      return {
        ...out,
        triggered: Boolean(out.run_id),
        hint: out.run_id
          ? `已启动（${out.run_id}）。不用等：稍后用 bilisee_status 看进度、bilisee_summary 取结论。`
          : (out.error || '没拿到 run_id，看 bilisee_health'),
      }
    },
  })

  /* bilisee_run / bilisee_wait 已移除：跑不跑由宿主定时任务决定，
     插件不再提供"点一下就跑、跑完阻塞返回"的人工入口。进度看 bilisee_status，结论看 bilisee_summary。 */

  tool({
    name: 'bilisee_status',
    description: '看本地模型巡航的状态（不返回 digest，省上下文）。不带 run_id 就看正在跑的那轮。',
    parameters: { run_id: { type: 'string', description: '留空 = 正在跑的那轮' } },
    async execute(args) {
      const runId = String(args.run_id || '').trim()
      if (!runId) {
        const a = activeRun()
        return {
          ok: true,
          active: a,
          count: listRuns(50).filter((r) => ['starting', 'running', 'resuming'].includes(r.status)).length,
        }
      }
      return fromPy(await callAp('status', { run_id: runId }))
    },
  })

  tool({
    name: 'bilisee_summary',
    description: '视频总结：本地模型先把这一轮的记忆压成一段结论（视频数据 + BV + 评论原文 + 推荐），'
      + '再交给你读。这是**最省 token** 的汇报入口。\n'
      + '默认返回 runner 收尾时已经整合好的那份（不再跑模型）；给 refresh=true 才现场重跑一次整合。\n'
      + '返回里带 raw_chars / chars / saved_ratio，能看出比直接读原始记忆省了多少。',
    parameters: {
      run_id: { type: 'string', description: '留空 = 最近一轮' },
      refresh: { type: 'boolean', description: 'true=让本地模型重新整合一次（多花几秒本地时间）' },
    },
    async execute(args) {
      const runId = String(args.run_id || '').trim()
      const r = await callAp('summarize', {
        run_id: runId,
        refresh: Boolean(args.refresh),
      }, { timeoutMs: 180_000 })
      return fromPy(r)
    },
  })

  tool({
    name: 'bilisee_digest',
    description: '重新读某一轮的 digest（记忆 / 评论 / 思维链要点）。汇报用这个，不要读 trace。',
    parameters: {
      run_id: { type: 'string', description: 'run_id' },
      max_chars: { type: 'number', description: '最多多少字（默认 14000）' },
    },
    async execute(args) {
      const runId = String(args.run_id || '').trim()
      const maxChars = Math.max(1000, Math.min(Number(args.max_chars) || 14000, 40000))
      if (!runId) {
        const a = listRuns(1)[0]
        if (!a) return { ok: false, error: '还没有任何 run' }
        return fromPy(await callAp('digest', { run_id: a.run_id, max_chars: maxChars }))
      }
      return fromPy(await callAp('digest', { run_id: runId, max_chars: maxChars }))
    },
  })

  tool({
    name: 'bilisee_runs',
    description: '列最近的本地模型巡航记录。',
    parameters: { limit: { type: 'number', description: '几条（默认 10）' } },
    async execute(args) {
      const rows = listRuns(Number(args.limit) || 10)
      return { ok: true, count: rows.length, runs: rows }
    },
  })

  tool({
    name: 'bilisee_cancel',
    description: '取消正在跑的本地模型巡航。',
    parameters: {
      force: { type: 'boolean', description: 'true = 直接杀进程' },
    },
    async execute(args) {
      const a = activeRun()
      if (!a) return { ok: false, error: '没有正在跑的 run' }
      return fromPy(await callAp('cancel', { run_id: a.run_id, force: Boolean(args.force) }))
    },
  })

  tool({
    name: 'bilisee_health',
    description: '自检：Python / Ollama / 本地模型 / B站 登录 / 写入开关。跑不起来的时候先看它。',
    parameters: {},
    async execute() {
      refreshCfg()
      const o = await probeOllama(cfg.ollamaUrl)
      let engine = null
      try { engine = fromPy(await callAp('health', {})) } catch (e) { engine = { ok: false, error: String(e?.message || e) } }
      let cred = null
      try { cred = fromPy(await callBili('credential_check'), {}) } catch (e) { cred = { ok: false, error: String(e?.message || e) } }
      const checks = {
        python: Boolean(ensurePython()?.hasDeps),
        ollama: o.ok,
        model: Boolean(o.models?.some((m) => m.name === cfg.model)),
        bili_login: Boolean(cred?.logged_in),
        engine: Boolean(engine?.ok),
        write_env: Boolean(cfg.writeEnabled),
      }
      return {
        ok: Object.entries(checks).every(([k, v]) => (k === 'write_env' ? true : v)),
        checks,
        model: cfg.model,
        ollama_url: cfg.ollamaUrl,
        ollama_models: (o.models || []).map((m) => m.name),
        ollama_error: o.error || '',
        bili: cred,
        engine_checks: engine?.checks || null,
        engine_error: engine?.error || '',
        python: pyInfo ? { path: pyInfo.path, version: pyInfo.version } : null,
        python_error: pyError || '',
        home: HOME,
        active: activeRun(),
      }
    },
  })

  tool({
    name: 'bilisee_models',
    description: '列出本地 Ollama 里装了哪些模型（挑一个填进设置页「本地模型」）。',
    parameters: {},
    async execute() {
      refreshCfg()
      return probeOllama(cfg.ollamaUrl)
    },
  })

  /* ── ② B站 读工具（合并自 dsh-bili-plus） ── */

  tool({
    name: 'bilisee_me',
    description: '看 B站 登录的是谁（凭证是否有效、昵称、mid、今日发帖额度）。',
    parameters: {},
    async execute() {
      if (!existsSync(join(HOME, 'bili_credential.json'))) {
        // 自己没登录过也不算错：Python 侧会借读隔壁插件的凭证
        const r = fromPy(await callBili('credential_check'))
        if (!r?.logged_in) {
          return { ...r, ok: false, hint: '还没登录：去 dsh 设置页的「B站 看见」里点「扫码登录」' }
        }
        return r
      }
      return fromPy(await callBili('credential_check'))
    },
  })

  tool({
    name: 'bilisee_search',
    description: '搜 B站 视频。返回标题 / BV号 / UP主 / 播放量 / 时长 / 发布时间。\n'
      + '⚠️ 判断内容不要看标题猜 —— 拿到结果后用 `bilisee_subtitle` 读字幕原文。',
    parameters: {
      keyword: { type: 'string', description: '搜索关键词' },
      num: { type: 'number', description: '返回几条（默认 10，最大 50）' },
      order: { type: 'string', description: 'totalrank=综合（默认）/ click=播放 / pubdate=最新' },
    },
    async execute(args) {
      return fromPy(await callBili('search', {
        keyword: String(args.keyword ?? ''),
        num: Math.max(1, Math.min(Number(args.num) || 10, 50)),
        order: String(args.order ?? 'totalrank'),
      }))
    },
  })

  tool({
    name: 'bilisee_video',
    description: '取一个视频的完整信息：标题 / UP主 / 时长 / 简介 / 分P / 播放点赞投币 / 标签。',
    parameters: { bvid: { type: 'string', description: 'BV号（如 BV1xx411c7mD）' } },
    async execute(args) {
      return fromPy(await callBili('video_info', { bvid: String(args.bvid ?? '') }))
    },
  })

  tool({
    name: 'bilisee_subtitle',
    description: '**取视频字幕原文**（优先人工字幕，没有就用 AI 字幕）。判断「这视频讲了什么」的首选接口。',
    parameters: {
      bvid: { type: 'string', description: 'BV号' },
      max_chars: { type: 'number', description: '最多返回多少字（默认 8000）' },
    },
    async execute(args) {
      return fromPy(await callBili('subtitle', {
        bvid: String(args.bvid ?? ''),
        max_chars: Math.max(200, Math.min(Number(args.max_chars) || 8000, 60000)),
      }))
    },
  })

  tool({
    name: 'bilisee_comments',
    description: '取视频的热门评论（按热度）。看观众在聊什么，也是判断「值不值得回」的依据。',
    parameters: {
      bvid: { type: 'string', description: 'BV号' },
      num: { type: 'number', description: '取几条（默认 20，最大 50）' },
      with_replies: { type: 'boolean', description: '是否顺带抓楼中楼（默认 false，慢）' },
    },
    async execute(args) {
      return fromPy(await callBili('comments', {
        bvid: String(args.bvid ?? ''),
        num: Math.max(1, Math.min(Number(args.num) || 20, 50)),
        with_replies: Boolean(args.with_replies),
      }))
    },
  })

  tool({
    name: 'bilisee_popular',
    description: 'B站 综合热门榜。',
    parameters: { num: { type: 'number', description: '几条（默认 20，最大 50）' } },
    async execute(args) {
      return fromPy(await callBili('popular', { num: Math.max(1, Math.min(Number(args.num) || 20, 50)) }))
    },
  })

  tool({
    name: 'bilisee_audit',
    description: '看最近的发帖审计流水（发帖 / 删帖 / 暂停 / 风控事件）。',
    parameters: { limit: { type: 'number', description: '几条（默认 20）' } },
    async execute(args) {
      return fromPy(await callBili('audit', { limit: Math.max(1, Math.min(Number(args.limit) || 20, 100)) }))
    },
  })

  /* ─────────────────────── HTTP 路由（设置页用） ─────────────────────── */

  const sendJson = (res, code, obj) => {
    try {
      res.statusCode = code
      res.setHeader('content-type', 'application/json; charset=utf-8')
      res.end(JSON.stringify(obj))
    } catch { /* 连接已断 */ }
  }

  const readBody = async (req) => {
    const chunks = []
    for await (const c of req) chunks.push(c)
    const raw = Buffer.concat(chunks).toString('utf8')
    if (!raw) return {}
    try { return JSON.parse(raw) } catch { return {} }
  }

  const API = '/api/bili-see'

  const handle = async (req, res, url) => {
    const path = url.pathname.replace(API, '')

    /* ── 配置 ── */
    if (req.method === 'GET' && path === '/config') {
      refreshCfg()
      return sendJson(res, 200, { ok: true, config: cfg, defaults: DEFAULTS, quota: quotaState() })
    }

    if (req.method === 'POST' && path === '/config') {
      const body = await readBody(req)
      const patch = body?.patch ?? body ?? {}
      const before = { ...cfg }
      saveCfgLocal(patch)
      let hostOk = false
      try {
        const row = settingsDesc(true)
        if (row?.ns && typeof ctx?.settings?.update === 'function') {
          await ctx.settings.update(row.ns, patch, row.revision)
          hostOk = true
        }
      } catch { /* 本地那份还在，不算致命 */ }
      invalidateSettings()
      refreshCfg()

      const envChanged = ENV_KEYS.some((k) => before[k] !== cfg[k])
      let restarted = { bili: false, engine: false }
      if (envChanged) {
        if (['writeEnabled', 'maxPostsPerDay', 'minIntervalMin'].some((k) => before[k] !== cfg[k])) {
          stopBili()
          try { await ensureBili(); restarted.bili = true } catch (e) { log(`改配置后重建 B站 MCP 失败：${e?.message ?? e}`) }
        }
        if (['model', 'ollamaUrl', 'writeEnabled'].some((k) => before[k] !== cfg[k])) {
          stopAp()
          restarted.engine = true // 懒重建：下次调用时起
        }
      }
      return sendJson(res, 200, { ok: true, config: cfg, hostSynced: hostOk, restarted, quota: quotaState() })
    }

    /* ── Ollama 模型列表 ── */
    if (req.method === 'GET' && path === '/models') {
      refreshCfg()
      const url2 = url.searchParams.get('url') || cfg.ollamaUrl
      return sendJson(res, 200, await probeOllama(url2))
    }

    /* ── 状态 ── */
    if (req.method === 'GET' && path === '/status') {
      refreshCfg()
      const st = readState(HOME)
      let engine = null
      try { engine = fromPy(await callAp('health', {}), {}) } catch (e) { engine = { ok: false, error: String(e?.message || e) } }
      let bili = null
      try { bili = fromPy(await callBili('status')) } catch (e) { bili = { ok: false, error: String(e?.message || e) } }
      /* 「这一页显示的」和「引擎真正在用的」逐项对比 —— 不同步要在页面上说出来，别让用户自己猜 */
      const uiCfg = {
        daily_video_max: Number(cfg.dailyVideoMax),
        ai_tech: Number(cfg.aiTechQuota),
        ai_fun: Number(cfg.aiFunQuota),
        max_posts: Number(cfg.maxPostsPerDay),
        min_interval: Number(cfg.minIntervalMin),
        write: Boolean(cfg.writeEnabled),
        model: String(cfg.model || ''),
      }
      const engineCfg = engine?.quota
        ? {
          daily_video_max: Number(engine.quota.daily_video_max),
          ai_tech: Number(engine.quota.ai_tech),
          ai_fun: Number(engine.quota.ai_fun),
          max_posts: null,       // health 不报这两个，用不到就不比
          min_interval: null,
          write: Boolean(engine.write_allowed_by_env),
          model: String(engine.model || ''),
        }
        : null
      const diffs = []
      if (engineCfg) {
        for (const k of ['daily_video_max', 'ai_tech', 'ai_fun', 'write', 'model']) {
          if (engineCfg[k] === null || engineCfg[k] === undefined) continue
          if (String(engineCfg[k]) !== String(uiCfg[k])) {
            diffs.push({ key: k, ui: uiCfg[k], engine: engineCfg[k] })
          }
        }
      }

      return sendJson(res, 200, {
        ok: true,
        writeEnabled: cfg.writeEnabled,
        model: cfg.model,
        ollamaUrl: cfg.ollamaUrl,
        today: { runs: st.runs, picked: st.picked, posted: st.posted },
        lastRunAt: st.lastRunAt,
        lastRunOk: st.lastRunOk,
        lastRunId: st.lastRunId,
        lastError: st.lastError,
        python: pyInfo ? { path: pyInfo.path, version: pyInfo.version, hasDeps: pyInfo.hasDeps } : null,
        pythonError: pyError || '',
        home: HOME,
        engine,
        bili,
        active: activeRun(),
        // 配额（前端要显示"今天还能看几条"）
        daily: engine?.daily || null,
        quality_gate: engine?.quality_gate || { min_up_fans: cfg.minUpFans, min_play: cfg.minPlay },
        quota: {
          daily_video_max: cfg.dailyVideoMax,
          ai_tech: cfg.aiTechQuota,
          ai_fun: cfg.aiFunQuota,
          wanted: Number(cfg.aiTechQuota) + Number(cfg.aiFunQuota) + otherQuotaTotal(cfg.otherPrefs),
        },
        /* ok=false 时前端要提示"引擎在用另一套数"，并给一个「重新下发」按钮 */
        sync: { ok: diffs.length === 0, diffs, ui: uiCfg, engine: engineCfg },
        summary: lastSummary(),
      })
    }

    /* ── 手动对齐：把这一页的生效值重新下发给引擎，并重建子进程让环境变量跟上 ── */
    if (req.method === 'POST' && path === '/sync') {
      refreshCfg()
      const wrote = syncEngineConfig(true)
      let restarted = { bili: false, engine: false }
      try { stopBili(); await ensureBili(); restarted.bili = true } catch { /* 下一轮再试 */ }
      try { stopAp(); restarted.engine = true } catch { /* 懒重建 */ }
      return sendJson(res, 200, { ok: true, wrote, restarted })
    }

    /* ── 自动化：本轮巡航由宿主定时任务驱动，插件不再提供"点一下就跑"的入口 ── */
    if (req.method === 'GET' && path === '/automation') {
      refreshCfg()
      const every = 86400
      const prompt = '调用 bilisee_tick 把一轮 B 站巡航启动起来，不要等它跑完、也不要重复调用；'
        + '稍后用 bilisee_summary 取结论并向用户简短汇报。'
      return sendJson(res, 200, {
        ok: true,
        mode: 'harness-schedule',
        tool: 'schedule_create',
        every_seconds: every,
        prompt,
        sample: `帮我建一个定时任务：每 ${Math.round(every / 3600)} 小时跑一次。${prompt}`,
        active: activeRun(),
        last_run: listRuns(1)[0] || null,
        notes: [
          '插件里没有启动按钮了：什么时候跑由 harness 的定时任务决定。',
          'harness 需要加载 @deepseek-ai/dsh-schedule 才有 schedule_create（最小间隔 5 分钟）。',
          '没有定时能力时，也可以用系统计划任务执行 dsh -p "调用 bilisee_tick"。',
        ],
      })
    }

    /* ── 单轮详情（进度 + digest；只读，不能再从这条路启动） ── */
    if (req.method === 'GET' && path === '/run') {
      const id = url.searchParams.get('run_id') || activeRun()?.run_id || ''
      if (!id) return sendJson(res, 200, { ok: false, error: '没有 run_id' })
      const st = readRunState(id)
      if (!st) return sendJson(res, 200, { ok: false, error: `找不到 ${id}` })
      const mem = readJson(join(RUNS_DIR, id, 'memory.json'), null)
      const sum = mem?.summary || null
      return sendJson(res, 200, {
        ok: true,
        run_id: id,
        state: {
          status: st.status, phase: st.phase, steps: st.steps,
          continues: st.continues, auto_continues: st.auto_continues,
          videos_seen: st.videos_seen, comments_posted: st.comments_posted,
          created_at: st.created_at, finished_at: st.finished_at,
          last_error: st.last_error, model: st.model,
          current_bvid: st.current_bvid, pid_alive: st.pid_alive,
        },
        bucket_plan: mem?.bucket_plan || null,
        summary: sum?.text ? {
          text: sum.text,
          degraded: Boolean(sum.degraded),
          chars: sum.summary_chars,
          raw_chars: sum.raw_chars,
          saved_ratio: sum.saved_ratio,
          usage: sum.usage || {},
        } : null,
        digest: readRunDigest(id, 24000),
      })
    }

    if (req.method === 'GET' && path === '/runs') {
      return sendJson(res, 200, { ok: true, runs: listRuns(Number(url.searchParams.get('limit')) || 12) })
    }

    if (req.method === 'POST' && path === '/cancel') {
      const active = activeRun()
      if (!active) return sendJson(res, 200, { ok: false, error: '没有正在跑的 run' })
      try {
        return sendJson(res, 200, fromPy(await callAp('cancel', { run_id: active.run_id, force: true })))
      } catch (e) {
        return sendJson(res, 200, { ok: false, error: e?.message || String(e) })
      }
    }

    /* ── 日志 ── */
    if (req.method === 'GET' && path === '/log') {
      return sendJson(res, 200, { ok: true, lines: tailLog(HOME, Number(url.searchParams.get('lines')) || 40) })
    }

    /* ── 登录 ── */
    if (req.method === 'POST' && path === '/login/start') {
      try {
        const r = fromPy(await callBili('login_qr_start'))
        let qr = null
        try { qr = 'data:image/png;base64,' + readFileSync(r.qrcode_path).toString('base64') } catch { /* 没图就给 URL */ }
        return sendJson(res, 200, { ok: true, key: r.key, url: r.qrcode_url, qr })
      } catch (e) {
        return sendJson(res, 200, { ok: false, error: e?.message || String(e) })
      }
    }

    if (req.method === 'POST' && path === '/login/poll') {
      const body = await readBody(req)
      try {
        return sendJson(res, 200, { ok: true, ...fromPy(await callBili('login_qr_poll', { key: String(body.key || '') })) })
      } catch (e) {
        return sendJson(res, 200, { ok: false, error: e?.message || String(e) })
      }
    }

    /* ── 人设 ── */
    if (req.method === 'GET' && path === '/persona') {
      try { return sendJson(res, 200, { ok: true, ...fromPy(await callBili('persona_read')) }) }
      catch (e) { return sendJson(res, 200, { ok: false, error: e?.message || String(e) }) }
    }

    if (req.method === 'POST' && path === '/persona') {
      const body = await readBody(req)
      const text = String(body.text ?? '')
      if (!text.trim()) return sendJson(res, 400, { ok: false, error: '人设不能为空' })
      try { return sendJson(res, 200, fromPy(await callBili('persona_save', { text, backup: true }))) }
      catch (e) { return sendJson(res, 200, { ok: false, error: e?.message || String(e) }) }
    }

    return sendJson(res, 404, { ok: false, error: `unknown ${req.method} ${path}` })
  }

  try {
    ctx.effect(() => ctx.webServer.register({
      kind: 'prefix',
      path: API,
      handler: async (req, res) => {
        try {
          await handle(req, res, new URL(req.url, 'http://127.0.0.1'))
        } catch (e) {
          sendJson(res, 500, { ok: false, error: e?.message ?? String(e) })
        }
      },
    }), 'bili-see: /api/bili-see 路由')
  } catch (e) {
    log(`HTTP 路由没挂上（设置页会降级）：${e?.message ?? e}`)
  }

  /* ── 收摊 ── */
  try {
    ctx.effect(() => () => { stopAp(); stopBili() }, 'bili-see: 子进程清理')
  } catch { /* 宿主不支持 effect 就算了 */ }

  log(`插件已加载：home=${HOME} model=${cfg.model} write=${cfg.writeEnabled ? '开' : '关'}`)
}
