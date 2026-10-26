/**
 * dsh-bili-see —— 浏览器半端（设置里的一页「B站视频评论设置」）
 * ============================================================================
 * client bundle **不是 ESM**，是自注册脚本：
 *
 *     window.__ModuleLoader__.load({
 *       id: '<包名>',                    // 必须等于 package.json 的 name
 *       factory: (require) => ({ inject, apply }),
 *     })
 *
 * 页面按「实际会动的顺序」排：
 *   ① 本地模型  ② 自动化  ③ 这一轮  ④ 账号  ⑤ 看什么  ⑥ 发评论  ⑦ 人设  ⑧ 历史  ⑨ 状态
 *
 * ⚠️ 组件一律放在模块级。放进 Panel 里，每次渲染都会生成新的组件类型，
 *    React 直接卸载重建，useState 里的草稿值被清空 ⇒ 输入框「打字被吃掉」。
 * ============================================================================
 */
window.__ModuleLoader__.load({
  id: 'dsh-bili-see',
  factory: (require) => {
    const React = require('react')
    const h = React.createElement

    const API = '/api/bili-see'

    const asJson = async (res) => {
      if (res.ok) return res.json()
      const why = res.status === 401 ? '（没权限）' : res.status === 403 ? '（被宿主拦了）' : ''
      throw new Error(`HTTP ${res.status}${why}`)
    }
    const jget = async (p) => asJson(await fetch(API + p, { credentials: 'same-origin' }))
    const jpost = async (p, body) => asJson(await fetch(API + p, {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify(body || {}),
    }))

    /* ── 通用控件 ── */

    const Row = ({ label, hint, children }) => h('div', { className: 'bs-row' },
      h('div', { className: 'bs-row-main' },
        h('div', { className: 'bs-label' }, label),
        hint ? h('div', { className: 'bs-hint' }, hint) : null,
      ),
      h('div', { className: 'bs-row-ctl' }, children),
    )

    const Toggle = ({ value, onChange, disabled }) => h('button', {
      className: `bs-toggle${value ? ' on' : ''}${disabled ? ' disabled' : ''}`,
      disabled: Boolean(disabled),
      onClick: () => { if (!disabled) onChange(!value) },
      type: 'button',
    }, h('span', { className: 'bs-knob' }))

    /**
     * 数字输入。内部存草稿、失焦才提交 ——
     * 纯受控的话每敲一个字符都会 patch→回灌，删空重打会被弹回旧值。
     */
    const Num = ({ value, onChange, min = 1, max = 100000, step, suffix, width }) => {
      const [draft, setDraft] = React.useState(String(value ?? 0))
      const [focus, setFocus] = React.useState(false)
      React.useEffect(() => { if (!focus) setDraft(String(value ?? 0)) }, [value, focus])
      const commit = () => {
        const n = Number(draft)
        if (!Number.isFinite(n) || draft === '') { setDraft(String(value ?? 0)); return }
        const c = Math.max(min, Math.min(n, max))
        setDraft(String(c))
        if (c !== value) onChange(c)
      }
      return h('div', { className: 'bs-num' },
        h('input', {
          type: 'number', value: draft, min, max, step,
          style: width ? { width } : undefined,
          onChange: (e) => setDraft(e.target.value),
          onFocus: () => setFocus(true),
          onBlur: () => { setFocus(false); commit() },
          onKeyDown: (e) => { if (e.key === 'Enter') e.target.blur() },
        }),
        suffix ? h('span', { className: 'bs-suffix' }, suffix) : null,
      )
    }

    const Float = ({ value, onChange, min = 0, max = 2, step = 0.05 }) => {
      const v = Number(value ?? 0)
      return h('div', { className: 'bs-float' },
        h('input', {
          type: 'range', min, max, step, value: v,
          onChange: (e) => onChange(Number(e.target.value)),
        }),
        h('span', { className: 'bs-suffix' }, v.toFixed(2)),
      )
    }

    const Btn = ({ children, onClick, kind = '', disabled, title }) => h('button', {
      className: `bs-btn ${kind}`.trim(),
      onClick, disabled, title, type: 'button',
    }, children)

    const Tag = ({ children, kind }) => h('span', { className: `bs-tag ${kind || ''}`.trim() }, children)

    /** 文本输入。同样内部存草稿，失焦/Enter 才提交。 */
    const Txt = ({ value, placeholder, width, onCommit, multiline, rows, mono }) => {
      const [draft, setDraft] = React.useState(String(value ?? ''))
      const [focus, setFocus] = React.useState(false)
      React.useEffect(() => { if (!focus) setDraft(String(value ?? '')) }, [value, focus])
      const commit = () => { if (draft !== String(value ?? '')) onCommit(draft) }
      const props = {
        className: `bs-text${mono ? ' bs-mono' : ''}`,
        value: draft,
        placeholder: placeholder || '',
        style: width ? { width } : undefined,
        onChange: (e) => setDraft(e.target.value),
        onFocus: () => setFocus(true),
        onBlur: () => { setFocus(false); commit() },
        onKeyDown: (e) => {
          if (e.key === 'Enter' && !multiline) e.target.blur()
          if (e.key === 'Escape') { setDraft(String(value ?? '')); e.target.blur() }
        },
      }
      if (multiline) return h('textarea', { ...props, rows: rows || 4, type: undefined })
      return h('input', { ...props, type: 'text' })
    }

    const Dot = ({ ok, text, title }) => h('span', { className: 'bs-dot', title },
      h('i', { className: ok === true ? 'ok' : ok === false ? 'bad' : 'idle' }),
      text ? h('span', null, text) : null,
    )

    /** 「其他视频」每行一个配额，加起来要跟总上限比 */
    const otherQuotaTotal = (text) => String(text || '')
      .split(/\r?\n/)
      .map((line) => line.trim())
      .filter((line) => line && !line.startsWith('#'))
      .reduce((sum, line) => {
        const parts = line.split('|').map((x) => x.trim())
        const quota = Number(parts[2])
        return sum + (Number.isFinite(quota) && quota > 0 ? quota : 1)
      }, 0)

    const statusKind = (s) => ({
      completed: 'ok', running: 'warn', resuming: 'warn', starting: 'warn',
      needs_continue: 'warn', failed: 'bad', cancelled: '',
    }[s] || '')

    /** 状态一律说中文，别让页面上冒出裸的英文枚举值 */
    const statusLabel = (s) => ({
      completed: '已完成', running: '进行中', resuming: '续跑中', starting: '启动中',
      needs_continue: '需续跑', partial: '部分完成', failed: '失败', cancelled: '已取消',
    }[s] || (s ? String(s) : '未知'))

    /** 引擎自检项的中文名 */
    const CHECK_LABEL = {
      python: 'Python', ollama: 'Ollama', model: '模型',
      bili_plus_server: 'B站服务端', bili_plus_db: '数据库', credential: '登录态',
    }

    /* ═══════════════════════════ 主面板 ═══════════════════════════ */

    function Panel() {
      const [cfg, setCfg] = React.useState(null)
      const [status, setStatus] = React.useState(null)
      const [models, setModels] = React.useState(null)
      const [runs, setRuns] = React.useState([])
      const [cur, setCur] = React.useState(null)
      const [busy, setBusy] = React.useState('')
      const [msg, setMsg] = React.useState(null)
      const [qr, setQr] = React.useState(null)
      const [loginKey, setLoginKey] = React.useState('')
      const [loaded, setLoaded] = React.useState(false)
      const [manualModel, setManualModel] = React.useState(false)
      const [persona, setPersona] = React.useState(null)
      const [pDraft, setPDraft] = React.useState('')
      const [pOpen, setPOpen] = React.useState(false)
      const [pDirty, setPDirty] = React.useState(false)
      const [showDigest, setShowDigest] = React.useState(false)
      const [auto, setAuto] = React.useState(null)

      const say = (kind, text) => setMsg({ kind, text })

      const refreshCfg = async () => {
        try { setCfg((await jget('/config')).config) }
        catch (e) { say('bad', `读设置失败：${e.message}`) }
      }
      const refreshStatus = async () => {
        try { setStatus(await jget('/status')) }
        catch (e) { say('bad', `读状态失败：${e.message}`) }
      }
      const refreshModels = async (url) => {
        setBusy('models')
        try { setModels(await jget(`/models${url ? `?url=${encodeURIComponent(url)}` : ''}`)) }
        catch (e) { setModels({ ok: false, models: [], error: e.message }) }
        finally { setBusy('') }
      }
      const refreshRuns = async () => {
        try { setRuns((await jget('/runs?limit=10')).runs || []) } catch { /* 静默 */ }
      }
      const refreshCurrent = async () => {
        try { setCur(await jget('/run')) } catch { /* 还没跑过 */ }
      }
      const refreshAuto = async () => {
        try { setAuto(await jget('/automation')) } catch { /* 拿不到就不显示 */ }
      }

      React.useEffect(() => {
        ;(async () => {
          await refreshCfg()
          await refreshStatus()
          await Promise.all([refreshRuns(), refreshCurrent(), refreshAuto()])
          setLoaded(true)
        })()
        const t1 = setInterval(() => { refreshStatus().catch(() => {}) }, 6_000)
        const t2 = setInterval(() => {
          refreshCurrent().then(() => refreshRuns().catch(() => {})).catch(() => {})
        }, 3_000)
        return () => { clearInterval(t1); clearInterval(t2) }
      }, [])

      const patch = async (p) => {
        setBusy('cfg')
        try {
          const r = await jpost('/config', { patch: p })
          setCfg(r.config)
          if (r.hostSynced === false) say('warn', '已存到插件自己的配置文件（宿主没接收，不影响使用）')
          else say('ok', '已保存')
        } catch (e) { say('bad', `保存失败：${e.message}`) }
        finally { setBusy('') }
      }

      const probe = async () => {
        await refreshModels(cfg?.ollamaUrl)
        await refreshStatus()
      }

      const startLogin = async () => {
        setBusy('login'); setQr(null)
        try {
          const r = await jpost('/login/start', {})
          if (!r.ok) throw new Error(r.error || '出码失败')
          setLoginKey(r.key)
          setQr({ url: r.url, qr: r.qr })
          say('ok', r.qr ? '二维码好了，用手机 B站 扫' : '拿到链接了，但没画出图（venv 里可能缺 qrcode）')
        } catch (e) { say('bad', `出码失败：${e.message}`) }
        finally { setBusy('') }
      }

      const pollLogin = async () => {
        if (!loginKey) return say('warn', '先点「扫码登录」')
        setBusy('poll')
        try {
          const r = await jpost('/login/poll', { key: loginKey })
          if (!r.ok) throw new Error(r.error || '查询失败')
          if (r.status === 'ok') {
            say('ok', `登录成功：${r.uname || ''}`)
            setQr(null); setLoginKey('')
            await refreshStatus()
          } else if (r.status === 'expired') {
            say('warn', '二维码过期了，重新出码'); setQr(null); setLoginKey('')
          } else say('warn', r.status === 'scanned' ? '扫到了，等手机确认' : '还没扫')
        } catch (e) { say('bad', `查询失败：${e.message}`) }
        finally { setBusy('') }
      }

      /* 「跑一轮」按钮已移除：启动交给宿主定时任务，这里只负责把指令递到用户手上 */
      const copyAuto = async () => {
        const text = auto?.sample || ''
        if (!text) { say('warn', '还没拿到定时任务指令，点一下刷新'); return }
        try {
          await navigator.clipboard.writeText(text)
          say('ok', '已复制。去对话里发给 DeepSeek，它就会用 schedule_create 建一条定时任务。')
        } catch {
          say('warn', '浏览器不让写剪贴板，请手动选中下面那段文字复制。')
        }
      }

      const cancelRun = async () => {
        setBusy('cancel')
        try {
          const r = await jpost('/cancel', {})
          if (!r.ok) throw new Error(r.error || '取消失败')
          say('ok', '已停止')
          await refreshStatus(); await refreshCurrent()
        } catch (e) { say('bad', `停止失败：${e.message}`) }
        finally { setBusy('') }
      }

      const openRun = async (id) => {
        try { setCur(await jget(`/run?run_id=${encodeURIComponent(id)}`)) }
        catch (e) { say('bad', `读这轮失败：${e.message}`) }
      }

      /* 引擎真正在用的数 vs 这一页显示的 —— 对不上就一键重新下发 */
      const SYNC_LABEL = {
        daily_video_max: '一天最多浏览',
        ai_tech: 'AI技术配额',
        ai_fun: 'AI二创配额',
        write: '发评论权限',
        model: '本地模型',
      }
      const resync = async () => {
        setBusy('sync')
        try {
          const r = await jpost('/sync', {})
          if (!r.ok) throw new Error(r.error || '下发失败')
          say('ok', '已把这一页的设置重新下发给引擎')
          await refreshCfg()
          await refreshStatus()
        } catch (e) { say('bad', `下发失败：${e.message}`) }
        finally { setBusy('') }
      }

      const savePersona = async () => {
        if (!pDraft.trim()) return say('warn', '人设不能为空')
        setBusy('persona')
        try {
          const r = await jpost('/persona', { text: pDraft })
          if (!r.ok) throw new Error(r.error || '保存失败')
          setPersona({ text: r.text, path: r.summary?.path || persona?.path })
          setPDraft(r.text); setPDirty(false)
          say('ok', `人设已保存${r.backup ? '（旧的备份成 .bak）' : ''}`)
        } catch (e) { say('bad', `保存人设失败：${e.message}`) }
        finally { setBusy('') }
      }

      if (!loaded) return h('div', { className: 'bs-panel' }, h('div', { className: 'bs-loading' }, '载入中…'))

      const c = cfg || {}
      const st = status || {}
      const eng = st.engine || {}
      /** 引擎自检里没通过的项，用中文名报出来 */
      const engFails = Object.entries(eng.checks || {})
        .filter(([, v]) => v === false)
        .map(([k]) => CHECK_LABEL[k] || k)
      const engText = eng.ok
        ? '引擎就绪'
        : (!eng.checks ? '引擎未连接' : `引擎未就绪（${engFails.join('/') || '未知'}）`)
      const engTitle = eng.error || (engFails.length ? `未通过：${engFails.join('、')}` : '')
      const active = st.active
      const running = Boolean(active)
      const modelList = models?.models || []
      const hasModel = modelList.some((m) => m.name === c.model)

      const header = h('div', { className: 'bs-head' },
        h('div', null,
          h('h2', { className: 'bs-title' }, 'B站视频评论设置'),
          h('div', { className: 'bs-caption' },
            running
              ? `正在跑：${active.steps || 0} 步`
              : st.bili?.logged_in
                ? `${st.bili.uname || '已登录'} · ${c.model || '未选模型'}`
                : '还没登录 B站',
          ),
        ),
        h('div', { className: 'bs-head-actions' },
          h(Btn, { onClick: () => { refreshStatus(); refreshRuns(); refreshCurrent(); refreshAuto() }, disabled: Boolean(busy) }, '刷新'),
          running
            ? h(Btn, { onClick: cancelRun, kind: 'danger', disabled: Boolean(busy) }, busy === 'cancel' ? '停止中…' : '停止')
            : null,
        ),
      )

      const notice = msg ? h('div', { className: `bs-notice ${msg.kind}` }, msg.text) : null

      /* 不同步提示条：引擎在用的限额 / 写权限 / 模型和这一页不一样时出现 */
      const syncInfo = st.sync
      const syncBanner = syncInfo && syncInfo.ok === false
        ? h('div', { className: 'bs-notice warn bs-syncbar' },
          h('div', null,
            '引擎在用的数和这一页不一样：'
            + (syncInfo.diffs || [])
              .map((d) => `${SYNC_LABEL[d.key] || d.key} 页面 ${d.ui} / 引擎 ${d.engine}`)
              .join('；')),
          h('div', { className: 'bs-inline' },
            h(Btn, { onClick: resync, disabled: Boolean(busy) },
              busy === 'sync' ? '下发中…' : '重新下发给引擎')))
        : null

      /* ═══════════ 本地模型 ═══════════ */
      const modelSec = h('section', { className: 'bs-sec' },
        h('h3', { className: 'bs-sec-title' }, '本地模型'),
        h('div', { className: 'bs-card' },

          h(Row, { label: 'Ollama 地址', hint: '一般不用改' },
            h('div', { className: 'bs-inline' },
              h(Txt, {
                value: c.ollamaUrl, placeholder: 'http://127.0.0.1:11434', width: '210px',
                onCommit: (v) => { patch({ ollamaUrl: v.trim() }); setTimeout(() => refreshModels(v.trim()), 400) },
              }),
              h(Btn, { onClick: probe, disabled: Boolean(busy) }, busy === 'models' ? '探测中…' : '探测'),
            )),

          h(Row, {
            label: '模型',
            hint: models === null
              ? '点「探测」看本机装了哪些，也可以手填'
              : models.ok
                ? `本机 ${modelList.length} 个模型${hasModel ? '' : '，当前这个不在列表里'}`
                : (models.error || '连不上 Ollama'),
          }, h('div', { className: 'bs-inline' },
            manualModel || !models?.ok
              ? h(Txt, {
                value: c.model, placeholder: 'qwen3.5:4b-32k', width: '210px',
                onCommit: (v) => patch({ model: v.trim() }),
              })
              : h('select', {
                className: 'bs-select',
                value: hasModel ? c.model : '',
                onChange: (e) => { if (e.target.value) patch({ model: e.target.value }) },
              },
                hasModel ? null : h('option', { value: '' }, `（当前：${c.model || '未设置'}）`),
                ...modelList.map((m) => h('option', { key: m.name, value: m.name },
                  `${m.name}${m.params ? ` · ${m.params}` : ''}`)),
              ),
            h(Btn, { onClick: () => setManualModel(!manualModel) }, manualModel ? '用列表' : '手填'),
          )),

          modelList.length && !hasModel
            ? h('div', { className: 'bs-warn' },
              `「${c.model}」不在本机模型里。换一个，或者先 ollama pull ${c.model}`,
              h('div', { className: 'bs-inline bs-actions tight' },
                ...modelList.slice(0, 4).map((m) => h(Btn, {
                  key: m.name, onClick: () => patch({ model: m.name }),
                }, m.name))))
            : null,

          h('div', { className: 'bs-grid' },
            h('div', { className: 'bs-cell' },
              h('div', { className: 'bs-label' }, '上下文窗口'),
              h('div', { className: 'bs-hint' }, '能同时记住多少 token'),
              h(Num, {
                value: c.numCtx, min: 2048, max: 131072, step: 1024, suffix: 'token',
                onChange: (v) => patch({ numCtx: v }),
              }),
              h('div', { className: 'bs-inline bs-quick' },
                ...[8192, 32768, 65536, 131072].map((n) => h(Btn, {
                  key: n, onClick: () => patch({ numCtx: n }),
                  kind: Number(c.numCtx) === n ? 'primary' : '',
                }, n === 131072 ? '128k' : `${n / 1024}k`)))),
            h('div', { className: 'bs-cell' },
              h('div', { className: 'bs-label' }, '单次输出上限'),
              h('div', { className: 'bs-hint' }, '太小会话说一半被截断'),
              h(Num, {
                value: c.numPredict, min: 256, max: 8192, step: 64, suffix: 'token',
                onChange: (v) => patch({ numPredict: v }),
              })),
            h('div', { className: 'bs-cell' },
              h('div', { className: 'bs-label' }, '温度'),
              h('div', { className: 'bs-hint' }, '越低越稳，越高越飘'),
              h(Float, { value: c.temperature, min: 0, max: 1.2, step: 0.05, onChange: (v) => patch({ temperature: v }) })),
            h('div', { className: 'bs-cell' },
              h('div', { className: 'bs-label' }, '单轮最长'),
              h('div', { className: 'bs-hint' }, '到点就收尾'),
              h(Num, {
                value: c.numMinutes, min: 1, max: 120, suffix: '分钟',
                onChange: (v) => patch({ numMinutes: v }),
              })),
          ),

          h(Row, { label: '自动续跑', hint: '上下文满了自己接着跑' },
            h(Toggle, { value: Boolean(c.autoContinue), onChange: (v) => patch({ autoContinue: v }) })),

          c.autoContinue ? h('div', { className: 'bs-children' },
            h(Row, { label: '最多续几次', hint: '每次调用单独计，不是每天' },
              h(Num, {
                value: c.autoContinueMax, min: 0, max: 10, suffix: '次',
                onChange: (v) => patch({ autoContinueMax: v }),
              })),
            h(Row, { label: '拿 run_id 最多等', hint: '只是等启动回执，不是等跑完（跑完看状态）' },
              h(Num, {
                value: c.waitSec, min: 3, max: 30, step: 1, suffix: '秒',
                onChange: (v) => patch({ waitSec: v }),
              })),
          ) : null,

          h(Row, { label: '一轮看几条', hint: '一条大约 40~90 秒' },
            h(Num, {
              value: c.videosPerRun, min: 1, max: 10, suffix: '条',
              onChange: (v) => patch({ videosPerRun: v }),
            })),

          h('div', { className: 'bs-checks' },
            h(Dot, { ok: st.python ? st.python.hasDeps : undefined, text: st.python ? `Python ${st.python.version}` : 'Python 未探测' }),
            h(Dot, { ok: models?.ok, text: models?.ok ? `Ollama 通（${modelList.length}）` : (models ? 'Ollama 不通' : 'Ollama 未探测') }),
            h(Dot, { ok: models ? hasModel : undefined, text: hasModel ? '模型就绪' : '模型未就绪' }),
            h(Dot, { ok: st.bili?.logged_in, text: st.bili?.logged_in ? 'B站 已登录' : 'B站 未登录' }),
            h(Dot, { ok: eng.ok, text: engText, title: engTitle }),
          ),

          /* 引擎没就绪时，把是哪一环拦住说清楚 —— 光一个红点等于没说 */
          !eng.ok
            ? h('div', { className: 'bs-note' },
              eng.checks
                ? `引擎自检没过：${engFails.length ? engFails.join('、') : '未知项'}。`
                  + (engFails.includes('Ollama') && models?.ok
                    ? '页面能连上 Ollama、子进程却连不上，多半是它继承了系统代理（回环请求被转成了 502）。插件已给子进程加 NO_PROXY，重启宿主生效。'
                    : 'Ollama 那项最常见的原因就是没启动；跑一次 bilisee_health 能看到完整原因。')
                : `引擎还没起来${eng.error ? `：${eng.error}` : ''}。第一次调用会自动拉起，再不通就点「刷新」看日志。`)
            : null,

          (st.pythonError || eng.error || models?.hint)
            ? h('pre', { className: 'bs-pre bad' },
              [st.pythonError, eng.error, models?.hint].filter(Boolean).join('\n'))
            : null,

          models && !models.ok
            ? h('div', { className: 'bs-warn' }, '连不上 Ollama。先 ollama serve，再 ollama pull 一个模型。')
            : null,
        ),
      )

      /* ═══════════ 自动化（启动入口已移除，交给宿主定时任务） ═══════════ */
      const autoSec = h('section', { className: 'bs-sec' },
        h('h3', { className: 'bs-sec-title' }, '自动化'),
        h('div', { className: 'bs-card' },
          h('div', { className: 'bs-note' },
            '这一页没有启动按钮：什么时候跑由 harness 自带的定时任务决定 —— '
            + '到点唤起一轮对话，模型调 bilisee_tick 把这一轮交出去，插件不自己定节奏。'),

          auto?.sample
            ? h('div', null,
              h('div', { className: 'bs-inline bs-actions' },
                h(Btn, { onClick: copyAuto }, '复制定时任务指令'),
                h(Btn, { onClick: refreshAuto }, '刷新')),
              h('pre', { className: 'bs-pre' }, auto.sample),
            )
            : h('div', { className: 'bs-hint' }, '还没取到指令。点右上角「刷新」再试。'),

          auto?.last_run
            ? h('div', { className: 'bs-hint' },
              `上一轮：${auto.last_run.run_id} · ${statusLabel(auto.last_run.status)}`
              + ` · ${String(auto.last_run.created_at || '').replace('T', ' ').slice(5, 16)}`)
            : null,

          (auto?.notes || []).map((n, i) => h('div', { key: i, className: 'bs-hint' }, `· ${n}`)),
        ),
      )

      /* ═══════════ 这一轮 ═══════════ */
      const runSec = h('section', { className: 'bs-sec' },
        h('h3', { className: 'bs-sec-title' }, '这一轮'),
        h('div', { className: 'bs-card' },
          !cur || !cur.ok
            ? h('div', { className: 'bs-empty' },
              running ? '刚启动，稍等一下。' : '还没有任何一轮。等定时任务触发，或让 DeepSeek 调一次 bilisee_tick。')
            : h('div', null,
              h('div', { className: 'bs-runhead' },
                h(Tag, { kind: statusKind(cur.state.status) }, statusLabel(cur.state.status)),
                h('span', { className: 'bs-hint' },
                  `${cur.state.steps || 0} 步 · 看 ${cur.state.videos_seen || 0} 条 · 发 ${cur.state.comments_posted || 0} 条`),
              ),
              running ? h('div', { className: 'bs-progress' }, h('i', null)) : null,
              cur.state.last_error ? h('pre', { className: 'bs-pre bad' }, cur.state.last_error) : null,
              cur.digest
                ? h('div', null,
                  h('div', { className: 'bs-inline bs-actions tight' },
                    h(Btn, { onClick: () => setShowDigest(!showDigest) },
                      showDigest ? '收起记录' : '看记录'),
                    h(Btn, { onClick: () => refreshCurrent() }, '刷新')),
                  showDigest ? h('pre', { className: 'bs-pre bs-digest' }, cur.digest) : null)
                : null,
            ),
        ),
      )

      /* ═══════════ 账号 ═══════════ */
      const account = h('section', { className: 'bs-sec' },
        h('h3', { className: 'bs-sec-title' }, '账号'),
        h('div', { className: 'bs-card' },
          h(Row, {
            label: 'B站 登录',
            hint: st.bili?.logged_in ? `已登录：${st.bili.uname || '?'}` : '扫一次码就行',
          }, h('div', { className: 'bs-inline' },
            h(Btn, { onClick: startLogin, disabled: Boolean(busy) }, busy === 'login' ? '出码中…' : '扫码登录'),
            loginKey ? h(Btn, { onClick: pollLogin, disabled: Boolean(busy) }, busy === 'poll' ? '查询中…' : '我扫了') : null,
          )),
          qr ? h('div', { className: 'bs-qr' },
            qr.qr
              ? h('img', { src: qr.qr, alt: 'B站登录二维码', width: 200, height: 200 })
              : h('div', { className: 'bs-hint' },
                '图没生成，手动打开：',
                h('a', { href: qr.url, target: '_blank', rel: 'noreferrer' }, qr.url)),
            h('div', { className: 'bs-hint' }, '手机 B站 → 我的 → 扫一扫。约 180 秒过期。'),
          ) : null,
        ),
      )

      /* ═══════════ 看什么 ═══════════ */
      const topic = h('section', { className: 'bs-sec' },
        h('h3', { className: 'bs-sec-title' }, '看什么'),
        h('div', { className: 'bs-card' },
          h(Row, { label: '只看最近几天', hint: '0 表示不限' },
            h(Num, { value: c.freshDays ?? 2, min: 0, max: 14, suffix: '天', onChange: (v) => patch({ freshDays: v }) })),

          h(Row, { label: '关注的 UP', hint: '填 UID，逗号分隔；这些 UP 不过质量门槛' },
            h(Txt, {
              value: c.watchMids, placeholder: '填 UP 的 UID，逗号分隔；留空表示不特别关注', width: '260px',
              onCommit: (v) => patch({ watchMids: v }),
            })),
        ),
      )

      /* ═══════════ 视频详细设置 ═══════════ */
      const dailyMax = Math.max(1, Number(c.dailyVideoMax) || 6)
      const techQ = Math.max(0, Number(c.aiTechQuota) || 0)
      const funQ = Math.max(0, Number(c.aiFunQuota) || 0)
      const otherQ = otherQuotaTotal(c.otherPrefs)
      const wanted = techQ + funQ + otherQ
      const overflow = wanted > dailyMax

      const subBlock = (title, quotaKey, kwKey, keywordHint) => h('div', { className: 'bs-block' },
        h('div', { className: 'bs-block-head' },
          h('span', { className: 'bs-block-title' }, title),
          h('div', { className: 'bs-inline' },
            h('span', { className: 'bs-hint' }, '允许'),
            h(Num, { value: c[quotaKey], min: 0, max: 20, suffix: '条', onChange: (v) => patch({ [quotaKey]: v }) }),
          ),
        ),
        h(Txt, {
          value: c[kwKey], placeholder: keywordHint, width: '100%',
          onCommit: (v) => patch({ [kwKey]: v }),
        }),
      )

      const detail = h('section', { className: 'bs-sec' },
        h('h3', { className: 'bs-sec-title' }, '视频详细设置'),
        h('div', { className: 'bs-card' },
          h(Row, {
            label: '一天最多浏览',
            hint: st.daily ? `今天已看 ${st.daily.viewed} 条，还剩 ${st.daily.remaining} 条` : '跨轮累计，超了不再开新视频',
          }, h(Num, {
            value: c.dailyVideoMax, min: 1, max: 20, suffix: '条',
            onChange: (v) => patch({ dailyVideoMax: v }),
          })),

          h(Row, { label: 'UP 粉丝下限', hint: '低于这个数不看（关注的 UP 豁免）' },
            h(Num, { value: c.minUpFans, min: 0, max: 10000000, step: 1000, suffix: '粉', onChange: (v) => patch({ minUpFans: v }) })),

          h(Row, { label: '视频播放下限', hint: '低于这个数不看（关注的 UP 豁免）' },
            h(Num, { value: c.minPlay, min: 0, max: 10000000, step: 1000, suffix: '播放', onChange: (v) => patch({ minPlay: v }) })),

          h('div', { className: 'bs-quota-head' },
            h('span', null, '板块配额'),
            h('span', { className: `bs-quota-sum${overflow ? ' bad' : ''}` },
              `已分配 ${wanted} / ${dailyMax} 条`),
          ),

          subBlock('AI技术（科技）', 'aiTechQuota', 'aiTechKeywords', 'AI 大模型,开源模型,编程工具'),
          subBlock('AI二创（娱乐）', 'aiFunQuota', 'aiFunKeywords', 'AI 绘画,AI 配音,AI 整活'),

          h('div', { className: 'bs-block' },
            h('div', { className: 'bs-block-head' },
              h('span', { className: 'bs-block-title' }, '其他视频'),
            ),
            h(Txt, {
              value: c.otherPrefs, multiline: true, rows: 3, mono: true, width: '100%',
              placeholder: '数码评测 | 手机,相机,耳机 | 2\n游戏实况 | 单机,攻略 | 1',
              onCommit: (v) => patch({ otherPrefs: v }),
            }),
            h('div', { className: 'bs-hint' }, '一行一条：名称 | 关键词 | 条数'),
          ),

          overflow
            ? h('div', { className: 'bs-warn' },
              h('div', null,
                `三块加起来 ${wanted} 条，超过「一天最多浏览」的 ${dailyMax} 条。`),
              h('div', { className: 'bs-hint' }, '超出的部分不会被看。要么调小板块，要么把上限调大。'),
              h('div', { className: 'bs-inline bs-actions tight' },
                h(Btn, { onClick: () => patch({ dailyVideoMax: wanted }) }, `把上限调到 ${wanted}`)))
            : h('div', { className: 'bs-hint bs-free' },
              wanted < dailyMax
                ? `还剩 ${dailyMax - wanted} 条给自主浏览（非 AI 内容）`
                : `三块正好用满 ${dailyMax} 条`),
        ),
      )

      /* ═══════════ 发评论 ═══════════ */
      const writeSec = h('section', { className: 'bs-sec' },
        h('h3', { className: 'bs-sec-title' }, '发评论'),
        h('div', { className: `bs-card${c.writeEnabled ? ' danger-on' : ''}` },
          h(Row, {
            label: '允许发评论',
            hint: '打开后才能真发，会拿你的号说话',
          }, h(Toggle, { value: Boolean(c.writeEnabled), onChange: (v) => patch({ writeEnabled: v }) })),

          c.writeEnabled ? h('div', { className: 'bs-children' },
            h(Row, { label: '每天最多', hint: '硬上限，超了直接拒' },
              h(Num, { value: c.maxPostsPerDay, min: 0, max: 20, suffix: '条', onChange: (v) => patch({ maxPostsPerDay: v }) })),

            h(Row, { label: '两次间隔', hint: '0 表示不冷却' },
              h(Num, { value: c.minIntervalMin, min: 0, max: 1440, suffix: '分钟', onChange: (v) => patch({ minIntervalMin: v }) })),
          ) : null,
        ),
      )

      /* ═══════════ 视频总结 ═══════════ */
      const sum = cur?.summary || null
      const sumSec = h('section', { className: 'bs-sec' },
        h('h3', { className: 'bs-sec-title' }, '视频总结'),
        h('div', { className: 'bs-card' },
          h(Row, { label: '每轮自动总结', hint: '本地先整合，云端只读结论' },
            h(Toggle, { value: Boolean(c.summaryEnabled), onChange: (v) => patch({ summaryEnabled: v }) })),

          sum
            ? h('div', { className: 'bs-summary' },
              h('div', { className: 'bs-inline' },
                h(Tag, { kind: sum.degraded ? 'warn' : 'ok' }, sum.degraded ? '规则拼装' : '模型整合'),
                h('span', { className: 'bs-meta' },
                  `${sum.chars || 0} 字` +
                  (sum.raw_chars ? ` / 原始记忆 ${Math.round(sum.raw_chars / 100) / 10}k 字符` : '') +
                  (sum.saved_ratio ? ` · 省 ${Math.round(sum.saved_ratio * 100)}%` : '')),
              ),
              h('pre', { className: 'bs-pre bs-summary-text' }, sum.text),
              sum.usage?.prompt_eval_count
                ? h('div', { className: 'bs-meta' },
                  `本地整合额外花 ${sum.usage.prompt_eval_count} prompt + ${sum.usage.eval_count || 0} eval token（本机免费）`)
                : null,
            )
            : h('div', { className: 'bs-empty' }, c.summaryEnabled ? '每轮跑完之后这里会出现总结。' : '已关闭。'),
        ),
      )

      /* ═══════════ 人设 ═══════════ */
      const pSum = persona?.summary || null
      const personaSec = h('section', { className: 'bs-sec' },
        h('h3', { className: 'bs-sec-title' }, '人设'),
        h('div', { className: 'bs-card' },
          h(Row, {
            label: pSum?.name ? `当前：${pSum.name}` : '刷视频人设',
            hint: persona?.path || '决定它怎么说话',
          }, h(Btn, {
            onClick: async () => {
              const next = !pOpen
              setPOpen(next)
              if (next && !persona) {
                try {
                  const r = await jget('/persona')
                  if (!r.ok) throw new Error(r.error || '读失败')
                  setPersona({ text: r.text, path: r.path, summary: r.summary })
                  setPDraft(r.text); setPDirty(false)
                } catch (e) { say('bad', `读人设失败：${e.message}`) }
              }
            },
          }, pOpen ? '收起' : '编辑')),

          pOpen ? h('div', { className: 'bs-children' },
            h('textarea', {
              className: 'bs-text bs-persona-edit bs-mono',
              value: pDraft, rows: 16, spellCheck: false,
              onChange: (e) => { setPDraft(e.target.value); setPDirty(true) },
            }),
            h('div', { className: 'bs-draft-actions' },
              h(Btn, { onClick: savePersona, kind: 'primary', disabled: Boolean(busy) || !pDirty },
                busy === 'persona' ? '保存中…' : '保存'),
              pDirty ? h('span', { className: 'bs-hint' }, '有改动没保存') : null,
            ),
          ) : null,
        ),
      )

      /* ═══════════ 历史 ═══════════ */
      const histSec = h('section', { className: 'bs-sec' },
        h('h3', { className: 'bs-sec-title' }, `历史（${runs.length}）`),
        runs.length === 0
          ? h('div', { className: 'bs-empty' }, '还没跑过。')
          : h('div', { className: 'bs-runs' },
            ...runs.map((r) => h('div', {
              key: r.run_id,
              className: `bs-runitem${cur?.run_id === r.run_id ? ' on' : ''}`,
              onClick: () => openRun(r.run_id),
            },
              h(Tag, { kind: statusKind(r.status) }, statusLabel(r.status)),
              h('span', { className: 'bs-hint' },
                `${r.steps} 步 · ${r.videos_seen} 条 · 发 ${r.comments_posted}`),
              h('span', { className: 'bs-runwhen' }, (r.created_at || '').replace('T', ' ').slice(5, 16)),
            )),
          ),
      )

      /* ═══════════ 运行状态 ═══════════ */
      const infoSec = h('section', { className: 'bs-sec' },
        h('h3', { className: 'bs-sec-title' }, '运行状态'),
        h('div', { className: 'bs-kv' },
          h('div', null, h('span', null, '今天'), h('b', null,
            `${st.today?.runs ?? 0} 轮 / ${st.today?.picked ?? 0} 条 / 发 ${st.today?.posted ?? 0}`)),
          h('div', null, h('span', null, '今日额度'), h('b', null,
            st.bili?.quota_left !== undefined ? `还剩 ${st.bili.quota_left} 条` : '—')),
          h('div', null, h('span', null, 'Python'), h('b', null, st.python ? st.python.version : '不可用')),
          h('div', null, h('span', null, '插件目录'), h('b', { title: st.home }, st.home || '—')),
        ),
      )

      return h('div', { className: 'bs-panel' },
        header, notice, syncBanner, modelSec, autoSec, runSec, account, topic, detail, writeSec, sumSec, personaSec, histSec, infoSec)
    }

    /* ── 样式 ──
     * 一套尺寸/颜色 token，别在规则里散着写字面值。
     * 圆角只有三档：容器 8 / 控件 6 / 标签 4；间距只有三档：6 / 10 / 20。
     */
    const CSS = `
.bs-panel {
  --bs-line: var(--border, rgba(128,128,128,.2));
  --bs-line-soft: rgba(128,128,128,.12);
  --bs-fill: rgba(128,128,128,.06);
  --bs-ok: #2ea043;
  --bs-bad: #e5534b;
  --bs-warn: #bf8700;
  --bs-r-lg: 8px;
  --bs-r-md: 6px;
  --bs-r-sm: 4px;
  padding: 4px 2px 24px;
  font-size: 13px;
}
.bs-loading { padding: 40px 0; text-align: center; opacity: .6; }

/* ── 头部 ── */
.bs-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 16px; margin-bottom: 14px; }
.bs-title { margin: 0 0 2px; font-size: 17px; font-weight: 600; line-height: 1.3; }
.bs-caption { font-size: 12px; opacity: .6; line-height: 1.5; }
.bs-head-actions { display: flex; gap: 8px; flex: 0 0 auto; }

/* ── 提示条 ── */
.bs-notice { margin: 0 0 12px; padding: 8px 11px; border-radius: var(--bs-r-lg); font-size: 12px; line-height: 1.55; }
.bs-notice.ok { background: rgba(46,160,67,.12); color: var(--bs-ok); }
.bs-notice.warn { background: rgba(191,135,0,.12); color: var(--bs-warn); }
.bs-notice.bad { background: rgba(229,83,75,.12); color: var(--bs-bad); }
.bs-syncbar > div + div { margin-top: 8px; }

/* ── 区块 ── */
.bs-sec { margin-bottom: 20px; }
.bs-sec-title { margin: 0 0 8px; font-size: 11.5px; font-weight: 600; letter-spacing: .02em; opacity: .52; }
.bs-card { border: 1px solid var(--bs-line); border-radius: var(--bs-r-lg); padding: 4px 12px; }
.bs-card.danger-on { border-left: 3px solid rgba(229,83,75,.55); }

/* ── 行 ── */
.bs-row { display: flex; align-items: flex-start; justify-content: space-between; gap: 16px; padding: 10px 0; }
.bs-row + .bs-row { border-top: 1px solid var(--bs-line-soft); }
.bs-row-main { flex: 1; min-width: 0; }
.bs-label { font-size: 13px; font-weight: 500; line-height: 1.4; }
.bs-hint { font-size: 11.5px; opacity: .58; line-height: 1.5; margin-top: 2px; }
.bs-meta { font-size: 11px; opacity: .48; line-height: 1.6; }
.bs-row-ctl { flex: 0 0 auto; display: flex; align-items: center; gap: 8px; }
.bs-inline { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
.bs-actions { margin-top: 10px; }
.bs-actions.tight { margin-top: 6px; }

/* ── 控件 ── */
.bs-toggle { width: 38px; height: 21px; border-radius: 11px; border: none; background: rgba(128,128,128,.32); position: relative; cursor: pointer; transition: background .16s; padding: 0; flex: 0 0 auto; }
.bs-toggle.on { background: var(--bs-ok); }
.bs-toggle.disabled { opacity: .45; cursor: not-allowed; }
.bs-knob { position: absolute; top: 2.5px; left: 2.5px; width: 16px; height: 16px; border-radius: 50%; background: #fff; transition: transform .16s; }
.bs-toggle.on .bs-knob { transform: translateX(17px); }
.bs-num { display: flex; align-items: center; gap: 6px; }
.bs-num input { width: 76px; padding: 4px 7px; border-radius: var(--bs-r-md); border: 1px solid var(--bs-line); background: var(--input-bg, transparent); color: inherit; font-size: 12.5px; text-align: right; }
.bs-suffix { font-size: 11.5px; opacity: .55; white-space: nowrap; min-width: 34px; }
.bs-float { display: flex; align-items: center; gap: 8px; }
.bs-float input[type=range] { width: 108px; accent-color: var(--bs-ok); }
.bs-select { padding: 4px 8px; border-radius: var(--bs-r-md); border: 1px solid var(--bs-line); background: var(--input-bg, transparent); color: inherit; font-size: 12.5px; max-width: 260px; }
.bs-text { padding: 4px 7px; border-radius: var(--bs-r-md); border: 1px solid var(--bs-line); background: var(--input-bg, transparent); color: inherit; font-size: 12.5px; box-sizing: border-box; }
.bs-btn { padding: 5px 12px; border-radius: var(--bs-r-md); border: 1px solid var(--bs-line); background: transparent; color: inherit; font-size: 12.5px; line-height: 1.4; cursor: pointer; white-space: nowrap; }
.bs-btn:hover:not(:disabled) { background: rgba(128,128,128,.1); }
.bs-btn:disabled { opacity: .42; cursor: not-allowed; }
.bs-btn.primary { background: var(--bs-ok); border-color: var(--bs-ok); color: #fff; }
.bs-btn.primary:hover:not(:disabled) { background: #2c9a3e; }
.bs-btn.danger { border-color: rgba(229,83,75,.5); color: var(--bs-bad); }
.bs-btn.danger:hover:not(:disabled) { background: rgba(229,83,75,.1); }

/* ── 网格 / 配额 ── */
.bs-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(214px, 1fr)); gap: 10px; padding: 10px 0 4px; border-top: 1px solid var(--bs-line-soft); }
.bs-cell { padding: 9px 11px; border-radius: var(--bs-r-lg); background: var(--bs-fill); min-width: 0; }
.bs-cell .bs-num { margin-top: 7px; }
.bs-quick { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 6px; }
.bs-quick .bs-btn { padding: 2px 8px; font-size: 11px; }
.bs-checks { display: flex; flex-wrap: wrap; gap: 14px; padding: 10px 0; border-top: 1px solid var(--bs-line-soft); }
.bs-dot { display: inline-flex; align-items: center; gap: 6px; font-size: 11.5px; opacity: .78; }
.bs-dot i { width: 7px; height: 7px; border-radius: 50%; display: inline-block; }
.bs-dot i.ok { background: var(--bs-ok); }
.bs-dot i.bad { background: var(--bs-bad); }
.bs-dot i.idle { background: rgba(128,128,128,.5); }

/* ── 二维码 / 空态 / 警告 ── */
.bs-qr { margin: 4px 0 12px; padding: 12px; border-radius: var(--bs-r-lg); background: var(--bs-fill); display: flex; flex-direction: column; align-items: center; gap: 8px; }
.bs-qr img { border-radius: var(--bs-r-md); background: #fff; padding: 6px; }
.bs-warn { margin: 10px 0 4px; padding: 8px 11px; border-radius: var(--bs-r-lg); background: rgba(191,135,0,.1); font-size: 11.5px; line-height: 1.6; }
.bs-empty { padding: 18px; text-align: center; font-size: 12px; opacity: .5; border: 1px dashed var(--bs-line); border-radius: var(--bs-r-lg); }
.bs-note { padding: 8px 0 2px; font-size: 12px; line-height: 1.65; opacity: .72; }

/* ── 这一轮 / 历史 ── */
.bs-runhead { display: flex; align-items: center; gap: 9px; flex-wrap: wrap; padding: 10px 0 4px; }
.bs-progress { height: 3px; border-radius: 2px; background: rgba(128,128,128,.16); overflow: hidden; margin: 6px 0; }
.bs-progress i { display: block; height: 100%; width: 38%; background: var(--bs-ok); animation: bs-slide 1.5s ease-in-out infinite; }
@keyframes bs-slide { 0% { transform: translateX(-100%); } 100% { transform: translateX(360%); } }
.bs-runs { display: flex; flex-direction: column; gap: 6px; }
.bs-runitem { display: flex; align-items: center; gap: 9px; padding: 8px 11px; border-radius: var(--bs-r-lg); border: 1px solid var(--bs-line-soft); cursor: pointer; font-size: 11.5px; }
.bs-runitem:hover { background: rgba(128,128,128,.07); }
.bs-runitem.on { border-color: rgba(46,160,67,.5); background: rgba(46,160,67,.06); }
.bs-runwhen { margin-left: auto; opacity: .45; white-space: nowrap; }

/* ── 子块（配额分组等） ── */
.bs-block { padding: 10px 0; border-top: 1px solid var(--bs-line-soft); }
.bs-block-head { display: flex; align-items: center; justify-content: space-between; gap: 12px; margin-bottom: 6px; }
.bs-block-title { font-size: 12.5px; font-weight: 500; }
.bs-children { border-left: 2px solid var(--bs-line); margin: 2px 0 8px 4px; padding-left: 12px; }
.bs-quota-head { display: flex; align-items: center; justify-content: space-between; padding: 12px 0 2px; border-top: 1px solid var(--bs-line-soft); font-size: 12px; }
.bs-quota-head span:first-child { font-weight: 500; }
.bs-quota-sum { font-size: 11.5px; opacity: .6; }
.bs-quota-sum.bad { color: var(--bs-warn); opacity: 1; }
.bs-free { padding: 8px 0 2px; }

/* ── 总结 / 人设 ── */
.bs-summary { padding: 8px 0 6px; border-top: 1px solid var(--bs-line-soft); }
.bs-summary-text { max-height: 320px; font-size: 12px; line-height: 1.7; }
.bs-mono { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }
.bs-persona-edit { width: 100%; resize: vertical; line-height: 1.6; font-size: 12px; tab-size: 2; padding: 9px 11px; margin-top: 8px; }
.bs-draft-actions { display: flex; align-items: center; gap: 8px; margin-top: 8px; padding-top: 8px; border-top: 1px solid var(--bs-line-soft); }

/* ── 标签 / KV / 代码块 ── */
.bs-tag { display: inline-block; padding: 1px 7px; border-radius: var(--bs-r-sm); font-size: 10.5px; line-height: 1.7; background: rgba(128,128,128,.16); }
.bs-tag.warn { background: rgba(191,135,0,.18); color: var(--bs-warn); }
.bs-tag.ok { background: rgba(46,160,67,.18); color: var(--bs-ok); }
.bs-tag.bad { background: rgba(229,83,75,.18); color: var(--bs-bad); }
.bs-kv { border: 1px solid var(--bs-line); border-radius: var(--bs-r-lg); padding: 4px 13px; }
.bs-kv > div { display: flex; justify-content: space-between; gap: 12px; padding: 7px 0; font-size: 12px; }
.bs-kv > div + div { border-top: 1px solid var(--bs-line-soft); }
.bs-kv span { opacity: .6; white-space: nowrap; }
.bs-kv b { font-weight: 500; max-width: 62%; text-align: right; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.bs-pre { margin: 9px 0 0; padding: 9px 11px; border-radius: var(--bs-r-lg); background: rgba(128,128,128,.08); font-size: 11px; line-height: 1.6; white-space: pre-wrap; word-break: break-word; max-height: 200px; overflow: auto; }
.bs-pre.bad { background: rgba(229,83,75,.08); color: var(--bs-bad); }
.bs-digest { max-height: 420px; font-size: 11.5px; }
`

    return {
      inject: ['slots'],
      apply(ctx) {
        const style = document.createElement('style')
        style.setAttribute('data-plugin-css', 'dsh-bili-see')
        style.textContent = CSS
        document.head.appendChild(style)
        ctx.effect(() => () => style.remove(), 'dsh-bili-see: 设置页样式')

        const slots = (typeof ctx.get === 'function' ? ctx.get('slots') : undefined) || ctx.slots
        if (!slots) return
        slots.inject('settings.section', () => slots.register(
          { name: 'settings.section', id: 'bili-see', order: 32, label: 'B站视频评论设置' },
          () => h(Panel),
        ))
      },
    }
  },
})
