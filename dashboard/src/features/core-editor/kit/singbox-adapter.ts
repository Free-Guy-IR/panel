import {
  createDefaultHysteria2InboundDraft,
  createDefaultInboundDraft,
  createDefaultSingBoxCoreDraft,
  generateSingBoxCoreConfigJsonFromDraft,
  validateSingBoxCoreConfig,
  validateSingBoxCoreDraft,
} from '@pasarguard/singbox-config-kit'
import type {
  HysteriaInboundDraft,
  ShadowsocksInboundDraft,
  SingBoxCoreConfig,
  SingBoxCoreDraft,
  SingBoxInboundDraft,
  SingBoxProtocol,
  SingBoxValidationIssue,
  TlsDraftFields,
  TransportDraftFields,
  TuicInboundDraft,
} from '@pasarguard/singbox-config-kit'
import { validateCoreConfig } from '@pasarguard/core-kit'

/** Same certMode inference used by the Xray inbound TLS certificate toggle: content wins if either field/value is present. */
function stringOrLines(value: unknown): string {
  if (Array.isArray(value)) return value.map(v => String(v)).join('\n')
  if (typeof value === 'string') return value
  return ''
}

function asRecord(value: unknown): Record<string, unknown> | null {
  return value && typeof value === 'object' && !Array.isArray(value) ? (value as Record<string, unknown>) : null
}

function strArray(value: unknown): string[] {
  return Array.isArray(value) ? value.map(v => String(v)) : []
}

function str(value: unknown): string {
  return typeof value === 'string' ? value : ''
}

/** Reads a sing-box tls object into the shared TLS draft fields (used by vless/vmess/trojan/tuic). */
function tlsToDraftFields(rawTls: unknown): TlsDraftFields {
  const tls = asRecord(rawTls) ?? {}
  const reality = asRecord(tls.reality)
  const handshake = reality ? asRecord(reality.handshake) : null
  const utls = asRecord(tls.utls)
  const ech = asRecord(tls.ech)
  const acme = asRecord(tls.acme)
  const dns01 = acme ? asRecord(acme.dns01_challenge) : null

  const certificate = stringOrLines(tls.certificate)
  const key = stringOrLines(tls.key)
  const hasCert = Object.prototype.hasOwnProperty.call(tls, 'certificate') || Object.prototype.hasOwnProperty.call(tls, 'key') || !!certificate || !!key

  return {
    tlsEnabled: tls.enabled === true,
    tlsServerName: str(tls.server_name),
    tlsAlpn: strArray(tls.alpn),
    tlsMinVersion: str(tls.min_version),
    tlsMaxVersion: str(tls.max_version),
    tlsCipherSuites: strArray(tls.cipher_suites),
    certMode: hasCert ? 'content' : 'path',
    certificateFile: str(tls.certificate_path),
    keyFile: str(tls.key_path),
    certificate,
    key,
    echEnabled: !!ech && ech.enabled === true,
    echKey: ech ? stringOrLines(ech.key) : '',
    echPqSignatureSchemesEnabled: !!ech && ech.pq_signature_schemes_enabled === true,
    echDynamicRecordSizingDisabled: !!ech && ech.dynamic_record_sizing_disabled === true,
    acmeEnabled: acme !== null,
    acmeDomain: acme ? strArray(acme.domain) : [],
    acmeEmail: acme ? str(acme.email) : '',
    acmeProvider: acme ? str(acme.provider) : '',
    acmeDns01Provider: dns01 ? str(dns01.provider) : '',
    acmeDns01ApiToken: dns01 ? str(dns01.api_token) : '',
    acmeDns01AccessKeyId: dns01 ? str(dns01.access_key_id) : '',
    acmeDns01AccessKeySecret: dns01 ? str(dns01.access_key_secret) : '',
    utlsEnabled: !!utls && utls.enabled === true,
    utlsFingerprint: utls ? str(utls.fingerprint) : '',
    realityEnabled: !!reality && reality.enabled === true,
    realityHandshakeServer: handshake ? str(handshake.server) : '',
    realityHandshakePort: handshake && typeof handshake.server_port === 'number' ? String(handshake.server_port) : '',
    realityPrivateKey: reality ? str(reality.private_key) : '',
    realityShortId: reality ? strArray(reality.short_id) : [],
    realityMaxTimeDifference: reality ? str(reality.max_time_difference) : '',
  }
}

