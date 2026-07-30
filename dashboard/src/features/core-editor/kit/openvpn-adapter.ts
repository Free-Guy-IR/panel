import {
  createDefaultOpenVPNCoreDraft,
  createDefaultOpenVPNInstanceDraft,
  generateOpenVPNCoreConfigJsonFromDraft,
  validateOpenVPNCoreConfig,
  validateOpenVPNCoreDraft,
} from '@pasarguard/openvpn-config-kit'
import type { OpenVPNCoreConfig, OpenVPNCoreDraft, OpenVPNInstanceDraft, OpenVPNValidationIssue } from '@pasarguard/openvpn-config-kit'
import { validateCoreConfig } from '@pasarguard/core-kit'

function instanceToDraft(raw: unknown): OpenVPNInstanceDraft {
  const instance = raw && typeof raw === 'object' ? (raw as Record<string, unknown>) : {}
  return {
    tag: typeof instance.tag === 'string' ? instance.tag : '',
    protocol: instance.protocol === 'tcp' ? 'tcp' : 'udp',
    port: typeof instance.port === 'number' ? instance.port : '',
    network: typeof instance.network === 'string' ? instance.network : '',
    cipher: typeof instance.cipher === 'string' ? instance.cipher : 'AES-256-GCM',
    auth: typeof instance.auth === 'string' ? instance.auth : 'SHA256',
    keepalive: typeof instance.keepalive === 'string' ? instance.keepalive : '',
    maxClients: typeof instance.max_clients === 'number' ? String(instance.max_clients) : '',
    dnsServers: Array.isArray(instance.dns_servers) ? instance.dns_servers.map(v => String(v)) : [],
    redirectGateway: instance.redirect_gateway !== false,
    duplicateCN: instance.duplicate_cn !== false,
    verb: typeof instance.verb === 'number' ? String(instance.verb) : '',
  }
}

function openVPNConfigToDraftFromValid(c: OpenVPNCoreConfig): OpenVPNCoreDraft {
  const pki = (c.pki ?? {}) as Record<string, unknown>
  return {
    instances: c.instances.map(instanceToDraft),
    pki: {
      caCert: typeof pki.ca_cert === 'string' ? pki.ca_cert : '',
      serverCert: typeof pki.server_cert === 'string' ? pki.server_cert : '',
      serverKey: typeof pki.server_key === 'string' ? pki.server_key : '',
      tlsCryptKey: typeof pki.tls_crypt_key === 'string' ? pki.tls_crypt_key : '',
    },
  }
}

/**
 * Loading an OpenVPN core always goes through the *strict* validator (same as sing-box's
 * `singBoxConfigToDraft`) rather than a lenient parse - any config that made it into the
 * database already passed `OpenVPNConfig._validate` server-side (including the pki presence
 * check), so a failure here means the stored config is unexpectedly malformed and the caller
 * should fall back to a fresh draft + surface the message as an import warning.
 */
export function openVPNConfigToDraft(raw: unknown): { ok: true; draft: OpenVPNCoreDraft } | { ok: false; message: string } {
  const result = validateOpenVPNCoreConfig(raw)
  if (!result.ok) {
    const first = result.issues[0]
    return { ok: false, message: first ? `${first.path}: ${first.message}` : 'Invalid OpenVPN config' }
  }
  return { ok: true, draft: openVPNConfigToDraftFromValid(result.config) }
}

export function createNewOpenVPNDraft(): OpenVPNCoreDraft {
  return createDefaultOpenVPNCoreDraft()
}

/** For the "Add instance" action: guarantees a fresh default tag unique against the current draft. */
export function createNewOpenVPNInstanceDraft(draft: OpenVPNCoreDraft): OpenVPNInstanceDraft {
  return createDefaultOpenVPNInstanceDraft(draft.instances.map(i => i.tag))
}

export function draftToPersistedConfig(draft: OpenVPNCoreDraft): Record<string, unknown> {
  const json = generateOpenVPNCoreConfigJsonFromDraft(draft)
  return JSON.parse(json) as Record<string, unknown>
}

function draftGenerationIssue(error: unknown): OpenVPNValidationIssue {
  const rawMessage = error instanceof Error ? error.message : String(error)
  const match = rawMessage.match(/^(\/[^:]*):\s*(.+)$/)
  return {
    path: match?.[1] ?? '/',
    code: 'OV_FORM_CONFIG_GENERATION_INVALID',
    message: match?.[2] ?? rawMessage,
  }
}

/** Draft issues + core-kit validation in one step for saves (mirrors getSingBoxPersistConfig). */
export function getOpenVPNPersistConfig(draft: OpenVPNCoreDraft) {
  const issues = validateOpenVPNCoreDraft(draft)
  if (issues.length > 0) {
    return { ok: false as const, draftIssues: issues }
  }
  let config: Record<string, unknown>
  try {
    config = draftToPersistedConfig(draft)
  } catch (error) {
    return { ok: false as const, draftIssues: [draftGenerationIssue(error)] }
  }
  const r = validateCoreConfig('openvpn', config)
  if (!r.ok) {
    return { ok: false as const, kitIssues: r.issues }
  }
  return { ok: true as const, config: r.config as Record<string, unknown> }
}

/** Maps the panel's POST /api/core/openvpn/generate-pki response onto the draft's camelCase pki shape. */
export function applyGeneratedPkiToDraft(
  draft: OpenVPNCoreDraft,
  pki: { ca_cert: string; server_cert: string; server_key: string; tls_crypt_key: string },
): OpenVPNCoreDraft {
  return {
    ...draft,
    pki: {
      caCert: pki.ca_cert,
      serverCert: pki.server_cert,
      serverKey: pki.server_key,
      tlsCryptKey: pki.tls_crypt_key,
    },
  }
}
