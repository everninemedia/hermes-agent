/** Ink's gateway client: `@hermes/shared/gateway-client` wired to the `hermes --tui` launcher (its
 *  bootstrap, `HERMES_TUI_*` overrides, launch tool-progress pin, i18n and crash-log breadcrumbs). */
import { type ConnectGateway, GatewayClient as SharedGatewayClient } from '@hermes/shared/gateway-client'
import { WebSocket as UndiciWebSocket } from 'undici'

import { t } from './i18n/runtime.js'
import { launchToolProgress } from './launchOptions.js'
import { recordParentLifecycle } from './lib/parentLog.js'
import { bootstrapLocalGateway, isTerminalBootstrapFailure } from './localGatewayBootstrap.js'

const STARTUP_TIMEOUT_MS = Math.max(5000, parseInt(process.env.HERMES_TUI_STARTUP_TIMEOUT_MS ?? '15000', 10) || 15000)
const REQUEST_TIMEOUT_MS = Math.max(30000, parseInt(process.env.HERMES_TUI_RPC_TIMEOUT_MS ?? '120000', 10) || 120000)
/** Manual compression can outlast the RPC default; matches the desktop's SESSION_COMPRESS_TIMEOUT_MS,
 *  above the compute host's 630s wait cap (tui_gateway/compute_host_bridge.py). */
const SESSION_COMPRESS_TIMEOUT_MS = 660_000

const envUrl = (name: 'HERMES_TUI_GATEWAY_URL' | 'HERMES_TUI_SIDECAR_URL') => () => process.env[name]?.trim() || null

export class GatewayClient extends SharedGatewayClient {
  constructor(bootstrap: ConnectGateway = bootstrapLocalGateway) {
    super({
      attachUrl: envUrl('HERMES_TUI_GATEWAY_URL'),
      connect: bootstrap,
      fallbackWebSocket: UndiciWebSocket as unknown as typeof WebSocket,
      terminalConnectFailure: error =>
        isTerminalBootstrapFailure(error.message) ? error.message.match(/^gateway unavailable: .*$/m)?.[0] : undefined,
      onLifecycle: recordParentLifecycle,
      requestTimeoutMs: REQUEST_TIMEOUT_MS,
      sidecarUrl: envUrl('HERMES_TUI_SIDECAR_URL'),
      startupTimeoutMs: STARTUP_TIMEOUT_MS,
      text: t,
      toolProgress: () => launchToolProgress()
    })
  }

  override request<T = unknown>(method: string, params: Record<string, unknown> = {}, timeoutMs?: number): Promise<T> {
    return super.request<T>(
      method,
      params,
      timeoutMs ?? (method === 'session.compress' ? SESSION_COMPRESS_TIMEOUT_MS : undefined)
    )
  }
}
