/**
 * lib/python-env.mjs —— 给插件找一位能跑 Python 的先生
 * ============================================================================
 * 现实约束（本机实测）：
 *   · 系统里没有全局 python（`python` / `python3` / `py` 全都不一定在 PATH）；
 *   · 已有的 `dsh-bili-scroll` 在 `$DSH_HOME/bili-duty/.venv` 里装了 venv，
 *     里面有 **mcp 1.30 + Pillow 13 + qrcode** —— 正好是我们要的全部依赖；
 *   · 但两个插件**不能共用同一个 venv 目录**做不同的事，所以本插件：
 *       ① 优先找自己家目录的 venv（$DSH_HOME/bili-plus/.venv）；
 *       ② 没有就找一个**能 import mcp** 的 Python（候选清单见下）；
 *       ③ 都没有就报一个**能照着做的**错（含 pip 命令），不静默失败。
 *
 * 🔴 关键：**不要假设 `python` 在 PATH**。这一条让原插件在无数机器上白屏过。
 * ============================================================================
 */
import { existsSync, readdirSync, readFileSync, rmSync } from 'node:fs'
import { join, dirname } from 'node:path'
import { spawnSync } from 'node:child_process'
import { homedir } from 'node:os'
import { fileURLToPath } from 'node:url'

const IS_WIN = process.platform === 'win32'

/** venv 里 python 可执行文件的位置（Windows / POSIX 不同） */
export function venvPython(venvDir) {
  return IS_WIN
    ? join(venvDir, 'Scripts', 'python.exe')
    : join(venvDir, 'bin', 'python')
}

/**
 * 跑一句 python 判断它是不是我们要的那位。
 *
 * 🔴 **三个坑都踩过，别改回去**：
 *   ① 别用 `stdio:'pipe'` 读结果 —— 受限环境（Windows 沙箱 / 某些 EDR）下
 *      `spawnSync(..., {stdio:'pipe'})` 直接 `EPERM`，`status === null`，
 *      看起来像"Python 装坏了"，其实进程根本没起来。
 *   ② 别把临时文件放 `%TEMP%` —— 它可能指向**宿主沙箱的私有目录**
 *      （本机实测 `%TEMP%` = `...\Temp\dsh-xxxx`），Python 子进程写不进去，
 *      表现为 `exit 0` 但文件不存在。
 *   ③ 也别放 `process.cwd()` —— 插件被加载时 cwd 是宿主的，完全不可控。
 *
 * ⇒ 落点用**本模块自己的位置**（插件包内，一定可写），跑完立刻删。
 */
const PROBE_DIR = dirname(fileURLToPath(import.meta.url))

function probe(pyPath, log) {
  if (!pyPath) return null
  const isBare = !pyPath.includes('/') && !pyPath.includes('\\')
  if (!isBare && !existsSync(pyPath)) return null

  const outFile = join(
    PROBE_DIR,
    `.probe-${process.pid}-${Math.random().toString(36).slice(2, 8)}.json`,
  )
  const script = [
    'import sys, json',
    'mods = {}',
    'for name in ("mcp", "httpx", "PIL", "qrcode"):',
    '    try:',
    '        __import__(name); mods[name] = True',
    '    except Exception:',
    '        mods[name] = False',
  ].join('\n')

  // -c 脚本里把结果写到 outFile（用 argv 传路径，避免拼字符串转义地狱）
  const code = `${script}\nimport io\nwith io.open(sys.argv[1], "w", encoding="utf-8") as f:\n    f.write(json.dumps({"v": list(sys.version_info[:3]), "exe": sys.executable, "mods": mods}))`

  let status
  try {
    const r = spawnSync(pyPath, ['-c', code, outFile], {
      stdio: 'ignore',
      timeout: 30_000,
      windowsHide: true,
    })
    status = r.status
    if (r.error?.code === 'EPERM' || r.error?.code === 'ENOENT') {
      log?.(`${pyPath} 起不来（${r.error.code}）`)
      return null
    }
  } catch (e) {
    log?.(`${pyPath} 探测异常：${e?.message || e}`)
    return null
  }

  let info = null
  try {
    if (existsSync(outFile)) info = JSON.parse(readFileSync(outFile, 'utf8'))
  } catch { /* 解析不了就当没跑通 */ }
  try { if (existsSync(outFile)) rmSync(outFile) } catch { /* 删不掉就算了 */ }

  if (!info) {
    log?.(`${pyPath} 没写出结果（exit ${status}）`)
    return null
  }
  const mods = info.mods || {}
  const missing = Object.keys(mods).filter((k) => !mods[k])
  return {
    path: info.exe || pyPath,
    version: (info.v || []).join('.'),
    hasDeps: missing.length === 0,
    missingDeps: missing.join(', '),
  }
}

