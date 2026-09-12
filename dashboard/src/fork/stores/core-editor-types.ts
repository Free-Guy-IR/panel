import type { SingBoxCoreDraft } from '@pasarguard/singbox-config-kit'
import type { OpenVPNCoreDraft } from '@pasarguard/openvpn-config-kit'
import type { MTProtoCoreDraft } from '@pasarguard/mtproto-config-kit'
import type { L2TPCoreDraft } from '@pasarguard/l2tp-config-kit'

export type SbCoreSection =
  | 'inbounds'
  | 'outbounds'
  | 'balancers'
  | 'route'
  | 'ruleSets'
  | 'dns'
  | 'bindings'
  | 'experimental'
  | 'advanced'

export type OvCoreSection = 'instances' | 'pki' | 'advanced'

export type MtCoreSection = 'instances' | 'advanced'

export type L2tpCoreSection = 'settings' | 'advanced'

export type ForkCoreSection = SbCoreSection | OvCoreSection | MtCoreSection | L2tpCoreSection

export type ForkCoreKind = 'singbox' | 'openvpn' | 'mtproto' | 'l2tp'

export type ForkPersistedFields = {
  sbDraft: SingBoxCoreDraft | null
  ovDraft: OpenVPNCoreDraft | null
  mtDraft: MTProtoCoreDraft | null
  l2tpDraft: L2TPCoreDraft | null
}

export interface ForkCoreEditorSlice extends ForkPersistedFields {
  sbBaseline: SingBoxCoreDraft | null
  ovBaseline: OpenVPNCoreDraft | null
  mtBaseline: MTProtoCoreDraft | null
  l2tpBaseline: L2TPCoreDraft | null
  setSbDraft: (d: SingBoxCoreDraft) => void
  updateSbDraft: (updater: (d: SingBoxCoreDraft) => SingBoxCoreDraft) => void
  setOvDraft: (d: OpenVPNCoreDraft) => void
  updateOvDraft: (updater: (d: OpenVPNCoreDraft) => OpenVPNCoreDraft) => void
  setMtDraft: (d: MTProtoCoreDraft) => void
  updateMtDraft: (updater: (d: MTProtoCoreDraft) => MTProtoCoreDraft) => void
  setL2tpDraft: (d: L2TPCoreDraft) => void
  updateL2tpDraft: (updater: (d: L2TPCoreDraft) => L2TPCoreDraft) => void
}

export function isForkCoreKind(kind: string): kind is ForkCoreKind {
  return kind === 'singbox' || kind === 'openvpn' || kind === 'mtproto' || kind === 'l2tp'
}
