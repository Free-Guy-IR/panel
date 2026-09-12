import { draftToPersistedConfig as sbDraftToPersistedConfig } from '@/features/core-editor/kit/singbox-adapter'
import { draftToPersistedConfig as ovDraftToPersistedConfig } from '@/features/core-editor/kit/openvpn-adapter'
import { draftToPersistedConfig as mtDraftToPersistedConfig } from '@/features/core-editor/kit/mtproto-adapter'
import { draftToPersistedConfig as l2tpDraftToPersistedConfig } from '@/features/core-editor/kit/l2tp-adapter'
import { isForkCoreKind } from './core-editor-types'

type ChangeStateView = {
  kind: string
  sbDraft: unknown
  ovDraft: unknown
  mtDraft: unknown
  l2tpDraft: unknown
  sbBaseline: unknown
  ovBaseline: unknown
  mtBaseline: unknown
  l2tpBaseline: unknown
}

function stableStringify(value: unknown): string {
  try {
    return JSON.stringify(value, null, 2)
  } catch {
    return ''
  }
}

function safeConfigString(label: string, factory: () => unknown): string {
  try {
    return stableStringify(factory())
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error)
    return `__invalid_${label}__:${message}`
  }
}

export function forkCurrentConfigString(s: ChangeStateView): string | null {
  if (!isForkCoreKind(s.kind)) return null
  if (s.kind === 'singbox' && s.sbDraft) {
    const draft = s.sbDraft
    return safeConfigString('sb_current_config', () => sbDraftToPersistedConfig(draft as never))
  }
  if (s.kind === 'openvpn' && s.ovDraft) {
    const draft = s.ovDraft
    return safeConfigString('ov_current_config', () => ovDraftToPersistedConfig(draft as never))
  }
  if (s.kind === 'mtproto' && s.mtDraft) {
    const draft = s.mtDraft
    return safeConfigString('mt_current_config', () => mtDraftToPersistedConfig(draft as never))
  }
  if (s.kind === 'l2tp' && s.l2tpDraft) {
    const draft = s.l2tpDraft
    return safeConfigString('l2tp_current_config', () => l2tpDraftToPersistedConfig(draft as never))
  }
  return ''
}

export function forkBaselineConfigString(s: ChangeStateView): string | null {
  if (!isForkCoreKind(s.kind)) return null
  if (s.kind === 'singbox' && s.sbBaseline) {
    const draft = s.sbBaseline
    return safeConfigString('sb_baseline_config', () => sbDraftToPersistedConfig(draft as never))
  }
  if (s.kind === 'openvpn' && s.ovBaseline) {
    const draft = s.ovBaseline
    return safeConfigString('ov_baseline_config', () => ovDraftToPersistedConfig(draft as never))
  }
  if (s.kind === 'mtproto' && s.mtBaseline) {
    const draft = s.mtBaseline
    return safeConfigString('mt_baseline_config', () => mtDraftToPersistedConfig(draft as never))
  }
  if (s.kind === 'l2tp' && s.l2tpBaseline) {
    const draft = s.l2tpBaseline
    return safeConfigString('l2tp_baseline_config', () => l2tpDraftToPersistedConfig(draft as never))
  }
  return ''
}
