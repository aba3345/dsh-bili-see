/**
 * lib/mcp-client.mjs —— 极简 MCP stdio 客户端（一行一个 JSON-RPC，按 \n 切分）
 * ============================================================================
 * 为什么不用官方 @modelcontextprotocol/sdk：
 *   它是 optional peer，桌面版 profile 的 `autoInstallPeers: false` 装不上，
 *   为了一个客户端拖进来一整棵树不值得。协议本身很简单：
 *
 *     → initialize / notifications/initialized
 *     → tools/list | tools/call
 *     ← { jsonrpc:"2.0", id, result | error }
 *
 * 关键点（都是原插件踩出来的）：
 *   ① 子进程必须用 **UTF-8** 管道，Windows 默认 GBK 会把中文标题变乱码；
 *   ② `stderr` 一定要接住 —— MCP 服务端往 stderr 打日志，
 *      不读会**把管道塞满然后整个卡死**（很隐蔽，表现为"工具永远不返回"）；
 *   ③ 超时要能真正杀掉进程，否则下一次调用会接到上一次的迟到响应；
 *   ④ 响应里 `result.content[0].text` 往往是一段 JSON 字符串，能解析就解析。
 * ============================================================================
 */
import { spawn } from 'node:child_process'

const DEFAULT_TIMEOUT = 120_000

export class McpClient {
  /**
   * @param {object} opts
   * @param {string} opts.command   可执行文件（python.exe）
   * @param {string[]} opts.args
   * @param {Record<string,string>} opts.env
   * @param {string} opts.cwd
   * @param {(msg:string)=>void} opts.log
   */
  constructor(opts) {
    this.opts = opts
    this.log = opts.log || (() => {})
    this.proc = null
    this.buf = ''
    this.nextId = 1
    this.pending = new Map()
    this.stderrTail = []
    this.ready = null
  }

  _env() {
    const clean = { ...process.env, ...(this.opts.env || {}) }
    // 别让子进程继承宿主的编码设置
    clean.PYTHONIOENCODING = 'utf-8'
    clean.PYTHONUTF8 = '1'
    clean.PYTHONUNBUFFERED = '1'
    return clean
  }

  async start() {
    if (this.proc) return this

    const proc = spawn(this.opts.command, this.opts.args || [], {
      cwd: this.opts.cwd,
      env: this._env(),
      stdio: ['pipe', 'pipe', 'pipe'],
      windowsHide: true,
    })
    this.proc = proc

    proc.stdout.setEncoding('utf8')
    proc.stdout.on('data', (chunk) => this._onData(chunk))

    proc.stderr.setEncoding('utf8')
    proc.stderr.on('data', (chunk) => {
      // 只留最后几行，够排查就行；不读会塞满管道
      const text = String(chunk)
      this.stderrTail.push(text)
      if (this.stderrTail.length > 40) this.stderrTail.shift()
      for (const line of text.split('\n')) {
        if (line.trim()) this.log(`[mcp] ${line.trim()}`)
      }
    })

    proc.on('exit', (code, signal) => {
      const err = new Error(`MCP 进程退出（code=${code} signal=${signal}）\n${this.stderrTail.join('').slice(-1200)}`)
      for (const { reject, timer } of this.pending.values()) {
        clearTimeout(timer)
        reject(err)
      }
      this.pending.clear()
      this.proc = null
      this.ready = null
    })

    proc.on('error', (e) => {
      const err = new Error(`MCP 进程起不来：${e?.message || e}`)
      for (const { reject, timer } of this.pending.values()) {
        clearTimeout(timer)
        reject(err)
      }
      this.pending.clear()
    })

    this.ready = await this._handshake()
    return this
  }

  async _handshake() {
    const init = await this.request('initialize', {
      protocolVersion: '2024-11-05',
      capabilities: {},
      clientInfo: { name: 'dsh-bili-plus', version: '1.0.0' },
    }, 60_000)
    this.notify('notifications/initialized')
    return init
  }

  _onData(chunk) {
    this.buf += chunk
    let nl
    while ((nl = this.buf.indexOf('\n')) >= 0) {
      const line = this.buf.slice(0, nl).trim()
      this.buf = this.buf.slice(nl + 1)
      if (!line) continue
      let msg
      try {
        msg = JSON.parse(line)
      } catch {
        // 不是 JSON 就当作日志（有些服务端会往 stdout 打东西）
        this.log(`[mcp:raw] ${line.slice(0, 200)}`)
        continue
      }
      if (msg.id === undefined || msg.id === null) continue // 通知，忽略
      const slot = this.pending.get(msg.id)
      if (!slot) continue
      this.pending.delete(msg.id)
      clearTimeout(slot.timer)
      if (msg.error) slot.reject(new Error(`MCP 错误 ${msg.error.code}: ${msg.error.message}`))
      else slot.resolve(msg.result)
    }
  }

  notify(method, params) {
    if (!this.proc) return
    this.proc.stdin.write(JSON.stringify({ jsonrpc: '2.0', method, params }) + '\n')
  }

  request(method, params, timeoutMs = DEFAULT_TIMEOUT) {
    if (!this.proc) return Promise.reject(new Error('MCP 未启动'))
    const id = this.nextId++
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pending.delete(id)
        reject(new Error(`MCP 超时 ${timeoutMs}ms: ${method}`))
        // 超时说明服务端卡住了，杀掉让下次重来
        this.kill()
      }, timeoutMs)
      this.pending.set(id, { resolve, reject, timer })
      this.proc.stdin.write(JSON.stringify({ jsonrpc: '2.0', id, method, params }) + '\n')
    })
  }

  async listTools(timeoutMs = 60_000) {
    const r = await this.request('tools/list', {}, timeoutMs)
    return r?.tools || []
  }

  /**
   * 调一个工具。返回 `{ ok, text, json }`：
   *   · json  —— 文本能解析成 JSON 就给对象（我们的服务端都是 JSON）
   *   · text  —— 原始文本
   * MCP 层面的 `isError` 会翻成 ok:false，但业务错误的 ok:false 由调用方自己看 json.ok
   */
  async call(name, args = {}, timeoutMs = DEFAULT_TIMEOUT) {
    const r = await this.request('tools/call', { name, arguments: args }, timeoutMs)
    const parts = r?.content || []
    const text = parts
      .filter((p) => p && p.type === 'text')
      .map((p) => p.text)
      .join('\n')
    let json = null
    try {
      json = JSON.parse(text)
    } catch { /* 不是 JSON 就只给 text */ }
    const isError = Boolean(r?.isError)
    return {
      ok: !isError && (json ? json.ok !== false : true),
      isError,
      text,
      json,
    }
  }

  kill() {
    if (!this.proc) return
    try { this.proc.kill() } catch { /* 已经死了 */ }
    this.proc = null
    this.ready = null
  }
}