/** Reads a sing-box transport object into the shared transport draft fields. */
function transportToDraftFields(rawTransport: unknown): TransportDraftFields {
  const tr = asRecord(rawTransport)
  if (!tr || typeof tr.type !== 'string') {
    return { transportType: '', transportPath: '', transportHost: '', transportServiceName: '', transportMethod: '' }
  }
  const type = tr.type as TransportDraftFields['transportType']
  const headers = asRecord(tr.headers)
  const host = headers ? str(headers.Host ?? headers.host) : Array.isArray(tr.host) ? String(tr.host[0] ?? '') : str(tr.host)
  return {
    transportType: type === 'ws' || type === 'grpc' || type === 'http' || type === 'httpupgrade' ? type : '',
    transportPath: str(tr.path),
    transportHost: host,
    transportServiceName: str(tr.service_name),
    transportMethod: str(tr.method),
  }
}

function baseFields(raw: Record<string, unknown>) {
  return {
    tag: str(raw.tag),
    listen: str(raw.listen) || '::',
    listenPort: typeof raw.listen_port === 'number' ? raw.listen_port : ('' as number | string),
  }
}

function hysteria2InboundToDraft(raw: unknown): HysteriaInboundDraft {
  const inbound = asRecord(raw) ?? {}
  const tls = asRecord(inbound.tls) ?? {}
  const obfs = asRecord(inbound.obfs)
  const ech = asRecord(tls.ech)
  const acme = asRecord(tls.acme)
  const masq = asRecord(inbound.masquerade)
  const dns01 = acme ? asRecord(acme.dns01_challenge) : null

  const certificate = stringOrLines(tls.certificate)
  const key = stringOrLines(tls.key)
  const hasCert = Object.prototype.hasOwnProperty.call(tls, 'certificate') || Object.prototype.hasOwnProperty.call(tls, 'key') || !!certificate || !!key
  const masqType = masq && typeof masq.type === 'string' ? (masq.type as string) : ''
  const masqUrl = masq && typeof masq.url === 'string' ? (masq.url as string) : ''

  return {
    protocol: 'hysteria2',
    ...baseFields(inbound),
    upMbps: typeof inbound.up_mbps === 'number' ? String(inbound.up_mbps) : '',
    downMbps: typeof inbound.down_mbps === 'number' ? String(inbound.down_mbps) : '',
    ignoreClientBandwidth: inbound.ignore_client_bandwidth === true,
    udpTimeout: typeof inbound.udp_timeout === 'string' || typeof inbound.udp_timeout === 'number' ? String(inbound.udp_timeout) : '',
    udpFragment: inbound.udp_fragment === true,
    brutalDebug: inbound.brutal_debug === true,
    portHoppingRange: typeof inbound.port_hopping_range === 'string' ? inbound.port_hopping_range : '',
    obfsEnabled: obfs !== null,
    obfsPassword: obfs && typeof obfs.password === 'string' ? obfs.password : '',
    masquerade: typeof inbound.masquerade === 'string' ? inbound.masquerade : masqUrl,
    masqueradeType: masqType === 'file' || masqType === 'proxy' || masqType === 'string' ? masqType : '',
    masqueradeDirectory: masq && typeof masq.directory === 'string' ? masq.directory : '',
    masqueradeRewriteHost: !!(masq && masq.rewrite_host === true),
    masqueradeStatusCode: masq && typeof masq.status_code === 'number' ? String(masq.status_code) : '',
    masqueradeHeaders: masq && asRecord(masq.headers) ? JSON.stringify(masq.headers) : '',
    masqueradeContent: masq && typeof masq.content === 'string' ? masq.content : '',
    tlsServerName: str(tls.server_name),
    tlsAlpn: strArray(tls.alpn),
    tlsMinVersion: str(tls.min_version),
    tlsMaxVersion: str(tls.max_version),
    tlsCipherSuites: strArray(tls.cipher_suites),
    certMode: hasCert ? 'content' : 'path',
    certificateFile: str(tls.certificate_path),
    keyFile: str(tls.key_path),
    certificate,
    key,
    echEnabled: !!ech && ech.enabled === true,
    echKey: ech ? stringOrLines(ech.key) : '',
    echPqSignatureSchemesEnabled: !!ech && ech.pq_signature_schemes_enabled === true,
    echDynamicRecordSizingDisabled: !!ech && ech.dynamic_record_sizing_disabled === true,
    acmeEnabled: acme !== null,
    acmeDomain: acme ? strArray(acme.domain) : [],
    acmeEmail: acme && typeof acme.email === 'string' ? acme.email : '',
    acmeProvider: acme && typeof acme.provider === 'string' ? acme.provider : '',
    acmeDns01Provider: dns01 && typeof dns01.provider === 'string' ? dns01.provider : '',
    acmeDns01ApiToken: dns01 && typeof dns01.api_token === 'string' ? dns01.api_token : '',
    acmeDns01AccessKeyId: dns01 && typeof dns01.access_key_id === 'string' ? dns01.access_key_id : '',
    acmeDns01AccessKeySecret: dns01 && typeof dns01.access_key_secret === 'string' ? dns01.access_key_secret : '',
  }
}

