import { createHash, randomBytes, timingSafeEqual } from 'node:crypto'
import { chmodSync, existsSync, mkdirSync, unlinkSync, writeFileSync } from 'node:fs'
import { createServer, type IncomingMessage, type ServerResponse } from 'node:http'
import { createConnection } from 'node:net'
import { dirname, join } from 'node:path'
import { URL } from 'node:url'

type Listener = (...args: any[]) => void

type WebContentsLike = {
  session?: SessionLike
  executeJavaScript: (code: string, userGesture?: boolean) => Promise<any>
  setAudioMuted?: (muted: boolean) => void
  setWindowOpenHandler?: (handler: (details: { url: string }) => { action: 'deny' | 'allow' }) => void
  on?: (event: string, listener: Listener) => void
  removeListener?: (event: string, listener: Listener) => void
  isDestroyed?: () => boolean
  stop?: () => void
}

type WindowLike = {
  webContents: WebContentsLike
  loadURL: (url: string, options?: any) => Promise<void>
  on: (event: string, listener: Listener) => void
  removeListener?: (event: string, listener: Listener) => void
  isDestroyed: () => boolean
  destroy: () => void
}

type BrowserWindowCtor = new (options: Record<string, any>) => WindowLike

type SessionLike = {
  on: (event: string, listener: Listener) => void
  off?: (event: string, listener: Listener) => void
}

type ServerLike = ReturnType<typeof createServer>

type ProviderId = 'claude' | 'codex' | 'zai' | 'grok'
type ProviderStatus =
  | 'ok'
  | 'auth_required'
  | 'challenge'
  | 'structure_unknown'
  | 'timeout'
  | 'desktop_down'
  | 'blocked_target'

export type BudgetMeasureServiceDeps = {
  appDataPath: string
  BrowserWindow: BrowserWindowCtor
  fromPartition: (partition: string) => SessionLike
  logger?: (message: string) => void
  now?: () => Date
  challengeGraceMs?: number
  chmodSocket?: (path: string, mode: number) => void
}

export type BudgetMeasureProviderResult = {
  status: ProviderStatus
  observed_at: string | null
  page_url?: string
  quote?: string
  dom_appendix?: string
  evidence?: {
    page_url: string
    quote: string
    dom_appendix: string
    quote_sha256: string
  }
  grok_dom?: {
    aria: string | null
    breakdown: Array<{ label: string; pct: string }>
    text: string
    cardHead: string
  }
  detail?: string
}

export type BudgetMeasureRun = {
  run_id: string
  started_at: string
  finished_at?: string
  app_state: 'ready' | 'desktop_down'
  status: 'running' | 'complete'
  providers: Partial<Record<ProviderId, BudgetMeasureProviderResult>>
}

const PARTITION = 'persist:hermes-preview'
const SOCKET_NAME = 'budget-measure.sock'
const TOKEN_NAME = 'budget-measure.token'
const SOCKET_MODE = 0o600
const TOKEN_MODE = 0o600
const WINDOW_WIDTH = 1280
const WINDOW_HEIGHT = 800
const NAVIGATION_TIMEOUT_MS = 20_000
const PROVIDER_TIMEOUT_MS = 45_000
const WAIT_TIMEOUT_MS = 30_000
const RUN_TIMEOUT_MS = 150_000
const RESULT_TTL_MS = 10 * 60_000
const CHALLENGE_GRACE_MS = 16_000
const POLL_MS = 500
const PROVIDERS: ProviderId[] = ['zai', 'grok', 'claude', 'codex']

const PROVIDER_HOSTS: Record<ProviderId, string[]> = {
  zai: ['z.ai', 'chat.z.ai'],
  grok: ['grok.com', 'x.com'],
  claude: ['claude.ai'],
  codex: ['chatgpt.com', 'openai.com']
}

