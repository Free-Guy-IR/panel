import {
  createDefaultL2TPCoreDraft,
  l2tpCoreDraftFromConfig,
  rawL2TPCoreConfigFromDraft,
  stripWhitespace,
  validateL2TPCoreConfig,
  validateL2TPCoreDraft,
} from '@pasarguard/l2tp-config-kit'
import type { L2TPCoreDraft, L2TPValidationIssue } from '@pasarguard/l2tp-config-kit'
import { validateCoreConfig } from '@pasarguard/core-kit'

const PSK_STANDIN = 'PanelGeneratesThisKey'

function pythonFalsy(value: unknown): boolean {
  if (value == null || value === false || value === 0 || value === '') return true
  if (Array.isArray(value)) return value.length === 0
  if (typeof value === 'object') return Object.keys(value as object).length === 0
  return false
}

function panelWillGeneratePsk(value: unknown): boolean {
  if (pythonFalsy(value)) return true
  return typeof value === 'string' && stripWhitespace(value) === ''
}

export function l2tpPskLeftToPanel(draft: L2TPCoreDraft): boolean {
  return stripWhitespace(draft.psk) === ''
}

export function l2tpDraftIssues(draft: L2TPCoreDraft): L2TPValidationIssue[] {
  const issues = validateL2TPCoreDraft(draft)
  if (!l2tpPskLeftToPanel(draft)) return issues
  return issues.filter(issue => issue.path !== '/psk')
}

export function l2tpConfigToDraft(raw: unknown): { ok: true; draft: L2TPCoreDraft } | { ok: false; message: string } {
  const record = raw && typeof raw === 'object' && !Array.isArray(raw) ? (raw as Record<string, unknown>) : null
  const leaveToPanel = record !== null && panelWillGeneratePsk(record.psk)
  const candidate = leaveToPanel ? { ...record, psk: PSK_STANDIN } : raw

  const result = validateL2TPCoreConfig(candidate)
  if (!result.ok) {
    const first = result.issues[0]
    return { ok: false, message: first ? `${first.path}: ${first.message}` : 'Invalid L2TP config' }
  }

  const draft = l2tpCoreDraftFromConfig(result.config)
  return { ok: true, draft: leaveToPanel ? { ...draft, psk: '' } : draft }
}

export function createNewL2TPDraft(): L2TPCoreDraft {
  return createDefaultL2TPCoreDraft()
}

export function draftToPersistedConfig(draft: L2TPCoreDraft): Record<string, unknown> {
  return rawL2TPCoreConfigFromDraft(draft)
}

export function getL2TPPersistConfig(draft: L2TPCoreDraft) {
  const issues = l2tpDraftIssues(draft)
  if (issues.length > 0) {
    return { ok: false as const, draftIssues: issues }
  }

  const leaveToPanel = l2tpPskLeftToPanel(draft)
  const raw = rawL2TPCoreConfigFromDraft(draft)
  const r = validateCoreConfig('l2tp', leaveToPanel ? { ...raw, psk: PSK_STANDIN } : raw)
  if (!r.ok) {
    return { ok: false as const, kitIssues: r.issues }
  }

  const config = r.config as Record<string, unknown>
  return { ok: true as const, config: leaveToPanel ? { ...config, psk: '' } : config }
}
