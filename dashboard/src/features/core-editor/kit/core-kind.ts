import type { CoreKind } from '@pasarguard/core-kit'
import type { CoreResponseType } from '@/service/api'

export function apiCoreTypeToKind(type: CoreResponseType | undefined): CoreKind {
  if (type === 'wg') return 'wg'
  if (type === 'singbox') return 'singbox'
  return 'xray'
}

export function isSupportedCoreEditorKind(type: CoreResponseType | undefined): boolean {
  return type === 'wg' || type === 'xray' || type === 'singbox' || type == null || type === undefined
}
