import { L2TPCoreForm } from '@/features/core-editor/components/l2tp/l2tp-form'
import { XrayAdvancedSection } from '@/features/core-editor/components/xray/xray-advanced-section'
import { useCoreEditorStore } from '@/features/core-editor/state/core-editor-store'
import type { L2tpCoreSection } from '@/features/core-editor/state/core-editor-store'

export function L2TPCoreEditor() {
  const section = useCoreEditorStore(s => s.activeSection) as L2tpCoreSection

  return (
    <div className="space-y-8">
      {section === 'settings' && <L2TPCoreForm />}
      {section === 'advanced' && <XrayAdvancedSection />}
    </div>
  )
}