/** 按可能性排序的候选 Python 路径 */
export function candidatePythons({ dshHome, log } = {}) {
  const home = dshHome || process.env.DSH_HOME || join(homedir(), '.dsh')
  const out = []

  // ① 本插件自己的 venv
  out.push(venvPython(join(home, 'bili-see', '.venv')))

  // ② 用户显式指定（设置页「Python 路径」或环境变量）
  if (process.env.BILI_SEE_PYTHON) out.push(process.env.BILI_SEE_PYTHON)

  // ③ 同机已装好的 bili-plus / bili-duty / bili-agent 的 venv
  //    依赖完全对得上（mcp 1.x + Pillow + qrcode），开箱即用
  out.push(venvPython(join(home, 'bili-plus', '.venv')))
  out.push(venvPython(join(home, 'bili-duty', '.venv')))

  // ④ 本机其它已知 venv（迁移过来的 bili-autopilot 独立项目）
  out.push(venvPython(join(homedir(), 'Documents', '默认项目', 'bili-autopilot', '.venv')))
  out.push(venvPython(join(homedir(), 'Documents', '默认项目', 'bili-agent', '.venv')))

  // ⑤ profiles 里其它插件带的 venv（不知道谁有，扫一遍）
  try {
    for (const prof of readdirSync(join(home, 'profiles'))) {
      const base = join(home, 'profiles', prof, 'node_modules')
      try {
        for (const pkg of readdirSync(base)) {
          if (!pkg.startsWith('dsh-')) continue
          out.push(venvPython(join(base, pkg, '.venv')))
          out.push(venvPython(join(base, pkg, 'vendor', '.venv')))
        }
      } catch { /* 没权限就算了 */ }
    }
  } catch { /* 没有 profiles 就算了 */ }

  // ⑥ 常见系统位置
  if (IS_WIN) {
    const local = process.env.LOCALAPPDATA
    if (local) {
      out.push(join(local, 'Programs', 'Python', 'Python313', 'python.exe'))
      out.push(join(local, 'Programs', 'Python', 'Python312', 'python.exe'))
      out.push(join(local, 'Programs', 'Python', 'Python311', 'python.exe'))
      out.push(join(local, 'uv', 'python', 'cpython-3.12-windows-x86_64-none', 'python.exe'))
    }
    out.push('C:\\Python313\\python.exe', 'C:\\Python312\\python.exe', 'C:\\Python311\\python.exe')
  } else {
    out.push('/usr/bin/python3', '/usr/local/bin/python3')
  }

  // ⑦ 最后才信 PATH（`python` 在很多 Windows 上是微软商店的空壳，会弹窗）
  out.push(IS_WIN ? 'python' : 'python3')

  const seen = new Set()
  return out.filter((p) => {
    if (!p || seen.has(p)) return false
    seen.add(p)
    return true
  })
}

/**
 * 找到一位能用的 Python。
 * @returns {{path:string, version:string, hasDeps:boolean, missingDeps:string} | null}
 */
export function findPython({ dshHome, log } = {}) {
  for (const cand of candidatePythons({ dshHome, log })) {
    // PATH 上的名字没法 existsSync，直接让 probe 试
    const isBare = !cand.includes('/') && !cand.includes('\\')
    if (!isBare && !existsSync(cand)) continue
    const info = probe(cand, log)
    if (!info) continue
    if (info.hasDeps) {
      log?.(`用 ${info.path}（Python ${info.version}，依赖齐全）`)
      return info
    }
    log?.(`${info.path} 能用但缺依赖（${info.missingDeps}）`)
    // 记下来当兜底：至少知道有个能跑的 python，可以拿它建 venv
    if (!findPython._fallback) findPython._fallback = info
  }
  return findPython._fallback || null
}

/** 给人看的修复指令 */
export function installHint(pythonPath) {
  const py = pythonPath || (IS_WIN ? 'python' : 'python3')
  return [
    '缺 Python 依赖，跑这两条：',
    `  "${py}" -m pip install --upgrade pip`,
    `  "${py}" -m pip install "mcp>=1.30,<2" httpx pillow qrcode`,
  ].join('\n')
}
