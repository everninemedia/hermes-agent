import assert from 'node:assert/strict'
import { EventEmitter } from 'node:events'
import { existsSync, readFileSync, writeFileSync } from 'node:fs'
import { mkdtemp, rm } from 'node:fs/promises'
import { request } from 'node:http'
import { tmpdir } from 'node:os'
import { join } from 'node:path'

import { test } from 'vitest'

import { budgetChallengeDetected, BudgetMeasureService, redactBudgetOutput } from './budget-measure-service'

class FakeSession extends EventEmitter {
  off(event: string, listener: (...args: any[]) => void) {
    return this.removeListener(event, listener)
  }
}

class FakeWindow extends EventEmitter {
  static delayMs = 0
  static missingEvidence = false
  static ignoreStaticClaudeHash = false
  static claudeUsageText = ''
  // Grok Bot: zweite Karte "Weekly Grok Bot Limit" auf grok.com (seit 14.09.2026).
  // null = Seite zeigt nur die SuperGrok-Karte (Stand bis 13.09.2026).
  static grokBot: { aria: string | null; text: string; cardHead: string } | null = null
  // Späte Bot-Karte (28.09.2026): erst ab dem n-ten Seitenlesen sichtbar.
  static grokBotAfterReads = 0
  static grokReads = 0
  static last: FakeWindow | null = null
  static lastOptions: any = null
  currentUrl = 'about:blank'
  destroyed = false
  session: FakeSession
  webContents: any
  removedEvents: string[] = []
  hashOpenedViaJs = false
  ignoreMouseEvents: boolean | null = null

  setIgnoreMouseEvents(ignore: boolean) {
    this.ignoreMouseEvents = ignore
  }

  hiddenInMissionControl: boolean | null = null

  setHiddenInMissionControl(hidden: boolean) {
    this.hiddenInMissionControl = hidden
  }

