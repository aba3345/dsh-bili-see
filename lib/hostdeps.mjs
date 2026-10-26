/**
 * lib/hostdeps.mjs —— 从宿主那棵树里取 `@deepseek-ai/*`
 * ============================================================================
 * 🔴 为什么必须有这个文件（这不是防御性编程，是**已经被踩过的坑**）：
 *
 *   `@deepseek-ai/schemastery` 和 `@deepseek-ai/dsh-tools` 在插件 package.json 里
 *   是 **optional peer**。桌面版 profile 用 `autoInstallPeers: false` ⇒ **永远不装**。
 *   于是 `import z from '@deepseek-ai/schemastery'` 直接抛
 *   `ERR_MODULE_NOT_FOUND` ⇒ **整条插件不激活**，DSH 只留一句
 *   `warning: 1 entry did not activate … failed to import`。
 *
 *   ⚠️ 本机实测：`~/.dsh/profiles/desktop/node_modules/@deepseek-ai`
 *      **整个目录不存在**。所以「裸 import 试试看」这条路是死的。
 *
 *   ⚠️ `dsh-tools` 更麻烦：它自己还有 7 个顶层 import、其中 4 个是 peer，
 *      光把它装上救不回来。
 *
 * ⭐ 策略（三条一起走，缺一条就会翻车）：
 *   ① **优先从宿主那棵树里解析** —— 跟宿主用同一份，行为最一致；
 *   ② 解析不到就 **bare import**（靠我们自己 dependencies 装一份保底）；
 *   ③ 彻底拿不到 ⇒ `z = null`、`defineTool = 本地等价实现`，
 *      **插件照常加载**（只是设置页降级）——
 *      🔴 绝不因为一个 import 就让整条插件不激活。
 * ============================================================================
 */
import { existsSync, readFileSync, readdirSync } from 'node:fs'
import { join, dirname } from 'node:path'
import { homedir } from 'node:os'
import { fileURLToPath, pathToFileURL } from 'node:url'

/** 插件自己那棵树（dependencies 装出来的那份）—— 用模块位置算，别信 cwd */
const SELF_MODULES = join(dirname(fileURLToPath(import.meta.url)), '..', 'node_modules', '@deepseek-ai')

/** 可能藏着宿主 `@deepseek-ai` 包的地方（不写死某一台机器，按可能性排序） */
export function candidateRoots() {
  const out = []
  const push = (p) => { if (p && !out.includes(p)) out.push(p) }
  push(process.env.DSH_HOST_MODULES)

  // ① 桌面版：宿主本体在 resources/runtime 或 app.asar 附近
  const local = process.env.LOCALAPPDATA
  if (local) {
    push(join(local, 'Programs', 'dsh', 'resources', 'runtime', 'node_modules', '@deepseek-ai'))
  }
  // ② npm 全局装的
  const appdata = process.env.APPDATA
  if (appdata) {
    const npm = join(appdata, 'npm', 'node_modules')
    // ⭐ **本机实测走通的就这条**（2026-10 自检日志里看到 defineTool 从这里加载）：
    //    C:\Users\…\AppData\Roaming\npm\node_modules\@deepseek-ai\dsh\node_modules\@deepseek-ai\dsh-tools
    push(join(npm, '@deepseek-ai', 'dsh', 'node_modules', '@deepseek-ai'))
    push(join(npm, '@deepseek-ai'))
    // 桌面版可能叫别的包名（dsh-desktop / dsh-next…）⇒ 逐个看它们的 node_modules
    try {
      for (const n of readdirSync(join(npm, '@deepseek-ai'))) {
        push(join(npm, '@deepseek-ai', n, 'node_modules', '@deepseek-ai'))
      }
    } catch { /* 没有就算了 */ }
  }
  push('/usr/local/lib/node_modules/@deepseek-ai/dsh/node_modules/@deepseek-ai')
  push('/usr/lib/node_modules/@deepseek-ai/dsh/node_modules/@deepseek-ai')
  push('/usr/local/lib/node_modules/@deepseek-ai')
  push('/usr/lib/node_modules/@deepseek-ai')

  // ③ profile 树：有些机器上这些包被别的插件 hoist 到 profile 根上了
  try {
    const dshHome = process.env.DSH_HOME || join(homedir(), '.dsh')
    for (const p of readdirSync(join(dshHome, 'profiles'))) {
      push(join(dshHome, 'profiles', p, 'node_modules', '@deepseek-ai'))
    }
  } catch { /* 没有就算了 */ }

  // ④ **本机实测有效的一条**：DSH 桌面版把依赖平铺在
  //    `D:\deepseekharness\resources\app.asar\dsh\node_modules\@deepseek-ai`
  //    这里用 DSH_IMPL_ROOT 环境变量兜底，或从 dshHome 往上找 resources/
  const impl = process.env.DSH_IMPL_ROOT
  if (impl) push(join(impl, 'node_modules', '@deepseek-ai'))
  for (const base of [process.env.DSH_HOME, join(homedir(), '.dsh')]) {
    if (!base) continue
    // 从 ~/.dsh 往上两级找 resources/runtime
    for (const rel of ['../resources/runtime/node_modules/@deepseek-ai',
      '../resources/app.asar/dsh/node_modules/@deepseek-ai']) {
      try { push(join(base, rel)) } catch { /* 忽略 */ }
    }
  }

  // ⑤ 插件自己那份（dependencies 装出来的）—— 放最后，前面优先"跟宿主同一份"
  push(SELF_MODULES)
  return out
}