function streamInboundToDraft(raw: Record<string, unknown>, protocol: 'vless' | 'vmess' | 'trojan'): SingBoxInboundDraft {
  return {
    protocol,
    ...baseFields(raw),
    ...tlsToDraftFields(raw.tls),
    ...transportToDraftFields(raw.transport),
  } as SingBoxInboundDraft
}

function shadowsocksInboundToDraft(raw: Record<string, unknown>): ShadowsocksInboundDraft {
  return {
    protocol: 'shadowsocks',
    ...baseFields(raw),
    method: str(raw.method) || '2022-blake3-aes-128-gcm',
    password: str(raw.password),
  }
}

function tuicInboundToDraft(raw: Record<string, unknown>): TuicInboundDraft {
  return {
    protocol: 'tuic',
    ...baseFields(raw),
    ...tlsToDraftFields(raw.tls),
    congestionControl: str(raw.congestion_control),
  }
}

function inboundToDraft(raw: unknown): SingBoxInboundDraft {
  const inbound = asRecord(raw) ?? {}
  switch (inbound.type) {
    case 'vless':
    case 'vmess':
    case 'trojan':
      return streamInboundToDraft(inbound, inbound.type)
    case 'shadowsocks':
      return shadowsocksInboundToDraft(inbound)
    case 'tuic':
      return tuicInboundToDraft(inbound)
    case 'hysteria2':
    default:
      return hysteria2InboundToDraft(inbound)
  }
}

function singBoxConfigToDraftFromValid(c: SingBoxCoreConfig): SingBoxCoreDraft {
  const logLevel = c.log && typeof c.log.level === 'string' ? c.log.level : 'info'
  return {
    logLevel,
    inbounds: c.inbounds.map(inboundToDraft),
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

/** Generic "Add inbound" for any protocol, unique against the current draft's tags. */
export function createNewInboundDraft(draft: SingBoxCoreDraft, protocol: SingBoxProtocol): SingBoxInboundDraft {
  return createDefaultInboundDraft(protocol, draft.inbounds.map(i => i.tag))
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
