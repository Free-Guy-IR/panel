import { cn } from '@/lib/utils'
import { useCoreEditorStore } from '@/features/core-editor/state/core-editor-store'
import { SING_BOX_CORE_SECTION_NAV, WG_CORE_SECTION_NAV, XRAY_CORE_SECTION_NAV } from '@/features/core-editor/kit/core-section-nav'
import { useTranslation } from 'react-i18next'

function sectionNavForKind(kind: 'xray' | 'wg' | 'singbox') {
  if (kind === 'wg') return WG_CORE_SECTION_NAV
  if (kind === 'singbox') return SING_BOX_CORE_SECTION_NAV
  return XRAY_CORE_SECTION_NAV
}

/** Horizontal section tabs — same spacing and triggers as `_dashboard.nodes` primary tabs. */
export function CoreSectionTabs({ className }: { className?: string }) {
  const { t } = useTranslation()
  const kind = useCoreEditorStore(s => s.kind)
  const active = useCoreEditorStore(s => s.activeSection)
  const setActive = useCoreEditorStore(s => s.setActiveSection)
  const items = sectionNavForKind(kind)

  return (
    <div className={cn('flex w-full border-b px-4', className)} role="tablist" aria-label={t('coreEditor.section.label', { defaultValue: 'Section' })}>
      <div className="scrollbar-none flex min-w-0 flex-1 overflow-x-auto">
        {items.map(item => {
          const Icon = item.icon
          const isActive = active === item.id
          return (
            <button
              key={item.id}
              type="button"
              role="tab"
              aria-selected={isActive}
              onClick={() => setActive(item.id)}
              className={cn(
                'relative shrink-0 px-3 py-2 text-sm font-medium transition-colors',
                isActive ? 'border-primary text-foreground border-b-2' : 'text-muted-foreground hover:text-foreground',
              )}
            >
              <span className="flex items-center gap-1.5 whitespace-nowrap">
                <Icon className="h-4 w-4 shrink-0" aria-hidden />
                {t(item.labelKey, { defaultValue: item.defaultLabel })}
              </span>
            </button>
          )
        })}
      </div>
    </div>
  )
}

/** Non-interactive tab strip matching {@link CoreSectionTabs} (loading / skeleton shell). */
export function CoreSectionTabsPlaceholder({
  kind,
  activeSectionId,
  className,
}: {
  kind: 'xray' | 'wg' | 'singbox'
  /** Defaults: inbounds (xray/singbox) / interface (wg). */
  activeSectionId?: string
  className?: string
}) {
  const { t } = useTranslation()
  const items = sectionNavForKind(kind)
  const active = activeSectionId ?? (kind === 'wg' ? 'interface' : 'inbounds')

  return (
    <div className={cn('flex w-full border-b px-4', className)} role="presentation" aria-busy="true" aria-label={t('coreEditor.section.label', { defaultValue: 'Section' })}>
      <div className="scrollbar-none flex min-w-0 flex-1 overflow-x-auto">
        {items.map(item => {
          const Icon = item.icon
          const isActive = active === item.id
          return (
            <div key={item.id} className={cn('relative shrink-0 px-3 py-2 text-sm font-medium', isActive ? 'border-primary text-foreground border-b-2' : 'text-muted-foreground')}>
              <span className="flex items-center gap-1.5 whitespace-nowrap">
                <Icon className="h-4 w-4 shrink-0" aria-hidden />
                {t(item.labelKey, { defaultValue: item.defaultLabel })}
              </span>
            </div>
          )
        })}
      </div>
    </div>
  )
}
