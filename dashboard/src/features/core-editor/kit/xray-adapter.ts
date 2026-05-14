import { DEFAULT_XRAY_CORE_CONFIG } from '@/lib/default-xray-core-config'
import { buildXrayConfig, importXrayConfig, normalizeProfile } from '@pasarguard/xray-config-kit'
import type { Issue, Profile } from '@pasarguard/xray-config-kit'
import type { CoreKitValidationIssue } from '@pasarguard/core-kit'
import { validateCoreConfig } from '@pasarguard/core-kit'
import { filterCoreKitIssuesHidingInboundClients } from './inbound-clients-issue-filter'
import { sanitizeProfileInbounds } from './sanitize-inbound'

function isEmptyCompiledConfig(config: unknown): boolean {
  return typeof config === 'object' && config !== null && !Array.isArray(config) && Object.keys(config as object).length === 0
}

function prepareProfileForKit(profile: Profile): Profile {
  return sanitizeProfileInbounds(normalizeProfile(JSON.parse(JSON.stringify(profile)) as Profile))
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function applyInboundSockoptToCompiledConfig(profile: Profile, config: Record<string, unknown>): Record<string, unknown> {
  if (!Array.isArray(config.inbounds)) return config
  const inbounds = config.inbounds.map((compiledInbound, index) => {
    if (!isRecord(compiledInbound)) return compiledInbound
    const profileInbound = profile.inbounds?.[index] as { streamAdvanced?: { sockopt?: unknown } } | undefined
    const sockopt = profileInbound?.streamAdvanced?.sockopt
    if (!isRecord(sockopt) || Object.keys(sockopt).length === 0) return compiledInbound
    const streamSettings = isRecord(compiledInbound.streamSettings) ? { ...compiledInbound.streamSettings } : {}
    streamSettings.sockopt = sockopt
    return { ...compiledInbound, streamSettings }
  })
  return { ...config, inbounds }
}

/**
 * Issues from {@link buildXrayConfig} in strict mode when the profile does not compile (schema / semantic / unsafe patches, …).
 */
export function getXrayStrictCompileBlockers(profile: Profile): Issue[] {
  const { config, issues } = buildXrayConfig(prepareProfileForKit(profile), { mode: 'strict' })
  if (!isEmptyCompiledConfig(config)) return []
  const errors = issues.filter(i => i.severity === 'error')
  return errors.length > 0 ? errors : issues
}

export type XrayPersistValidationResult =
  | { ok: true; config: Record<string, unknown> }
  | { ok: false; strictBlockers: Issue[]; coreKitIssues: CoreKitValidationIssue[] }

export function importRawToProfile(raw: unknown): { profile: Profile; issues: Issue[] } {
  const imported = importXrayConfig(raw)
  let profile = sanitizeProfileInbounds(normalizeProfile(imported.profile))
  
  // Preserve policy from raw config if it exists
  if (typeof raw === 'object' && raw !== null && 'policy' in raw) {
    const policy = (raw as Record<string, unknown>).policy
    if (policy !== undefined) {
      const topLevel = {
        ...(profile.raw?.topLevel ?? {}),
        policy,
      }
      profile = {
        ...profile,
        raw: {
          ...(profile.raw ?? {}),
          topLevel,
        },
      } as Profile
    }
  }
  
  return { profile, issues: [...imported.issues] }
}

export function profileToPersistedConfig(profile: Profile): Record<string, unknown> {
  const prepared = prepareProfileForKit(profile)
  const { config } = buildXrayConfig(prepared, { mode: 'permissive' })
  const result = applyInboundSockoptToCompiledConfig(prepared, config as Record<string, unknown>)
  
  // Restore policy from raw.topLevel if it exists
  if (profile.raw?.topLevel?.policy !== undefined) {
    result.policy = profile.raw.topLevel.policy
  }
  
  return result
}

export function validateProfileForSave(profile: Profile) {
  const config = profileToPersistedConfig(profile)
  return validateCoreConfig('xray', config)
}

/**
 * Persist validation: strict-mode Xray compile blockers from xray-config-kit plus core-kit checks on permissive JSON
 * (inbound clients noise filtered out).
 */
export function validateProfileForPersist(profile: Profile): XrayPersistValidationResult {
  const strictBlockers = getXrayStrictCompileBlockers(profile)
  const config = profileToPersistedConfig(profile)
  const r = validateCoreConfig('xray', config)
  const coreKitIssues = r.ok ? [] : filterCoreKitIssuesHidingInboundClients([...r.issues])

  if (strictBlockers.length > 0 || coreKitIssues.length > 0) {
    return { ok: false, strictBlockers, coreKitIssues }
  }
  return { ok: true, config }
}

export function createNewXrayProfile(): Profile {
  const { profile } = importRawToProfile(DEFAULT_XRAY_CORE_CONFIG)
  return profile
}
