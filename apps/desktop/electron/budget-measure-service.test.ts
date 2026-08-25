import assert from 'node:assert/strict'
import { EventEmitter } from 'node:events'
import { existsSync, readFileSync } from 'node:fs'
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
  currentUrl = 'about:blank'
  destroyed = false
  session: FakeSession
  webContents: any

  constructor(options: any) {
    super()
    this.session = options.webPreferences.session
    this.webContents = {
      session: this.session,
      setAudioMuted: () => {},
      setWindowOpenHandler: () => {},
      on: () => {},
      removeListener: () => {},
      stop: () => {},
      executeJavaScript: async (code: string) => {
        if (code.includes('url: location.href')) {
          const states: Record<string, string> = {
            'https://z.ai/manage-apikey/coding-plan/personal/usage': 'Usage Weekly Quota 21 % Used Reset Time: 2026-08-30 12:46',
            'https://grok.com/?_s=usage': 'Nutzung Wöchentliches SuperGrok Heavy-Limit gebraucht Grok Build 53% Setzt 27. August 2026 um 18:04 zurück',
            'https://claude.ai/new#settings/usage': 'Nutzung Alle Modelle Zurücksetzung Sa., 14:59 31 % verwendet',
            'https://chatgpt.com/codex/cloud/settings/analytics#usage': 'Weekly usage limit 91% remaining Resets Aug 31, 2026 9:02 AM'
          }

          return { url: this.currentUrl, title: 'provider', text: states[this.currentUrl] || '' }
        }

        if (FakeWindow.missingEvidence) {return {}}

        if (code.includes('accountAvatar')) {return { accountAvatar: 'T' }}

        if (code.includes('accountMarker')) {return { accountMarker: 'Tobi' }}

        if (code.includes('planBadge')) {return { planBadge: 'PRO' }}

        if (code.includes('handle')) {
          return { handle: '@tobi', aria: '53%', breakdown: [{ label: 'Grok Build', pct: '53' }], text: 'Grok Build 53%', cardHead: 'Wöchentliches SuperGrok Heavy-Limit' }
        }

        return {}
      }
    }
  }

  loadURL(url: string) {
    this.currentUrl = url

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
    const runId = accepted.body.run_id
    let result: any = null

    for (let i = 0; i < 20; i += 1) {
      const polled = await rpc(socketPath, token, 'GET', `/result?run_id=${encodeURIComponent(runId)}`)

      if (polled.body.status === 'complete') {
        result = polled.body.result

        break
      }

      await new Promise(resolve => setTimeout(resolve, 5))
    }

    assert.equal(result.status, 'complete')
    assert.equal(result.providers.zai.status, 'ok')
    assert.equal(result.providers.grok.status, 'ok')
    assert.match(result.providers.grok.dom_appendix, /Konto-Handle/)
    assert.equal(result.providers.claude.status, 'ok')
    assert.equal(result.providers.codex.status, 'ok')
    assert.equal(result.providers.zai.quote.includes('21'), true)

    FakeWindow.missingEvidence = true
    const missingEvidenceRun = await rpc(socketPath, token, 'POST', '/measure', { providers: ['zai'] })
    assert.equal(missingEvidenceRun.status, 202)
    let missingEvidenceResult: any = null

    for (let i = 0; i < 20; i += 1) {
      const polled = await rpc(socketPath, token, 'GET', `/result?run_id=${encodeURIComponent(missingEvidenceRun.body.run_id)}`)

      if (polled.body.status === 'complete') {
        missingEvidenceResult = polled.body.result
        break
      }

      await new Promise(resolve => setTimeout(resolve, 5))
    }

    assert.equal(missingEvidenceResult.providers.zai.status, 'structure_unknown')
  } finally {
    FakeWindow.delayMs = 0
    FakeWindow.missingEvidence = false
    await service.stop()
    assert.equal(existsSync(join(root, 'budget-measure.token')), false)
    await rm(root, { recursive: true, force: true })
  }
})
