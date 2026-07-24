import {
  createDefaultHysteria2InboundDraft,
  createDefaultSingBoxCoreDraft,
  generateSingBoxCoreConfigJsonFromDraft,
  validateSingBoxCoreConfig,
  validateSingBoxCoreDraft,
} from '@pasarguard/singbox-config-kit'
import type { HysteriaInboundDraft, SingBoxCoreConfig, SingBoxCoreDraft, SingBoxValidationIssue } from '@pasarguard/singbox-config-kit'
import { validateCoreConfig } from '@pasarguard/core-kit'

/** Same certMode inference used by the Xray inbound TLS certificate toggle: content wins if either field/value is present. */
function stringOrLines(value: unknown): string {
  if (Array.isArray(value)) return value.map(v => String(v)).join('\n')
  if (typeof value === 'string') return value
  return ''
}

function hysteria2InboundToDraft(raw: unknown): HysteriaInboundDraft {
  const inbound = raw && typeof raw === 'object' ? (raw as Record<string, unknown>) : {}
  const tls = inbound.tls && typeof inbound.tls === 'object' ? (inbound.tls as Record<string, unknown>) : {}
  const obfs = inbound.obfs && typeof inbound.obfs === 'object' ? (inbound.obfs as Record<string, unknown>) : null

  const certificate = stringOrLines(tls.certificate)
  const key = stringOrLines(tls.key)
  const hasCertificateField = Object.prototype.hasOwnProperty.call(tls, 'certificate')
  const hasKeyField = Object.prototype.hasOwnProperty.call(tls, 'key')

  return {
    tag: typeof inbound.tag === 'string' ? inbound.tag : '',
    listen: typeof inbound.listen === 'string' && inbound.listen ? inbound.listen : '::',
    listenPort: typeof inbound.listen_port === 'number' ? inbound.listen_port : '',
    upMbps: typeof inbound.up_mbps === 'number' ? String(inbound.up_mbps) : '',
    downMbps: typeof inbound.down_mbps === 'number' ? String(inbound.down_mbps) : '',
    ignoreClientBandwidth: inbound.ignore_client_bandwidth === true,
    obfsEnabled: obfs !== null,
    obfsPassword: obfs && typeof obfs.password === 'string' ? obfs.password : '',
    masquerade: typeof inbound.masquerade === 'string' ? inbound.masquerade : '',
    tlsServerName: typeof tls.server_name === 'string' ? tls.server_name : '',
    tlsAlpn: Array.isArray(tls.alpn) ? tls.alpn.map(v => String(v)) : [],
    certMode: hasCertificateField || hasKeyField || certificate || key ? 'content' : 'path',
    certificateFile: typeof tls.certificate_path === 'string' ? tls.certificate_path : '',
    keyFile: typeof tls.key_path === 'string' ? tls.key_path : '',
    certificate,
    key,
  }
}

function singBoxConfigToDraftFromValid(c: SingBoxCoreConfig): SingBoxCoreDraft {
  const logLevel = c.log && typeof c.log.level === 'string' ? c.log.level : 'info'
  return {
    logLevel,
    inbounds: c.inbounds.map(hysteria2InboundToDraft),
  }
}

export function singBoxConfigToDraft(raw: unknown): { ok: true; draft: SingBoxCoreDraft } | { ok: false; message: string } {
  const result = validateSingBoxCoreConfig(raw)
  if (!result.ok) {
    const first = result.issues[0]
    return { ok: false, message: first ? `${first.path}: ${first.message}` : 'Invalid sing-box config' }
  }
  return { ok: true, draft: singBoxConfigToDraftFromValid(result.config) }
}

export function createNewSingBoxDraft(): SingBoxCoreDraft {
  return createDefaultSingBoxCoreDraft()
}

/** For the "Add inbound" button: guarantees a fresh default tag unique against the current draft. */
export function createNewHysteria2InboundDraft(draft: SingBoxCoreDraft): HysteriaInboundDraft {
  return createDefaultHysteria2InboundDraft(draft.inbounds.map(i => i.tag))
}

export function draftToPersistedConfig(draft: SingBoxCoreDraft): Record<string, unknown> {
  const json = generateSingBoxCoreConfigJsonFromDraft(draft)
  return JSON.parse(json) as Record<string, unknown>
}

function draftGenerationIssue(error: unknown): SingBoxValidationIssue {
  const rawMessage = error instanceof Error ? error.message : String(error)
  const match = rawMessage.match(/^(\/[^:]*):\s*(.+)$/)
  return {
    path: match?.[1] ?? '/',
    code: 'SB_FORM_CONFIG_GENERATION_INVALID',
    message: match?.[2] ?? rawMessage,
  }
}

/** Draft issues + core-kit validation in one step for saves. */
export function getSingBoxPersistConfig(draft: SingBoxCoreDraft) {
  const issues = validateSingBoxCoreDraft(draft)
  if (issues.length > 0) {
    return { ok: false as const, draftIssues: issues }
  }
  let config: Record<string, unknown>
  try {
    config = draftToPersistedConfig(draft)
  } catch (error) {
    return { ok: false as const, draftIssues: [draftGenerationIssue(error)] }
  }
  const r = validateCoreConfig('singbox', config)
  if (!r.ok) {
    return { ok: false as const, kitIssues: r.issues }
  }
  return { ok: true as const, config: r.config as Record<string, unknown> }
}
