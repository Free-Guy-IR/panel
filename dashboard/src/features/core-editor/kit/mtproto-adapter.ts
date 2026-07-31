import {
  createDefaultMTProtoCoreDraft,
  createDefaultMTProtoInstanceDraft,
  generateMTProtoCoreConfigJsonFromDraft,
  validateMTProtoCoreConfig,
  validateMTProtoCoreDraft,
} from '@pasarguard/mtproto-config-kit'
import type { MTProtoCoreConfig, MTProtoCoreDraft, MTProtoInstanceDraft, MTProtoValidationIssue } from '@pasarguard/mtproto-config-kit'
import { validateCoreConfig } from '@pasarguard/core-kit'

function instanceToDraft(raw: unknown): MTProtoInstanceDraft {
  const instance = raw && typeof raw === 'object' ? (raw as Record<string, unknown>) : {}
  return {
    tag: typeof instance.tag === 'string' ? instance.tag : '',
    port: typeof instance.port === 'number' ? instance.port : '',
    fakeTlsDomain: typeof instance.fake_tls_domain === 'string' ? instance.fake_tls_domain : '',
  }
}

function mtprotoConfigToDraftFromValid(c: MTProtoCoreConfig): MTProtoCoreDraft {
  return {
    instances: c.instances.map(instanceToDraft),
  }
}

/**
 * Loading an MTProto core always goes through the *strict* validator (same as OpenVPN's
 * `openVPNConfigToDraft`) rather than a lenient parse - any config that made it into the
 * database already passed `MTProtoConfig._validate` server-side, so a failure here means the
 * stored config is unexpectedly malformed and the caller should fall back to a fresh draft +
 * surface the message as an import warning.
 */
export function mtprotoConfigToDraft(raw: unknown): { ok: true; draft: MTProtoCoreDraft } | { ok: false; message: string } {
  const result = validateMTProtoCoreConfig(raw)
  if (!result.ok) {
    const first = result.issues[0]
    return { ok: false, message: first ? `${first.path}: ${first.message}` : 'Invalid MTProto config' }
  }
  return { ok: true, draft: mtprotoConfigToDraftFromValid(result.config) }
}

export function createNewMTProtoDraft(): MTProtoCoreDraft {
  return createDefaultMTProtoCoreDraft()
}

/** For the "Add instance" action: guarantees a fresh default tag unique against the current draft. */
export function createNewMTProtoInstanceDraft(draft: MTProtoCoreDraft): MTProtoInstanceDraft {
  return createDefaultMTProtoInstanceDraft(draft.instances.map(i => i.tag))
}

export function draftToPersistedConfig(draft: MTProtoCoreDraft): Record<string, unknown> {
  const json = generateMTProtoCoreConfigJsonFromDraft(draft)
  return JSON.parse(json) as Record<string, unknown>
}

function draftGenerationIssue(error: unknown): MTProtoValidationIssue {
  const rawMessage = error instanceof Error ? error.message : String(error)
  const match = rawMessage.match(/^(\/[^:]*):\s*(.+)$/)
  return {
    path: match?.[1] ?? '/',
    code: 'MT_FORM_CONFIG_GENERATION_INVALID',
    message: match?.[2] ?? rawMessage,
  }
}

/** Draft issues + core-kit validation in one step for saves (mirrors getOpenVPNPersistConfig). */
export function getMTProtoPersistConfig(draft: MTProtoCoreDraft) {
  const issues = validateMTProtoCoreDraft(draft)
  if (issues.length > 0) {
    return { ok: false as const, draftIssues: issues }
  }
  let config: Record<string, unknown>
  try {
    config = draftToPersistedConfig(draft)
  } catch (error) {
    return { ok: false as const, draftIssues: [draftGenerationIssue(error)] }
  }
  const r = validateCoreConfig('mtproto', config)
  if (!r.ok) {
    return { ok: false as const, kitIssues: r.issues }
  }
  return { ok: true as const, config: r.config as Record<string, unknown> }
}
