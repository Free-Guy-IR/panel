import type { CoreKind } from '@pasarguard/core-kit'
import type { CoreResponseType } from '@/service/api'

export function apiCoreTypeToKind(type: CoreResponseType | undefined): CoreKind {
  if (type === 'wg') return 'wg'
  if (type === 'singbox') return 'singbox'
  if (type === 'openvpn') return 'openvpn'
  if (type === 'mtproto') return 'mtproto'
  return 'xray'
}

export function isSupportedCoreEditorKind(type: CoreResponseType | undefined): boolean {
  return (
    type === 'wg' ||
    type === 'xray' ||
    type === 'singbox' ||
    type === 'openvpn' ||
    type === 'mtproto' ||
    type == null ||
    type === undefined
  )
}