const PROVIDER_CONFIG: Record<ProviderId, { url: string; warmup: string; marker: RegExp }> = {
  zai: {
    url: 'https://z.ai/manage-apikey/coding-plan/personal/usage',
    warmup: 'https://z.ai/',
    marker: /(?:weekly|wöchentliche)\s+(?:quota|quote)/i
  },
  grok: {
    url: 'https://grok.com/?_s=usage',
    warmup: 'https://grok.com/',
    marker: /supergrok[\s\w]*?-?limit/i
  },
  claude: {
    url: 'https://claude.ai/new#settings/usage',
    warmup: 'https://claude.ai/new',
    marker: /(?:all\s+models|alle\s+modelle)/i
  },
  codex: {
    url: 'https://chatgpt.com/codex/cloud/settings/analytics#usage',
    warmup: 'https://chatgpt.com/',
    marker: /(?:weekly\s+usage\s+limit|wöchentliches\s+nutzungslimit)/i
  }
}

const LOGIN_RE = /unlock all features by logging in|continue\s+with\s+(?:google|email|github)|sign in to continue|melde dich an,?\s+um fortzufahren|log in to continue/i
const CHALLENGE_URL_RE = /(?:\/challenge|cf-chl|__cf_chl_|\/verify(?:[/?#]|$))/i
const CHALLENGE_BODY_RE = /just a moment|nur einen moment|attention required|verifying you are human|cloudflare|ray id/i
const EMAIL_RE = /[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}/gi

const PAGE_STATE_JS = `(() => ({
  url: location.href,
  title: document.title || '',
  captured_at: new Date().toISOString(),
  text: (document.body && document.body.innerText || '').slice(0, 8000)
}))()`

const PROVIDER_EXTRACT_JS: Record<ProviderId, string> = {
  zai: `(() => {
    const el = document.querySelector('.ant-avatar, [class*="avatar" i]')
    const value = el ? String(el.innerText || el.textContent || '').trim() : ''
    return { accountAvatar: value.length >= 1 && value.length <= 3 ? value : '' }
  })()`,
  claude: `(() => {
    const el = document.querySelector('[data-testid="user-menu-button"], [data-testid*="user-menu" i]')
    const value = el ? String(el.innerText || el.textContent || '').trim() : ''
    return { accountMarker: value.slice(0, 80) }
  })()`,
  codex: `(() => {
    const el = document.querySelector('[data-testid="profile-button"], [data-testid*="profile" i], button[aria-label*="profile" i]')
    const value = el ? String(el.innerText || el.textContent || '').trim() : ''
    const match = value.match(/\b(pro|plus|team|enterprise)\b/i)
    return { planBadge: match ? match[1].toUpperCase() : '' }
  })()`,
  grok: `(() => {
    const heading = Array.from(document.querySelectorAll('h1,h2,h3,h4')).find(el => /SuperGrok.*-?Limit/i.test((el.textContent || '').trim()))
    let box = heading ? heading.parentElement : null
    let flow = null
    for (let i = 0; i < 6 && box; i += 1) {
      flow = Array.from(box.querySelectorAll('number-flow-react[aria-label]')).find(el => /%/.test(el.getAttribute('aria-label') || ''))
      if (flow) break
      box = box.parentElement
    }
    const breakdown = []
    if (box) box.querySelectorAll('*').forEach(el => {
      if (el.children.length) return
      const match = String(el.textContent || '').trim().match(/^([0-9]{1,3}(?:[,.][0-9]+)?) *%$/)
      if (!match) return
      const label = String(el.previousElementSibling && el.previousElementSibling.textContent || '').trim()
      if (label) breakdown.push({ label: label.slice(0, 120), pct: match[1] })
    })
    const handle = Array.from(document.querySelectorAll('button,a,[role="button"]'))
      .map(el => String(el.innerText || el.textContent || '').trim())
      .find(value => /^@[A-Za-z0-9_]{2,}$/.test(value)) || ''
    return {
      handle,
      aria: flow ? flow.getAttribute('aria-label') : null,
      breakdown,
      text: box ? String(box.innerText || '').slice(0, 2000) : '',
      cardHead: heading ? String(heading.textContent || '').trim().slice(0, 200) : ''
    }
  })()`
}

export function budgetChallengeDetected(url: string, text: string): boolean {
  return CHALLENGE_URL_RE.test(url) || CHALLENGE_BODY_RE.test(String(text).slice(0, 4096))
}

function isoNow(now: () => Date): string {
  return now().toISOString()
}

function json(res: ServerResponse, status: number, body: unknown) {
  const payload = JSON.stringify(body)
  res.statusCode = status
  res.setHeader('content-type', 'application/json; charset=utf-8')
  res.setHeader('cache-control', 'no-store')
  res.end(payload)
}

function sha256(value: string): string {
  return createHash('sha256').update(value, 'utf8').digest('hex')
}

export function redactBudgetOutput(input: string): string {
  return String(input || '')
    .replace(EMAIL_RE, '[REDACTED:EMAIL]')
    .replace(/([?&](?:token|key|secret|magic|sig)=)[^&#\s]*/gi, '$1[REDACTED]')
    .replace(/\bBearer\s+[A-Za-z0-9._~+/=-]+/gi, 'Bearer [REDACTED]')
    .replace(/\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b/g, '[REDACTED:JWT]')
    .replace(/\/magic\/[^/?#\s]+/gi, '/magic/[REDACTED]')
}

function safePageUrl(value: string): string {
  try {
    const parsed = new URL(value)

    return `${parsed.origin}${redactBudgetOutput(parsed.pathname)}`
  } catch {
    return ''
  }
}

function compactText(value: string, max = 8000): string {
  return redactBudgetOutput(String(value || '').replace(/[ \t]+/g, ' ').replace(/\n{2,}/g, '\n').trim()).slice(0, max)
}

function isProvider(provider: string): provider is ProviderId {
  return PROVIDERS.includes(provider as ProviderId)
}

function allowedTarget(provider: ProviderId, target: string): boolean {
  try {
    const parsed = new URL(target)
    const host = parsed.hostname.toLowerCase()

    return parsed.protocol === 'https:' && PROVIDER_HOSTS[provider].some(allowed => host === allowed || host.endsWith(`.${allowed}`))
  } catch {
    return false
  }
}

function readJsonBody(req: IncomingMessage, maxBytes = 32_768): Promise<Record<string, any>> {
  return new Promise((resolve, reject) => {
    let body = ''
    let bytes = 0
    req.setEncoding('utf8')
    req.on('data', chunk => {
      bytes += Buffer.byteLength(chunk)

      if (bytes > maxBytes) {
        reject(new Error('request_too_large'))
        req.destroy()

        return
      }

      body += chunk
    })
    req.on('end', () => {
      if (!body.trim()) {return resolve({})}

      try {
        const parsed = JSON.parse(body)
        resolve(parsed && typeof parsed === 'object' && !Array.isArray(parsed) ? parsed : {})
      } catch {
        reject(new Error('invalid_json'))
      }
    })
    req.on('error', reject)
  })
}

function timeout<T>(promise: Promise<T>, ms: number, label: string): Promise<T> {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error(label)), ms)
    promise.then(value => {
      clearTimeout(timer)
      resolve(value)
    }, error => {
      clearTimeout(timer)
      reject(error)
    })
  })
}

function writeSecret(path: string, value: string) {
  mkdirSync(dirname(path), { recursive: true, mode: 0o700 })
  writeFileSync(path, `${value}\n`, { encoding: 'utf8', mode: TOKEN_MODE })
  chmodSync(path, TOKEN_MODE)
}

function removeIfPresent(path: string) {
  try {
    unlinkSync(path)
  } catch (error: any) {
    if (error?.code !== 'ENOENT') {throw error}
  }
}

async function canConnectUnixSocket(path: string): Promise<boolean> {
  if (!existsSync(path)) {return false}

  return new Promise(resolve => {
    const socket = createConnection({ path })
    let settled = false

    const finish = (value: boolean) => {
      if (settled) {return}
      settled = true
      socket.destroy()
      resolve(value)
    }

    socket.once('connect', () => finish(true))
    socket.once('error', () => finish(false))
    socket.setTimeout(300, () => finish(false))
  })
}

export function budgetMeasurePaths(appDataPath: string) {
  return {
    socketPath: join(appDataPath, SOCKET_NAME),
    tokenPath: join(appDataPath, TOKEN_NAME)
  }
}

export class BudgetMeasureService {
  private readonly deps: Required<Pick<BudgetMeasureServiceDeps, 'BrowserWindow' | 'fromPartition'>> & BudgetMeasureServiceDeps
  private readonly now: () => Date
  private readonly challengeGraceMs: number
  private readonly socketPath: string
  private readonly tokenPath: string
  private readonly results = new Map<string, { run: BudgetMeasureRun; expiresAt: number }>()
  private server: ServerLike | null = null
  private cleanupTimer: NodeJS.Timeout | null = null
  private token = ''
  private measurementWindow: WindowLike | null = null
  private measurementSession: SessionLike | null = null
  private downloadHandler: Listener | null = null
  private navigationHandler: Listener | null = null
  private openWindowHandler: ((details: { url: string }) => { action: 'deny' | 'allow' }) | null = null
  private closedHandler: Listener | null = null
  private rendererGoneHandler: Listener | null = null
  private busy = false
  private measureEpoch = 0
  private currentRunStarted: string | null = null
  private activeAbort: AbortController | null = null

  constructor(deps: BudgetMeasureServiceDeps) {
    this.deps = deps as any
    this.now = deps.now || (() => new Date())
    this.challengeGraceMs = Math.max(0, deps.challengeGraceMs ?? CHALLENGE_GRACE_MS)
    const paths = budgetMeasurePaths(deps.appDataPath)
    this.socketPath = paths.socketPath
    this.tokenPath = paths.tokenPath
  }

  getPaths() {
    return { socketPath: this.socketPath, tokenPath: this.tokenPath }
  }

  getStatus() {
    return {
      status: this.server ? 'up' : 'down',
      app_ready: Boolean(this.server),
      partition: this.measurementSession ? 'ok' : 'cold',
      busy: this.busy,
      version: 1
    }
  }

  async start() {
    if (this.server) {return}

    if (await canConnectUnixSocket(this.socketPath)) {
      throw new Error('budget_measure_socket_in_use')
    }

    removeIfPresent(this.socketPath)
    this.token = randomBytes(32).toString('base64url')
    writeSecret(this.tokenPath, this.token)

    const server = createServer((req, res) => {
      void this.handleRequest(req, res)
    })

    this.server = server
    await new Promise<void>((resolve, reject) => {
      const onError = (error: Error) => {
        server.off('error', onError)
        reject(error)
      }

      server.once('error', onError)
      server.listen(this.socketPath, () => {
        server.off('error', onError)
        const chmodSocket = this.deps.chmodSocket || chmodSync

        try {
          chmodSocket(this.socketPath, SOCKET_MODE)
        } catch (error) {
          server.close()
          this.server = null
          removeIfPresent(this.socketPath)
          removeIfPresent(this.tokenPath)
          reject(error)

          return
        }

        resolve()
      })
    }).catch(error => {
      this.server = null
      removeIfPresent(this.socketPath)
      removeIfPresent(this.tokenPath)
      throw error
    })

    this.cleanupTimer = setInterval(() => this.cleanupResults(), 60_000)
    this.cleanupTimer.unref?.()
    this.deps.logger?.(`[budget] D1-RPC bereit (${this.socketPath})`)
  }

  async stop() {
    this.activeAbort?.abort()
    this.activeAbort = null
    this.busy = false
    this.currentRunStarted = null
    this.destroyMeasurementWindow()

    if (this.cleanupTimer) {
      clearInterval(this.cleanupTimer)
      this.cleanupTimer = null
    }

    const server = this.server
    this.server = null
    removeIfPresent(this.socketPath)
    removeIfPresent(this.tokenPath)

    if (server) {
      await new Promise<void>(resolve => {
        server.close(() => resolve())
        setTimeout(resolve, 1000).unref?.()
      })
    }

    removeIfPresent(this.socketPath)
    removeIfPresent(this.tokenPath)
    this.results.clear()
    this.token = ''
  }

  onWake() {
    // The next provider run deliberately navigates afresh. Keeping the window
    // alive here avoids touching the shared partition during macOS resume.
    this.deps.logger?.('[budget] macOS-Wake erkannt; nächster Lauf navigiert frisch')
  }

  private cleanupResults() {
    const now = Date.now()

    for (const [runId, entry] of this.results) {
      if (entry.expiresAt <= now) {this.results.delete(runId)}
    }
  }

  private authenticated(req: IncomingMessage, body?: Record<string, any>): boolean {
    const supplied = String(req.headers['x-hermes-budget-token'] || body?.auth || '')

    if (!supplied || !this.token) {return false}
    const left = Buffer.from(supplied)
    const right = Buffer.from(this.token)

    return left.length === right.length && timingSafeEqual(left, right)
  }

  private async handleRequest(req: IncomingMessage, res: ServerResponse) {
    let body: Record<string, any> = {}

    try {
      if (req.method === 'POST') {body = await readJsonBody(req)}
    } catch (error: any) {
      json(res, 400, { error: error?.message === 'request_too_large' ? 'request_too_large' : 'invalid_json' })

      return
    }

    if (!this.authenticated(req, body)) {
      json(res, 401, { error: 'unauthorized' })

      return
    }

    const pathname = (() => {
      try { return new URL(req.url || '/', 'http://budget.local').pathname } catch { return '/' }
    })()

    if (req.method === 'GET' && pathname === '/health') {
      json(res, 200, this.getStatus())

      return
    }

    if (req.method === 'POST' && pathname === '/measure') {
      await this.acceptMeasurement(body, res)

      return
    }

    if (req.method === 'GET' && pathname === '/result') {
      this.sendResult(req, res)

      return
    }

    if (req.method === 'POST' && pathname === '/cancel') {
      this.activeAbort?.abort()
      json(res, 200, { accepted: true, cancelled: this.busy })

      return
    }

    json(res, 404, { error: 'not_found' })
  }

  private async acceptMeasurement(body: Record<string, any>, res: ServerResponse) {
    if (this.busy) {
      json(res, 409, { accepted: false, reason: 'busy', current_run_started: this.currentRunStarted })

      return
    }

    const requested = body.providers == null ? [...PROVIDERS] : body.providers

    if (!Array.isArray(requested) || requested.length === 0 || requested.some(provider => !isProvider(provider))) {
      json(res, 400, { accepted: false, reason: 'invalid_providers' })

      return
    }

    const runId = `${this.now().toISOString().replace(/[-:.TZ]/g, '').slice(0, 14)}-${randomBytes(3).toString('hex')}`

    const run: BudgetMeasureRun = {
      run_id: runId,
      started_at: isoNow(this.now),
      app_state: 'ready',
      status: 'running',
      providers: {}
    }

    this.results.set(runId, { run, expiresAt: Date.now() + RESULT_TTL_MS })
    this.measureEpoch += 1
    this.busy = true
    this.currentRunStarted = run.started_at
    this.activeAbort = new AbortController()
    json(res, 202, { accepted: true, run_id: runId })
    void this.runMeasurement(run, requested as ProviderId[], this.activeAbort.signal)
  }

  private sendResult(req: IncomingMessage, res: ServerResponse) {
    let runId = ''

    try { runId = new URL(req.url || '/', 'http://budget.local').searchParams.get('run_id') || '' } catch { /* malformed query means unknown_run */ }
    const entry = this.results.get(runId)

    if (!entry) {
      json(res, 404, { found: false, reason: 'unknown_run' })

      return
    }

    if (entry.expiresAt <= Date.now()) {
      this.results.delete(runId)
      json(res, 404, { found: false, reason: 'expired' })

      return
    }

    if (entry.run.status === 'running') {
      json(res, 202, { found: true, status: 'running', run_id: runId })

      return
    }

    json(res, 200, entry.run)
  }

  private async runMeasurement(run: BudgetMeasureRun, providers: ProviderId[], signal: AbortSignal) {
    const deadline = Date.now() + RUN_TIMEOUT_MS

    try {
      const window = await this.ensureMeasurementWindow()

      for (const provider of providers) {
        if (signal.aborted || Date.now() >= deadline) {
          run.providers[provider] = this.failure('desktop_down', 'D1-Laufzeitlimit erreicht')

          continue
        }

        run.providers[provider] = await this.measureProvider(window, provider, signal, Math.min(PROVIDER_TIMEOUT_MS, deadline - Date.now()))
      }
    } catch (error) {
      run.app_state = 'desktop_down'

      for (const provider of providers) {
        if (!run.providers[provider]) {run.providers[provider] = this.failure('desktop_down', 'Desktop-Messfenster nicht verfügbar')}
      }

      this.deps.logger?.(`[budget] D1-Lauf abgebrochen: ${String(error)}`)
    } finally {
      run.finished_at = isoNow(this.now)
      run.status = 'complete'
      const entry = this.results.get(run.run_id)

      if (entry) {entry.expiresAt = Date.now() + RESULT_TTL_MS}
      const epoch = this.measureEpoch
      this.busy = false
      this.currentRunStarted = null
      this.activeAbort = null
      await this.parkMeasurementWindow(epoch)
    }
  }

  private failure(status: ProviderStatus, detail: string): BudgetMeasureProviderResult {
    return { status, observed_at: null, detail: redactBudgetOutput(detail).slice(0, 400) }
  }

  private async parkMeasurementWindow(epoch: number) {
    const window = this.measurementWindow

    if (this.measureEpoch !== epoch || this.busy) {return}

    if (!window || window.isDestroyed()) {return}

    try {
      await timeout(window.loadURL('about:blank', { timeout: 5000 }), 5000, 'park_timeout')
    } catch {
      if (this.measureEpoch === epoch && !this.busy) {this.destroyMeasurementWindow()}

      return
    }

    if (this.measureEpoch !== epoch || this.busy) {return}
  }

  private async ensureMeasurementWindow(): Promise<WindowLike> {
    if (this.measurementWindow && !this.measurementWindow.isDestroyed()) {return this.measurementWindow}
    const partitionSession = this.deps.fromPartition(PARTITION)

    const window = new this.deps.BrowserWindow({
      show: false,
      width: WINDOW_WIDTH,
      height: WINDOW_HEIGHT,
      title: 'Hermes Budget-Messung',
      autoHideMenuBar: true,
      webPreferences: {
        session: partitionSession,
        contextIsolation: true,
        javascript: true,
        nodeIntegration: false,
        sandbox: true,
        webSecurity: true,
        backgroundThrottling: false
      }
    })

    this.measurementWindow = window
    this.measurementSession = partitionSession

    try { window.webContents.setAudioMuted?.(true) } catch { /* muting is best effort */ }

    this.navigationHandler = (event: any, target: string) => {
      const provider = this.providerForTarget(target)

      if (!provider || !allowedTarget(provider, target)) {event?.preventDefault?.()}
    }

    window.webContents.on?.('will-navigate', this.navigationHandler)

    this.rendererGoneHandler = (_event: any, details: any) => {
      this.deps.logger?.(`[budget] Renderer-Crash im Messfenster (${String(details?.reason || 'unbekannt')})`)
      this.destroyMeasurementWindow()
    }

    window.webContents.on?.('render-process-gone', this.rendererGoneHandler)

    this.openWindowHandler = details => {
      this.deps.logger?.(`[budget] Popup blockiert (${safePageUrl(details.url)})`)

      return { action: 'deny' }
    }

    window.webContents.setWindowOpenHandler?.(this.openWindowHandler)

    this.downloadHandler = (event: any, item: any, contents: any) => {
      if (contents === window.webContents || event?.sender === window.webContents) {item?.cancel?.()}
    }

    partitionSession.on('will-download', this.downloadHandler)

    this.closedHandler = () => {
      if (this.measurementWindow === window) {this.destroyMeasurementWindow()}
    }

    window.on('closed', this.closedHandler)

    return window
  }

  private providerForTarget(target: string): ProviderId | null {
    for (const provider of PROVIDERS) {if (allowedTarget(provider, target)) {return provider}}

    return null
  }

  private destroyMeasurementWindow() {
    const window = this.measurementWindow

    if (!window) {return}

    try {
      if (this.navigationHandler) {window.webContents.removeListener?.('will-navigate', this.navigationHandler)}

      if (this.rendererGoneHandler) {window.webContents.removeListener?.('render-process-gone', this.rendererGoneHandler)}

      if (this.closedHandler) {window.removeListener?.('closed', this.closedHandler)}

      if (this.measurementSession && this.downloadHandler) {this.measurementSession.off?.('will-download', this.downloadHandler)}

      if (!window.isDestroyed()) { window.destroy() }
    } catch { /* teardown is best effort during app shutdown */ }

    this.measurementWindow = null
    this.measurementSession = null
    this.navigationHandler = null
    this.rendererGoneHandler = null
    this.downloadHandler = null
    this.closedHandler = null
    this.openWindowHandler = null
  }

  private async measureProvider(window: WindowLike, provider: ProviderId, signal: AbortSignal, limitMs: number): Promise<BudgetMeasureProviderResult> {
    const deadline = Date.now() + Math.max(1000, limitMs)

    try {
      await timeout(window.loadURL(PROVIDER_CONFIG[provider].url, { timeout: NAVIGATION_TIMEOUT_MS }), Math.min(NAVIGATION_TIMEOUT_MS, deadline - Date.now()), 'navigation_timeout')
    } catch (error: any) {
      try { window.webContents.stop?.() } catch { /* stop is best effort after navigation failure */ }

      if (signal.aborted) {return this.failure('desktop_down', 'Messung abgebrochen')}

      if (error?.message === 'navigation_timeout') {return this.failure('timeout', 'Navigation-Timeout')}

      return this.failure('desktop_down', 'Navigation fehlgeschlagen')
    }

    let challengeAt = 0
    let lastState: any = null

    while (!signal.aborted && Date.now() < deadline) {
      if (window.isDestroyed() || window.webContents.isDestroyed?.()) {
        return this.failure('desktop_down', 'Messfenster zerstört')
      }

      lastState = await this.readPageState(window)
      const text = String(lastState?.text || '')
      const url = String(lastState?.url || PROVIDER_CONFIG[provider].url)

      if (this.isChallenge(url, text)) {
        challengeAt ||= Date.now()

        if (Date.now() - challengeAt >= CHALLENGE_GRACE_MS) {
          return this.failure('challenge', 'Challenge-Seite erkannt')
        }
      } else if (text && LOGIN_RE.test(text) && !PROVIDER_CONFIG[provider].marker.test(text)) {
        return this.failure('auth_required', 'Anmeldung im Hermes-Preview erforderlich')
      } else if (text && PROVIDER_CONFIG[provider].marker.test(text)) {
        return this.extractProviderResult(window, provider, lastState)
      }

      await new Promise(resolve => setTimeout(resolve, POLL_MS))
    }

    if (signal.aborted) {return this.failure('desktop_down', 'Messung abgebrochen')}
    const detail = lastState?.text ? 'Provider-Seite geladen, Struktur unbekannt' : 'Provider-Seite ohne lesbaren Inhalt'

    return this.failure(lastState?.text ? 'structure_unknown' : 'timeout', detail)
  }

  private isChallenge(url: string, text: string): boolean {
    return budgetChallengeDetected(url, text)
  }

  private async readPageState(window: WindowLike): Promise<{ url: string; title: string; text: string } | null> {
    try {
      return await timeout(window.webContents.executeJavaScript(PAGE_STATE_JS, false), 5000, 'dom_read_timeout')
    } catch {
      return null
    }
  }

  private async extractProviderResult(window: WindowLike, provider: ProviderId, state: any): Promise<BudgetMeasureProviderResult> {
    let extracted: any = {}

    try {
      extracted = await timeout(window.webContents.executeJavaScript(PROVIDER_EXTRACT_JS[provider], false), 5000, 'dom_extract_timeout')
    } catch {
      extracted = {}
    }

    const requiredEvidence = provider === 'zai'
      ? Boolean(extracted.accountAvatar)
      : provider === 'claude'
        ? Boolean(extracted.accountMarker)
        : provider === 'codex'
          ? Boolean(extracted.planBadge)
          : /^@[A-Za-z0-9_]{2,}$/.test(String(extracted.handle || ''))

    if (!requiredEvidence) {
      return this.failure('structure_unknown', 'Konto-Nachweis im Messfenster fehlt')
    }

    const quote = compactText(state?.text || '')
    const pageUrl = safePageUrl(state?.url || PROVIDER_CONFIG[provider].url)
    const appendixParts: string[] = []

    if (provider === 'zai' && extracted.accountAvatar) {appendixParts.push(`[DOM Konto-Avatar: ${compactText(extracted.accountAvatar, 20)}]`)}

    if (provider === 'claude' && extracted.accountMarker) {appendixParts.push(`[DOM Nutzer-Knopf: ${compactText(extracted.accountMarker, 80)}]`)}

    if (provider === 'codex' && extracted.planBadge) {appendixParts.push(`[DOM Profil-Badge: ${compactText(extracted.planBadge, 20)}]`)}
    let grokDom: any = undefined

    if (provider === 'grok') {
      const handle = /^@[A-Za-z0-9_]{2,}$/.test(String(extracted.handle || '')) ? extracted.handle : ''
      const cardParts = [extracted.cardHead, extracted.text, extracted.aria ? `aria-label: ${extracted.aria}` : ''].filter(Boolean)

      if (cardParts.length) {appendixParts.push(`[Nutzungs-Karte: ${compactText(cardParts.join(' | '), 2000)}]`)}

      if (handle) {appendixParts.push(`[DOM Konto-Handle: ${handle}]`)}
      grokDom = {
        aria: extracted.aria ? redactBudgetOutput(String(extracted.aria)).slice(0, 200) : null,
        breakdown: Array.isArray(extracted.breakdown) ? extracted.breakdown.map(item => ({
          label: compactText(item?.label, 120),
          pct: String(item?.pct || '').slice(0, 20)
        })) : [],
        text: compactText(extracted.text, 2000),
        cardHead: compactText(extracted.cardHead, 200)
      }
    }

    const domAppendix = appendixParts.join(' ')

    const evidence = {
      page_url: pageUrl,
      quote,
      dom_appendix: domAppendix,
      quote_sha256: sha256(`${quote}${domAppendix}`)
    }

    return {
      status: 'ok',
      observed_at: isoNow(this.now),
      page_url: pageUrl,
      quote,
      dom_appendix: domAppendix,
      evidence,
      grok_dom: grokDom
    }
  }
}