/** 按包的 exports['.'] 找入口文件，返回 file:// URL */
function entryUrlFor(root, name) {
  try {
    const dir = join(root, name)
    const pkgFile = join(dir, 'package.json')
    if (!existsSync(pkgFile)) return null
    const pkg = JSON.parse(readFileSync(pkgFile, 'utf8'))
    const dot = pkg.exports?.['.']
    const rel = typeof dot === 'string'
      ? dot
      : (dot?.import || dot?.default || pkg.module || pkg.main)
    if (!rel) return null
    const file = join(dir, String(rel).replace(/^\.\//, ''))
    return existsSync(file) ? pathToFileURL(file).href : null
  } catch { return null }
}

/** 试从宿主树里 import 一个包；拿不到返回 null（失败原因写进 notes，方便远程排查） */
async function fromHostTree(name, log) {
  const fails = []
  for (const root of candidateRoots()) {
    if (root === SELF_MODULES) continue      // 自己那份交给下面的 bare import 处理
    const url = entryUrlFor(root, name)
    if (!url) continue
    try {
      const mod = await import(url)
      log(`${name}：从宿主树取到（${url.replace(/^file:\/\/\//, '').replace(/^file:\/\//, '')}）`)
      return mod
    } catch (e) { fails.push(`${e?.code || 'ERR'}:${root}`) }
  }
  if (fails.length) log(`${name}：宿主树里都不行（${fails.slice(0, 3).join(' ')}）`)
  return null
}

/**
 * 兜底：**我们自己的等价实现**，只在拿不到宿主的 dsh-tools 时用。
 *
 * 🔴 这里的语义必须跟宿主**完全一致**，否则「本地自检通过、装上就炸」：
 *
 *   宿主的 `defineTool(options)` 里做的是（app.asar 里 dsh-tools/lib/index.js:838）：
 *     `const parameters = parameterSchemaSpecToJsonSchema(options.parameters)`
 *     `const outputSchema = valueSchemaSpecToJsonSchema(options.output.schema)`
 *
 *   ⇒ **`options.parameters` 是「属性表」，不是 JSON Schema**：
 *       `{ keyword: { type: 'string', description: '...' }, num: { type: 'number', ... } }`
 *       宿主会把它编译成
 *       `{ type: 'object', properties: {...}, required?: [...] }`
 *
 *   ⚠️ 所以不能拿 `{type:'object', properties:...}` 当 parameters 传 ——
 *      那样 `type` 会被当成一个**属性名**，它的值 `'object'` 是个字符串不是对象，
 *      宿主直接抛 `parameters.type must be a value schema object`。
 *      （这个坑 2026-10 在这台机器上真撞过，报错原文就在上面。）
 *
 *   `output.schema` 才是**真正的 JSON Schema**（value schema），
 *   形如 `{ type:'object', properties:{}, additionalProperties:true }`。
 */
function localDefineTool(options, log) {
  // ① parameters：属性表 → 编译成 object schema
  const spec = options.parameters
  if (!spec || typeof spec !== 'object' || Array.isArray(spec)) {
    throw new Error(`defineTool(${options.name}): parameters 必须是对象（属性表）`)
  }
  const properties = {}
  const required = []
  for (const [key, val] of Object.entries(spec)) {
    // 每个属性值必须是**对象**（value schema），不能是字符串/数组
    if (!val || typeof val !== 'object' || Array.isArray(val)) {
      throw new Error(
        `defineTool(${options.name}): parameters.${key} must be a value schema object`
        + `（要写 {type:'string',description:'…'}，不能写 'string'）`)
    }
    if (val.type === undefined) {
      throw new Error(`defineTool(${options.name}): parameters.${key} 缺 type`)
    }
    properties[key] = validateValueSchema(val, `parameters.${key}`, options.name)
    if (val.required === true) required.push(key)
  }
  const parameters = { type: 'object', properties }
  if (required.length) parameters.required = required

  // ② output.schema：真 JSON Schema
  const outputSchema = options.output?.schema
  if (!outputSchema || typeof outputSchema !== 'object') {
    throw new Error(`defineTool(${options.name}): 缺 output.schema`)
  }
  const compiledOutput = validateValueSchema(outputSchema, 'output.schema', options.name)

  log?.(`dsh-tools 拿不到 —— 用本地等价实现顶上（工具 ${options.name}）`)
  return {
    ...options,
    parameters,
    output: { ...options.output, schema: compiledOutput },
  }
}

/**
 * 校验一个 value schema（递归）。
 *
 * 规矩来自宿主 `assertSupportedJsonSchema`：**object 必须写 additionalProperties**
 * —— 这是 2026-10-14 那次 `JsonSchemaError` 换来的。根节点 parameters 由我们来补，
 * 但用户手写的嵌套 object 必须自己写清楚是封闭还是开放。
 */
function validateValueSchema(schema, where, toolName) {
  if (!schema || typeof schema !== 'object' || Array.isArray(schema)) {
    throw new Error(`defineTool(${toolName}): ${where} must be a value schema object`)
  }
  const out = { ...schema }
  if (out.type === 'object') {
    if (out.additionalProperties === undefined) {
      throw new Error(
        `defineTool(${toolName}): ${where} 是 object 却没写 additionalProperties`
        + '（封闭写 false，开放写 true）')
    }
    if (out.properties) {
      const props = {}
      for (const [k, v] of Object.entries(out.properties)) {
        props[k] = validateValueSchema(v, `${where}.properties.${k}`, toolName)
      }
      out.properties = props
    }
  }
  if (out.items) {
    out.items = Array.isArray(out.items)
      ? out.items.map((v, i) => validateValueSchema(v, `${where}.items[${i}]`, toolName))
      : validateValueSchema(out.items, `${where}.items`, toolName)
  }
  return out
}

/**
 * 取回宿主半端要用的一点点东西
 * @returns {Promise<{z:any, defineTool:Function, notes:string[]}>}
 */
export async function loadHostDeps() {
  const notes = []
  const log = (m) => notes.push(m)

  // ① schemastery（Config 要用；宿主设置层要用**它自己那份**读 volatile 字段）
  let zMod = await fromHostTree('schemastery', log)
  if (!zMod) {
    try {
      zMod = await import('@deepseek-ai/schemastery')
      log('schemastery：宿主树里没有，用自己依赖里那份')
    } catch (e) {
      log(`⚠️ schemastery 彻底拿不到（${e?.code || ''} ${String(e?.message || '').slice(0, 120)}）`
        + ' —— 这份插件没有 Config，设置页会降级（**插件本体照常跑**）')
    }
  }
  const z = zMod?.default ?? zMod ?? null

  // ② dsh-tools 的 defineTool（拿不到就用本地等价实现 —— 这是桌面版修复的关键一步）
  let dtMod = await fromHostTree('dsh-tools', log)
  if (!dtMod) {
    try {
      dtMod = await import('@deepseek-ai/dsh-tools')
      log('dsh-tools：宿主树里没有，用自己依赖里那份')
    } catch (e) {
      log(`dsh-tools 拿不到（${e?.code || ''}）—— 用本地等价实现顶上`)
      dtMod = null
    }
  }
  const defineTool = dtMod?.defineTool || ((options) => localDefineTool(options, log))

  return { z, defineTool, notes }
}
