import { isForkCoreKind } from './core-editor-types'

type DraftSigView = {
  kind: string
  sbDraft: unknown
  ovDraft: unknown
  mtDraft: unknown
  l2tpDraft: unknown
}

export function forkDraftSig(s: DraftSigView): string | null {
  if (!isForkCoreKind(s.kind)) return null
  if (s.kind === 'singbox') return JSON.stringify(s.sbDraft)
  if (s.kind === 'openvpn') return JSON.stringify(s.ovDraft)
  if (s.kind === 'mtproto') return JSON.stringify(s.mtDraft)
  return JSON.stringify(s.l2tpDraft)
}
