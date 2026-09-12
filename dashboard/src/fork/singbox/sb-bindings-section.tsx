import { XrayInboundTagSelectors } from '@/features/core-editor/components/shared/xray-inbound-tag-selectors'
import { useCoreEditorStore } from '@/features/core-editor/state/core-editor-store'
import { useMemo } from 'react'

/**
 * sing-box bindings: the fallback + exclusion inbound-tag pickers, kept identical to the Xray
 * editor's Bindings section. Tags come from the sing-box inbounds; values live on the store's
 * fallbacksInboundTags / excludeInboundTags (same as Xray).
 */
export function SbBindingsSection() {
  const draft = useCoreEditorStore(s => s.sbDraft)
  const fallbackTags = useCoreEditorStore(s => s.fallbacksInboundTags)
  const excludedTags = useCoreEditorStore(s => s.excludeInboundTags)
  const setFallbacks = useCoreEditorStore(s => s.setFallbacksInboundTags)
  const setExcluded = useCoreEditorStore(s => s.setExcludeInboundTags)

  const inboundTags = useMemo(() => (draft?.inbounds ?? []).map(i => i.tag).filter((t): t is string => !!t), [draft])

  return <XrayInboundTagSelectors inboundTags={inboundTags} fallbackTags={fallbackTags} excludedTags={excludedTags} onFallbackChange={setFallbacks} onExcludedChange={setExcluded} />
}