  constructor(options: any) {
    super()
    FakeWindow.last = this
    this.session = options.webPreferences.session
    FakeWindow.lastOptions = options
    this.webContents = {
      session: this.session,
      setAudioMuted: () => {},
      setWindowOpenHandler: () => {},
      on: () => {},
      removeListener: (event: string) => {
        this.removedEvents.push(event)
      },
      stop: () => {},
      executeJavaScript: async (code: string) => {
        const hashAssign = code.match(/location\.hash\s*=\s*(['"])(.*?)\1/)
        if (hashAssign) {
          const hash = hashAssign[2]
          const base = this.currentUrl.split('#')[0]
          this.currentUrl = hash ? `${base}${hash.startsWith('#') ? hash : `#${hash}`}` : base
          this.hashOpenedViaJs = /settings\/usage/i.test(hash)
          return hash
        }

        if (code.includes('url: location.href')) {
          if (/grok\.com/.test(this.currentUrl)) {FakeWindow.grokReads += 1}
          const botVisible = FakeWindow.grokBot && FakeWindow.grokReads > FakeWindow.grokBotAfterReads
          const states: Record<string, string> = {
            'https://z.ai/manage-apikey/coding-plan/personal/usage': 'Usage Weekly Quota 21 % Used Reset Time: 2026-08-30 12:46',
            'https://grok.com/?_s=usage': botVisible && FakeWindow.grokBot
              ? `Nutzung Wöchentliches SuperGrok Heavy-Limit gebraucht Grok Build 53% Setzt 27. August 2026 um 18:04 zurück ${FakeWindow.grokBot.cardHead} ${FakeWindow.grokBot.text}`
              : 'Nutzung Wöchentliches SuperGrok Heavy-Limit gebraucht Grok Build 53% Setzt 27. August 2026 um 18:04 zurück',
            'https://claude.ai/new#settings/usage': FakeWindow.claudeUsageText || 'Nutzung Alle Modelle Zurücksetzung Sa., 14:59 31 % verwendet',
            'https://chatgpt.com/codex/cloud/settings/analytics#usage': 'Weekly usage limit 91% remaining Resets Aug 31, 2026 9:02 AM'
          }
          let text = states[this.currentUrl] || ''
          if (
            FakeWindow.ignoreStaticClaudeHash
            && /claude\.ai/i.test(this.currentUrl)
            && /#settings\/usage/i.test(this.currentUrl)
            && !this.hashOpenedViaJs
          ) {
            text = 'Claude Chat Wie kann ich dir heute helfen?'
          }

          return { url: this.currentUrl, title: 'provider', text }
        }

        if (FakeWindow.missingEvidence) {return {}}

        if (code.includes('accountAvatar')) {return { accountAvatar: 'T' }}

        if (code.includes('accountMarker')) {return { accountMarker: 'Tobi' }}

        if (code.includes('planBadge')) {return { planBadge: 'PRO' }}

        if (code.includes('handle')) {
          return {
            handle: '@tobi',
            aria: '53%',
            breakdown: [{ label: 'Grok Build', pct: '53' }],
            text: 'Grok Build 53%',
            cardHead: 'Wöchentliches SuperGrok Heavy-Limit',
            bot: FakeWindow.grokReads > FakeWindow.grokBotAfterReads ? FakeWindow.grokBot : null
          }
        }

        return {}
      }
    }
  }

  loadURL(url: string) {
    this.currentUrl = url
    if (/claude\.ai/i.test(url) && /#settings\/usage/i.test(url)) {
      this.hashOpenedViaJs = false
    }

    return FakeWindow.delayMs > 0 ? new Promise<void>(resolve => setTimeout(resolve, FakeWindow.delayMs)) : Promise.resolve()
  }

  isDestroyed() {
    return this.destroyed
  }

  destroy() {
    this.destroyed = true
    this.emit('closed')
  }
}

function rpc(socketPath: string, token: string, method: string, path: string, body?: unknown) {
  return new Promise<{ status: number; body: any }>((resolve, reject) => {
    const payload = body === undefined ? '' : JSON.stringify(body)

    const req = request({
      socketPath,
      path,
      method,
      headers: {
        'x-hermes-budget-token': token,
        ...(payload ? { 'content-type': 'application/json', 'content-length': Buffer.byteLength(payload) } : {})
      }
    }, res => {
      let text = ''
      res.setEncoding('utf8')
      res.on('data', chunk => { text += chunk })
      res.on('end', () => resolve({ status: res.statusCode || 0, body: text ? JSON.parse(text) : null }))
    })

    req.on('error', reject)

    if (payload) {req.write(payload)}
    req.end()
  })
}

// Der Service pollt Provider-Seiten mit POLL_MS = 500 ms; ein Lauf ohne
// Bot-Karte wartet real einen Gnaden-Poll ab, bevor er komplettiert. Der
// Helfer wartet deshalb bis zu einer begrenzten monotonen Deadline statt
// einer festen Versuchzahl, die schon vor dem ersten regulären Poll ablaufen
// kann (40 * 5 ms = 200 ms reichten dafuer nicht aus).
const COMPLETE_TIMEOUT_MS = 10_000

async function waitForComplete(socketPath: string, token: string, runId: string, timeoutMs = COMPLETE_TIMEOUT_MS) {
  const deadline = performance.now() + timeoutMs

  while (performance.now() < deadline) {
    const polled = await rpc(socketPath, token, 'GET', `/result?run_id=${encodeURIComponent(runId)}`)
    const payload = polled.body?.result && polled.body.result.providers ? polled.body.result : polled.body

    if (polled.status === 200 && payload?.status === 'complete' && payload.providers) {
      return payload
    }

    await new Promise(resolve => setTimeout(resolve, 5))
  }

  throw new Error(`kein complete fuer ${runId}`)
}

test('redactBudgetOutput removes credential-shaped output before transport', () => {
  const value = redactBudgetOutput('mail t@example.com ?token=abc Bearer abc /magic/one eyJabc.def.ghi')
  assert.equal(value.includes('t@example.com'), false)
  assert.equal(value.includes('abc'), false)
  assert.match(value, /REDACTED/)
})

test('challenge detection is fail-closed even with a provider shell and plausible number', () => {
  assert.equal(budgetChallengeDetected('https://claude.ai/new', 'All models 31% used Just a moment... 31%'), true)
  assert.equal(budgetChallengeDetected('https://claude.ai/new', 'All models 31% used'), false)
})

test('D1 RPC authenticates, enforces one run, and returns normalized provider results', async () => {
  const root = await mkdtemp(join(tmpdir(), 'hermes-budget-test-'))
  const session = new FakeSession()

  const service = new BudgetMeasureService({
    appDataPath: root,
    BrowserWindow: FakeWindow as any,
    hashSettleMs: 0,
    grokBotGraceMs: 0,
    fromPartition: () => session,
    now: () => new Date('2026-08-25T08:08:00.000Z')
  })

  try {
    await service.start()
    const { socketPath, tokenPath } = service.getPaths()
    assert.equal(existsSync(socketPath), true)
    assert.equal(existsSync(tokenPath), true)
    const token = readFileSync(tokenPath, 'utf8').trim()
    assert.equal((await rpc(socketPath, 'wrong', 'GET', '/health')).status, 401)
    assert.deepEqual((await rpc(socketPath, token, 'GET', '/health')).body.status, 'up')

    FakeWindow.delayMs = 25
    const accepted = await rpc(socketPath, token, 'POST', '/measure', { providers: ['zai', 'grok', 'claude', 'codex'] })
    assert.equal(accepted.status, 202)
    assert.equal(accepted.body.accepted, true)
    const busy = await rpc(socketPath, token, 'POST', '/measure', { providers: ['zai'] })
    assert.equal(busy.status, 409)
    assert.equal(busy.body.reason, 'busy')
    const result = await waitForComplete(socketPath, token, accepted.body.run_id)

    assert.equal(result.status, 'complete')
    assert.equal(result.providers.zai.status, 'ok')
    assert.equal(result.providers.grok.status, 'ok')
    assert.match(result.providers.grok.dom_appendix, /Konto-Handle/)
    assert.equal(result.providers.claude.status, 'ok')
    assert.equal(result.providers.codex.status, 'ok')
    assert.equal(FakeWindow.lastOptions?.show, true)
    assert.equal(FakeWindow.lastOptions?.x, -4096)
    assert.equal(FakeWindow.lastOptions?.opacity, 0)
    assert.equal(FakeWindow.lastOptions?.hasShadow, false)
    assert.equal(FakeWindow.last?.ignoreMouseEvents, true)
    assert.equal(result.providers.zai.quote.includes('21'), true)

    const healthAfterComplete = await rpc(socketPath, token, 'GET', '/health')
    assert.equal(healthAfterComplete.body.busy, false)

    FakeWindow.missingEvidence = true
    const missingEvidenceRun = await rpc(socketPath, token, 'POST', '/measure', { providers: ['zai'] })
    assert.equal(missingEvidenceRun.status, 202)
    const missingEvidenceResult = await waitForComplete(socketPath, token, missingEvidenceRun.body.run_id)

    assert.equal(missingEvidenceResult.providers.zai.status, 'structure_unknown')
  } finally {
    FakeWindow.delayMs = 0
    FakeWindow.missingEvidence = false
    await service.stop()
    assert.equal(existsSync(join(root, 'budget-measure.token')), false)
    await rm(root, { recursive: true, force: true })
  }
})

test('grok: second card Weekly Grok Bot Limit lands in grok_dom.bot and its own appendix, SuperGrok unchanged', async () => {
  const root = await mkdtemp(join(tmpdir(), 'hermes-budget-grok-bot-'))
  const session = new FakeSession()
  FakeWindow.grokBot = {
    aria: '12%',
    text: 'Weekly Grok Bot Limit 12% gebraucht Setzt 17. September 2026 um 19:45 zurück',
    cardHead: 'Weekly Grok Bot Limit'
  }

  const service = new BudgetMeasureService({
    appDataPath: root,
    BrowserWindow: FakeWindow as any,
    hashSettleMs: 0,
    grokBotGraceMs: 0,
    fromPartition: () => session,
    now: () => new Date('2026-09-16T15:00:00.000Z')
  })

  try {
    await service.start()
    const { socketPath, tokenPath } = service.getPaths()
    const token = readFileSync(tokenPath, 'utf8').trim()
    const accepted = await rpc(socketPath, token, 'POST', '/measure', { providers: ['grok'] })
    assert.equal(accepted.status, 202)
    const result = await waitForComplete(socketPath, token, accepted.body.run_id)
    const grok = result.providers.grok

    assert.equal(grok.status, 'ok')
    // SuperGrok-Karte bleibt die Hauptmessung, Werte wie ohne zweite Karte.
    assert.equal(grok.grok_dom.aria, '53%')
    assert.equal(grok.grok_dom.cardHead, 'Wöchentliches SuperGrok Heavy-Limit')
    assert.deepEqual(grok.grok_dom.breakdown, [{ label: 'Grok Build', pct: '53' }])
    // Bot-Karte als eigener Block mit eigenem Prozentwert.
    assert.equal(grok.grok_dom.bot.aria, '12%')
    assert.equal(grok.grok_dom.bot.cardHead, 'Weekly Grok Bot Limit')
    assert.match(grok.grok_dom.bot.text, /19:45/)
    // Anhang mit beiden Karten getrennt; der Bot-Prozentwert steht als Token im Anhang.
    assert.match(grok.dom_appendix, /\[Nutzungs-Karte: Wöchentliches SuperGrok Heavy-Limit \| Grok Build 53% \| aria-label: 53%\]/)
    assert.match(grok.dom_appendix, /\[Nutzungs-Karte Grok Bot: Weekly Grok Bot Limit \| .*19:45.* \| aria-label: 12%\]/)
    assert.match(grok.dom_appendix, /Konto-Handle: @tobi/)
    assert.equal(grok.evidence.dom_appendix, grok.dom_appendix)
  } finally {
    FakeWindow.grokBot = null
    await service.stop()
    await rm(root, { recursive: true, force: true })
  }
})

test('grok: without a Grok Bot card grok_dom.bot is null and no bot appendix is written', async () => {
  const root = await mkdtemp(join(tmpdir(), 'hermes-budget-grok-nobot-'))
  const session = new FakeSession()
  FakeWindow.grokBot = null

  const service = new BudgetMeasureService({
    appDataPath: root,
    BrowserWindow: FakeWindow as any,
    hashSettleMs: 0,
    grokBotGraceMs: 50,
    fromPartition: () => session,
    now: () => new Date('2026-09-16T15:00:00.000Z')
  })

  try {
    await service.start()
    const { socketPath, tokenPath } = service.getPaths()
    const token = readFileSync(tokenPath, 'utf8').trim()
    const accepted = await rpc(socketPath, token, 'POST', '/measure', { providers: ['grok'] })
    const result = await waitForComplete(socketPath, token, accepted.body.run_id)
    const grok = result.providers.grok

    assert.equal(grok.status, 'ok')
    assert.equal(grok.grok_dom.bot, null)
    assert.doesNotMatch(grok.dom_appendix, /Nutzungs-Karte Grok Bot/)
  } finally {
    await service.stop()
    await rm(root, { recursive: true, force: true })
  }
})

test('opens Claude usage via hashchange, not static #settings/usage', async () => {
  const root = await mkdtemp(join(tmpdir(), 'hermes-budget-claude-hash-'))
  const session = new FakeSession()
  FakeWindow.ignoreStaticClaudeHash = true

  const service = new BudgetMeasureService({
    appDataPath: root,
    BrowserWindow: FakeWindow as any,
    hashSettleMs: 0,
    grokBotGraceMs: 0,
    fromPartition: () => session,
    now: () => new Date('2026-08-25T08:08:00.000Z')
  })

  try {
    await service.start()
    const { socketPath, tokenPath } = service.getPaths()
    const token = readFileSync(tokenPath, 'utf8').trim()
    const accepted = await rpc(socketPath, token, 'POST', '/measure', { providers: ['claude'] })
    assert.equal(accepted.status, 202)
    const result = await waitForComplete(socketPath, token, accepted.body.run_id)
    assert.equal(result.status, 'complete')
    assert.equal(result.providers.claude.status, 'ok')
    assert.match(String(result.providers.claude.quote || ''), /31/)
    assert.equal(FakeWindow.last?.hashOpenedViaJs, true)
  } finally {
    FakeWindow.ignoreStaticClaudeHash = false
    await service.stop()
    await rm(root, { recursive: true, force: true })
  }
})

test('accepts Claude Diese-Woche usage card without Alle Modelle', async () => {
  const root = await mkdtemp(join(tmpdir(), 'hermes-budget-claude-week-'))
  const session = new FakeSession()
  FakeWindow.ignoreStaticClaudeHash = true
  FakeWindow.claudeUsageText = [
    'Dein Verbrauch Max (20x)',
    'Neue Woche. 5 % deines wöchentlichen Limits verwendet.',
    'Aktuelle Sitzung 3 % verwendet',
    'Diese Woche Zurücksetzung Samstag, 15:00 5 % verwendet',
  ].join('\n')

  const service = new BudgetMeasureService({
    appDataPath: root,
    BrowserWindow: FakeWindow as any,
    hashSettleMs: 0,
    grokBotGraceMs: 0,
    fromPartition: () => session,
    now: () => new Date('2026-09-12T14:54:00.000Z')
  })

  try {
    await service.start()
    const { socketPath, tokenPath } = service.getPaths()
    const token = readFileSync(tokenPath, 'utf8').trim()
    const accepted = await rpc(socketPath, token, 'POST', '/measure', { providers: ['claude'] })
    assert.equal(accepted.status, 202)
    const result = await waitForComplete(socketPath, token, accepted.body.run_id)
    assert.equal(result.status, 'complete')
    assert.equal(result.providers.claude.status, 'ok')
    assert.match(String(result.providers.claude.quote || ''), /Diese Woche/)
  } finally {
    FakeWindow.ignoreStaticClaudeHash = false
    FakeWindow.claudeUsageText = ''
    await service.stop()
    await rm(root, { recursive: true, force: true })
  }
})

test('busy is released before park so the next measure is accepted', async () => {
  const root = await mkdtemp(join(tmpdir(), 'hermes-budget-park-'))
  const session = new FakeSession()

  const service = new BudgetMeasureService({
    appDataPath: root,
    BrowserWindow: FakeWindow as any,
    hashSettleMs: 0,
    grokBotGraceMs: 0,
    fromPartition: () => session,
    now: () => new Date('2026-08-25T08:08:00.000Z')
  })

  try {
    await service.start()
    const { socketPath, tokenPath } = service.getPaths()
    const token = readFileSync(tokenPath, 'utf8').trim()
    FakeWindow.delayMs = 40
    const first = await rpc(socketPath, token, 'POST', '/measure', { providers: ['zai'] })
    const firstResult = await waitForComplete(socketPath, token, first.body.run_id)
    assert.equal(firstResult.status, 'complete')
    const second = await rpc(socketPath, token, 'POST', '/measure', { providers: ['zai'] })
    assert.equal(second.status, 202)
    assert.equal(second.body.accepted, true)
    await waitForComplete(socketPath, token, second.body.run_id)

    for (let i = 0; i < 20; i += 1) {
      if (FakeWindow.last?.currentUrl === 'about:blank') {break}
      await new Promise(resolve => setTimeout(resolve, 10))
    }

    assert.equal(FakeWindow.last?.currentUrl, 'about:blank')

    // 13.10.2026: a parked window came back on screen as an empty "Grok" frame.
    // After the run the window is gone, not parked.
    for (let i = 0; i < 20; i += 1) {
      if (FakeWindow.last?.destroyed) {break}
      await new Promise(resolve => setTimeout(resolve, 10))
    }

    assert.equal(FakeWindow.last?.destroyed, true)
    assert.equal(FakeWindow.last?.hiddenInMissionControl, true)
  } finally {
    FakeWindow.delayMs = 0
    await service.stop()
    await rm(root, { recursive: true, force: true })
  }
})

test('stale socket without a live server is replaced and a live socket is a start error', async () => {
  const root = await mkdtemp(join(tmpdir(), 'hermes-budget-stale-'))
  const session = new FakeSession()
  writeFileSync(join(root, 'budget-measure.sock'), 'stale')

  const first = new BudgetMeasureService({
    appDataPath: root,
    BrowserWindow: FakeWindow as any,
    hashSettleMs: 0,
    grokBotGraceMs: 0,
    fromPartition: () => session,
    now: () => new Date('2026-08-25T08:08:00.000Z')
  })

  const second = new BudgetMeasureService({
    appDataPath: root,
    BrowserWindow: FakeWindow as any,
    hashSettleMs: 0,
    grokBotGraceMs: 0,
    fromPartition: () => session,
    now: () => new Date('2026-08-25T08:08:00.000Z')
  })

  try {
    await first.start()
    await assert.rejects(() => second.start(), /budget_measure_socket_in_use/)
  } finally {
    await first.stop()
    await rm(root, { recursive: true, force: true })
  }
})

test('renderer crash handler is removed on teardown', async () => {
  const root = await mkdtemp(join(tmpdir(), 'hermes-budget-teardown-'))
  const session = new FakeSession()

  const service = new BudgetMeasureService({
    appDataPath: root,
    BrowserWindow: FakeWindow as any,
    hashSettleMs: 0,
    grokBotGraceMs: 0,
    fromPartition: () => session,
    now: () => new Date('2026-08-25T08:08:00.000Z')
  })

  try {
    await service.start()
    const { socketPath, tokenPath } = service.getPaths()
    const token = readFileSync(tokenPath, 'utf8').trim()
    const accepted = await rpc(socketPath, token, 'POST', '/measure', { providers: ['zai'] })
    await waitForComplete(socketPath, token, accepted.body.run_id)
    const window = FakeWindow.last
    await service.stop()
    assert.equal(window?.removedEvents.includes('render-process-gone'), true)
  } finally {
    await rm(root, { recursive: true, force: true })
  }
})

test('socket chmod failure is fail-closed', async () => {
  const root = await mkdtemp(join(tmpdir(), 'hermes-budget-chmod-'))
  const session = new FakeSession()

  const service = new BudgetMeasureService({
    appDataPath: root,
    BrowserWindow: FakeWindow as any,
    hashSettleMs: 0,
    grokBotGraceMs: 0,
    fromPartition: () => session,
    now: () => new Date('2026-08-25T08:08:00.000Z'),
    chmodSocket: () => {
      throw new Error('chmod_denied')
    }
  })

  try {
    await assert.rejects(() => service.start(), /chmod_denied/)
    assert.equal(existsSync(join(root, 'budget-measure.sock')), false)
    assert.equal(existsSync(join(root, 'budget-measure.token')), false)
  } finally {
    await service.stop()
    await rm(root, { recursive: true, force: true })
  }
})

test('grok: a late-rendering Grok Bot card is waited for and captured (28.09.2026)', async () => {
  const root = await mkdtemp(join(tmpdir(), 'hermes-budget-grok-latebot-'))
  const session = new FakeSession()
  FakeWindow.grokBot = {
    aria: '15%',
    text: '15% gebraucht Setzt 1. Oktober 2026 um 19:45 zurück',
    cardHead: 'Weekly Grok Bot Limit'
  }
  FakeWindow.grokReads = 0
  FakeWindow.grokBotAfterReads = 3

  const service = new BudgetMeasureService({
    appDataPath: root,
    BrowserWindow: FakeWindow as any,
    hashSettleMs: 0,
    grokBotGraceMs: 5_000,
    fromPartition: () => session,
    now: () => new Date('2026-09-28T08:00:00.000Z')
  })

  try {
    await service.start()
    const { socketPath, tokenPath } = service.getPaths()
    const token = readFileSync(tokenPath, 'utf8').trim()
    const accepted = await rpc(socketPath, token, 'POST', '/measure', { providers: ['grok'] })
    // 2_000 Versuche zu 5 ms waren effektiv ~10 s Wartezeit; als explizite
    // Millisekunden-Deadline uebersetzt, damit die Einheit eindeutig bleibt.
    const result = await waitForComplete(socketPath, token, accepted.body.run_id, 10_000)
    const grok = result.providers.grok

    assert.equal(grok.status, 'ok')
    assert.ok(FakeWindow.grokReads > 3, `nur ${FakeWindow.grokReads} Lesevorgänge`)
    assert.equal(grok.grok_dom.bot.aria, '15%')
    assert.match(grok.dom_appendix, /\[Nutzungs-Karte Grok Bot: Weekly Grok Bot Limit/)
  } finally {
    FakeWindow.grokBot = null
    FakeWindow.grokBotAfterReads = 0
    FakeWindow.grokReads = 0
    await service.stop()
    await rm(root, { recursive: true, force: true })
  }
})
