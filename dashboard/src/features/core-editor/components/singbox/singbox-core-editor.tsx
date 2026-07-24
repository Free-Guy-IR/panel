import { Accordion, AccordionContent, AccordionItem, AccordionTrigger } from '@/components/ui/accordion'
import { Button } from '@/components/ui/button'
import { SingBoxInboundForm } from '@/features/core-editor/components/singbox/singbox-inbound-form'
import { XrayAdvancedSection } from '@/features/core-editor/components/xray/xray-advanced-section'
import { useSectionHeaderAddPulseEffect, type SectionHeaderAddPulse } from '@/features/core-editor/hooks/use-section-header-add-pulse'
import { createNewHysteria2InboundDraft } from '@/features/core-editor/kit/singbox-adapter'
import { useCoreEditorStore } from '@/features/core-editor/state/core-editor-store'
import type { SbCoreSection } from '@/features/core-editor/state/core-editor-store'
import { validateHysteria2InboundDraft } from '@pasarguard/singbox-config-kit'
import { Plus, Trash2 } from 'lucide-react'
import { useCallback, useState } from 'react'
import { useTranslation } from 'react-i18next'

interface SingBoxCoreEditorProps {
  headerAddPulse?: SectionHeaderAddPulse
  headerAddEpoch?: number
}

/** Top-level sing-box section: a list of Hysteria2 inbound cards (add/remove), plus the shared Advanced JSON tab. */
export function SingBoxCoreEditor({ headerAddPulse, headerAddEpoch }: SingBoxCoreEditorProps) {
  const { t } = useTranslation()
  const section = useCoreEditorStore(s => s.activeSection) as SbCoreSection
  const draft = useCoreEditorStore(s => s.sbDraft)
  const updateSbDraft = useCoreEditorStore(s => s.updateSbDraft)
  const [openItems, setOpenItems] = useState<string[]>(() => (draft?.inbounds ?? []).map((_, i) => `inbound-${i}`))

  const addInbound = useCallback(() => {
    updateSbDraft(d => {
      const next = createNewHysteria2InboundDraft(d)
      const nextInbounds = [...d.inbounds, next]
      setOpenItems(items => [...items, `inbound-${nextInbounds.length - 1}`])
      return { ...d, inbounds: nextInbounds }
    })
  }, [updateSbDraft])

  // Reacts to the page header's "+ Add" button, but only while the Inbounds section is active.
  useSectionHeaderAddPulseEffect(headerAddPulse, headerAddEpoch, 'inbounds', addInbound)

  const removeInbound = useCallback(
    (index: number) => {
      updateSbDraft(d => ({ ...d, inbounds: d.inbounds.filter((_, i) => i !== index) }))
      setOpenItems(items => items.filter(item => item !== `inbound-${index}`))
    },
    [updateSbDraft],
  )

  if (section === 'advanced') return <XrayAdvancedSection />
  if (!draft) return null

  const allTags = draft.inbounds.map(i => i.tag)

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <p className="text-muted-foreground text-sm">{t('coreEditor.singbox.inboundsHint', { defaultValue: 'Each inbound is a separate Hysteria2 listener on this sing-box core.' })}</p>
        <Button type="button" variant="outline" size="sm" onClick={addInbound} className="gap-1.5">
          <Plus className="size-4" />
          {t('coreEditor.inbound.add', { defaultValue: 'Add inbound' })}
        </Button>
      </div>

      {draft.inbounds.length === 0 ? (
        <div className="text-muted-foreground rounded-md border border-dashed px-4 py-8 text-center text-sm">
          {t('coreEditor.singbox.emptyInbounds', { defaultValue: 'No inbounds yet. Click "Add inbound" to create a Hysteria2 listener.' })}
        </div>
      ) : (
        <Accordion type="multiple" value={openItems} onValueChange={setOpenItems} className="space-y-3">
          {draft.inbounds.map((inbound, index) => {
            const issues = validateHysteria2InboundDraft(inbound, index, allTags)
            const hasErrors = issues.length > 0
            return (
              <AccordionItem key={index} value={`inbound-${index}`} className="rounded-lg border px-3 [&_[data-state=closed]]:no-underline [&_[data-state=open]]:no-underline">
                <div className="flex items-center gap-1">
                  <AccordionTrigger className="flex-1 py-3">
                    <div className="flex min-w-0 flex-1 items-center gap-2 pr-2 text-left">
                      <span className="truncate font-medium">{inbound.tag || t('coreEditor.singbox.untitledInbound', { defaultValue: 'Untitled inbound' })}</span>
                      <span className="text-muted-foreground shrink-0 text-xs">:{inbound.listenPort || '?'}</span>
                      {hasErrors && <span className="text-destructive shrink-0 text-xs font-medium">{t('coreEditor.singbox.hasErrors', { defaultValue: 'Needs attention' })}</span>}
                    </div>
                  </AccordionTrigger>
                  <Button
                    type="button"
                    variant="ghost"
                    size="icon"
                    className="size-8 shrink-0 border-red-500/20 transition-colors hover:border-red-500 hover:bg-red-50 dark:hover:bg-red-950/20"
                    onClick={() => removeInbound(index)}
                    aria-label={t('coreEditor.inbound.remove', { defaultValue: 'Remove inbound' })}
                  >
                    <Trash2 className="text-red-500" />
                  </Button>
                </div>
                <AccordionContent className="pb-4">
                  <SingBoxInboundForm
                    inbound={inbound}
                    issues={issues}
                    onChange={updater => {
                      updateSbDraft(d => ({
                        ...d,
                        inbounds: d.inbounds.map((it, i) => (i === index ? updater(it) : it)),
                      }))
                    }}
                  />
                </AccordionContent>
              </AccordionItem>
            )
          })}
        </Accordion>
      )}
    </div>
  )
}
