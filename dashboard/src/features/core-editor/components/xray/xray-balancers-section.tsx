import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from '@/components/ui/alert-dialog'
import { StringTagPicker } from '@/components/common/string-tag-picker'
import { Button } from '@/components/ui/button'
import { Form, FormField, FormItem, FormLabel, FormMessage } from '@/components/ui/form'
import { Label } from '@/components/ui/label'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { XrayParityFormControl, isBooleanParityField, type XrayProfileTagOptions } from '@/features/core-editor/components/shared/xray-parity-form-control'
import { CoreEditorDataTable } from '@/features/core-editor/components/shared/core-editor-data-table'
import { CoreEditorFormDialog } from '@/features/core-editor/components/shared/core-editor-form-dialog'
import { useSectionHeaderAddPulseEffect, type SectionHeaderAddPulse } from '@/features/core-editor/hooks/use-section-header-add-pulse'
import { useXrayPersistModifyGuard } from '@/features/core-editor/hooks/use-xray-persist-modify-guard'
import { inferParityFieldMode, parseRoutingRuleFieldValue, routingBalancerFieldToString } from '@/features/core-editor/kit/xray-parity-value'
import { useCoreEditorStore } from '@/features/core-editor/state/core-editor-store'
import { validateBalancerForCommit } from '@/features/core-editor/kit/balancer-dialog-schema'
import { profileDuplicateTagMessage, profileTagHasDuplicateUsage } from '@/features/core-editor/kit/profile-tag-uniqueness'
import { remapIndexAfterArrayMove } from '@/features/core-editor/kit/remap-index-after-move'
import { createDefaultRoutingBalancer, getGeneratedRoutingBalancerFields } from '@pasarguard/xray-config-kit'
import type { Profile, Routing, RoutingBalancer } from '@pasarguard/xray-config-kit'
import useDirDetection from '@/hooks/use-dir-detection'
import type { ColumnDef } from '@tanstack/react-table'
import { arrayMove } from '@dnd-kit/sortable'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useForm } from 'react-hook-form'
import { useTranslation } from 'react-i18next'
import { cn } from '@/lib/utils'
import { Pencil, Plus } from 'lucide-react'

function defaultRouting(): Routing {
  return { domainStrategy: 'AsIs', rules: [] }
}

function replaceBalancer(profile: Profile, index: number, b: RoutingBalancer): Profile {
  const routing = profile.routing ?? defaultRouting()
  const list = [...(routing.balancers ?? [])]
  list[index] = b
  return { ...profile, routing: { ...routing, balancers: list } }
}

function removeBalancer(profile: Profile, index: number): Profile {
  const routing = profile.routing ?? defaultRouting()
  const list = [...(routing.balancers ?? [])]
  list.splice(index, 1)
  return { ...profile, routing: { ...routing, balancers: list.length ? list : undefined } }
}

function balancerSelectorSummary(b: RoutingBalancer): string {
  const s = b.selector ?? []
  if (s.length === 0) return ''
  if (s.length <= 2) return s.join(', ')
  return `${s.slice(0, 2).join(', ')} (+${s.length - 2})`
}

function balancerSearchHaystack(b: RoutingBalancer): string {
  const parts: string[] = [
    b.tag,
    b.fallbackTag ?? '',
    b.strategy?.type ?? '',
    ...(b.selector ?? []),
  ]
  const st = b.strategy
  if (st?.settings !== undefined && st.settings !== null) {
    try {
      parts.push(JSON.stringify(st.settings))
    } catch {
      parts.push(String(st.settings))
    }
  }
  try {
    parts.push(JSON.stringify(b))
  } catch {
    parts.push(String(b))
  }
  return parts.join(' ')
}

function uniqueNonEmptyTags(tags: (string | undefined)[] | undefined): string[] {
  return [...new Set((tags ?? []).filter((t): t is string => typeof t === 'string' && t.trim() !== ''))]
}

/** Xray balancer.strategy.type values (see `conf/router` balancing strategy). */
const BALANCING_STRATEGY_TYPES = ['random', 'roundRobin', 'leastPing', 'leastLoad'] as const

type DialogMode = 'add' | 'edit'

interface XrayBalancersSectionProps {
  headerAddPulse?: SectionHeaderAddPulse
  headerAddEpoch?: number
}

export function XrayBalancersSection({ headerAddPulse, headerAddEpoch }: XrayBalancersSectionProps) {
  const { t } = useTranslation()
  const dir = useDirDetection()
  const profile = useCoreEditorStore(s => s.xrayProfile)
  const updateXrayProfile = useCoreEditorStore(s => s.updateXrayProfile)
  const { assertNoPersistBlockingErrors } = useXrayPersistModifyGuard()
  const profileTagOptions = useMemo<XrayProfileTagOptions>(
    () => ({
      outboundTags: uniqueNonEmptyTags(profile?.outbounds?.map(o => o.tag)),
      inboundTags: uniqueNonEmptyTags(profile?.inbounds?.map(i => i.tag)),
      balancerTags: uniqueNonEmptyTags(profile?.routing?.balancers?.map(b => b.tag)),
    }),
    [profile],
  )
  const [selected, setSelected] = useState(0)
  const [detailOpen, setDetailOpen] = useState(false)
  const [dialogMode, setDialogMode] = useState<DialogMode>('edit')
  const [draftBalancer, setDraftBalancer] = useState<RoutingBalancer | null>(null)
  const [discardDraftOpen, setDiscardDraftOpen] = useState(false)
  const [blockAddWhileDraftOpen, setBlockAddWhileDraftOpen] = useState(false)
  const [selectorCommitError, setSelectorCommitError] = useState<string | null>(null)
  const balancers = profile?.routing?.balancers ?? []

  const b = useMemo(() => {
    if (dialogMode === 'add' && draftBalancer) return draftBalancer
    return balancers[selected]
  }, [dialogMode, draftBalancer, balancers, selected])

  const balancerParityFields = useMemo(() => getGeneratedRoutingBalancerFields(), [])
  const strategyTypeLabel = useMemo(() => {
    const strategyField = balancerParityFields.find(f => f.json === 'strategy')
    return strategyField?.go ?? 'Strategy'
  }, [balancerParityFields])
  const selectorFieldLabel = useMemo(() => {
    const f = balancerParityFields.find(x => x.json === 'selector')
    return f?.go ?? f?.json ?? 'Selectors'
  }, [balancerParityFields])
  const fallbackFieldLabel = useMemo(() => {
    const f = balancerParityFields.find(x => x.json === 'fallbackTag')
    return f?.go ?? f?.json ?? 'Fallback'
  }, [balancerParityFields])
  /** Tag + strategy object only — selector / fallback use {@link StringTagPicker}. */
  const dialogScalarBalancerFields = useMemo(
    () => {
      return balancerParityFields
        .filter(f => f.json !== 'strategy' && f.json !== 'selector' && f.json !== 'fallbackTag')
        .sort((a, b) => {
          const isBoolA = isBooleanParityField(a)
          const isBoolB = isBooleanParityField(b)
          if (isBoolA && !isBoolB) return 1
          if (!isBoolA && isBoolB) return -1
          return 0
        })
    },
    [balancerParityFields],
  )

  const form = useForm<Record<string, string>>({})

  const isBalancerTagDuplicate = useCallback(
    (candidateRaw: string): boolean => {
      if (!profile) return false
      return profileTagHasDuplicateUsage(
        profile,
        candidateRaw,
        dialogMode === 'edit' ? { owner: 'balancer', index: selected } : undefined,
      )
    },
    [profile, dialogMode, selected],
  )

  const setDuplicateBalancerTagError = useCallback(
    (tagValue: string) => {
      form.setError('tag', {
        type: 'validate',
        message: profileDuplicateTagMessage(t, tagValue),
      })
    },
    [form, t],
  )

  const profileRef = useRef(profile)
  profileRef.current = profile

  useEffect(() => {
    if (!detailOpen) return
    const p = profileRef.current
    if (!p) return
    const row = dialogMode === 'add' && draftBalancer ? draftBalancer : p.routing?.balancers?.[selected]
    if (!row) return
    const next: Record<string, string> = {}
    for (const f of dialogScalarBalancerFields) {
      next[f.json] = routingBalancerFieldToString(row, f.json, f)
    }
    form.reset(next)
    setSelectorCommitError(null)
  }, [detailOpen, selected, dialogMode, draftBalancer, form, dialogScalarBalancerFields])

  const beginAddBalancer = useCallback(() => {
    if (!profile) return
    if (detailOpen && dialogMode === 'add' && draftBalancer !== null) {
      setBlockAddWhileDraftOpen(true)
      return
    }
    const next = createDefaultRoutingBalancer({
      tag: `balancer-${(profile.routing?.balancers ?? []).length + 1}`,
    })
    // Kit defaults selector to ["proxy-"]; editor starts with no outbound tags selected.
    setDraftBalancer({ ...next, selector: [] })
    setDialogMode('add')
    setDetailOpen(true)
  }, [profile, detailOpen, dialogMode, draftBalancer])

  useSectionHeaderAddPulseEffect(headerAddPulse, headerAddEpoch, 'balancers', beginAddBalancer)

  const columns = useMemo<ColumnDef<RoutingBalancer, unknown>[]>(
    () => [
      {
        id: 'index',
        header: '#',
        cell: ({ row }) => row.index + 1,
      },
      {
        accessorKey: 'tag',
        header: () => t('coreEditor.col.tag', { defaultValue: 'Tag' }),
        cell: ({ row }) => <span className="text-xs">{row.original.tag}</span>,
      },
      {
        id: 'selector',
        header: () => t('coreEditor.balancer.selector', { defaultValue: 'Selector (outbounds)' }),
        cell: ({ row }) => {
          const full = (row.original.selector ?? []).join(', ')
          const summary = balancerSelectorSummary(row.original)
          return (
            <span
              className="line-clamp-2 min-w-0 max-w-72 text-xs"
              title={full || undefined}
            >
              {summary || '—'}
            </span>
          )
        },
      },
      {
        id: 'fallback',
        header: () => t('coreEditor.balancer.fallback', { defaultValue: 'Fallback' }),
        cell: ({ row }) => {
          const fb = row.original.fallbackTag
          return <span className="text-xs">{fb != null && String(fb) !== '' ? String(fb) : '—'}</span>
        },
      },
      {
        id: 'strategy',
        header: () => t('coreEditor.balancer.strategy', { defaultValue: 'Strategy' }),
        cell: ({ row }) => <span className="text-xs">{row.original.strategy?.type ?? '—'}</span>,
      },
    ],
    [t],
  )

  if (!profile) return null

  const finalizeDetailClose = () => {
    setDetailOpen(false)
    setDialogMode('edit')
    setDraftBalancer(null)
  }

  const handleDetailOpenChange = (open: boolean) => {
    if (open) {
      setDetailOpen(true)
      return
    }
    if (dialogMode === 'add' && draftBalancer !== null) {
      setDiscardDraftOpen(true)
      return
    }
    finalizeDetailClose()
  }

  const commitAddBalancer = () => {
    if (!draftBalancer) return
    const rowForValidate: RoutingBalancer = {
      ...draftBalancer,
      tag: String(form.getValues('tag') ?? draftBalancer.tag ?? ''),
    }
    const parsed = validateBalancerForCommit(t, rowForValidate)
    if (!parsed.success) {
      const fe = parsed.error.flatten().fieldErrors
      if (fe.tag?.[0]) form.setError('tag', { type: 'validate', message: fe.tag[0] })
      setSelectorCommitError(fe.selector?.[0] ?? null)
      return
    }
    form.clearErrors()
    setSelectorCommitError(null)
    if (profile && isBalancerTagDuplicate(parsed.data.tag)) {
      setDuplicateBalancerTagError(parsed.data.tag)
      return
    }
    if (!assertNoPersistBlockingErrors()) return
    const { tag, selector } = parsed.data
    updateXrayProfile(p => {
      const routing = p.routing ?? defaultRouting()
      const nextRow: RoutingBalancer = { ...draftBalancer, tag, selector }
      return { ...p, routing: { ...routing, balancers: [...(routing.balancers ?? []), nextRow] } }
    })
    setSelected(balancers.length)
    finalizeDetailClose()
  }

  const commitEditBalancer = () => {
    if (dialogMode !== 'edit' || !b) return
    const rowForValidate: RoutingBalancer = {
      ...b,
      tag: String(form.getValues('tag') ?? b.tag ?? ''),
    }
    const parsed = validateBalancerForCommit(t, rowForValidate)
    if (!parsed.success) {
      const fe = parsed.error.flatten().fieldErrors
      if (fe.tag?.[0]) form.setError('tag', { type: 'validate', message: fe.tag[0] })
      setSelectorCommitError(fe.selector?.[0] ?? null)
      return
    }
    form.clearErrors()
    setSelectorCommitError(null)
    if (profile && isBalancerTagDuplicate(parsed.data.tag)) {
      setDuplicateBalancerTagError(parsed.data.tag)
      return
    }
    finalizeDetailClose()
  }

  const patchBalancer = (patch: Partial<RoutingBalancer>) => {
    if (!b) return
    if (patch.selector !== undefined) {
      const sel = (patch.selector ?? []).map(s => String(s).trim()).filter(s => s.length > 0)
      if (sel.length > 0) setSelectorCommitError(null)
    }
    if (dialogMode === 'add' && draftBalancer !== null) {
      setDraftBalancer({ ...draftBalancer, ...patch })
      return
    }
    updateXrayProfile(p => replaceBalancer(p, selected, { ...b, ...patch }))
  }

  return (
    <div className="space-y-6">
      <CoreEditorDataTable
        columns={columns}
        data={balancers}
        getSearchableText={balancerSearchHaystack}
        searchPlaceholder={t('coreEditor.balancer.searchPlaceholder', {
          defaultValue: 'Search by tag, selector outbounds, strategy…',
        })}
        bulkDeleteTitle={t('coreEditor.balancer.bulkDeleteTitle', {
          defaultValue: 'Remove selected balancers',
        })}
        emptyLabel={t('coreEditor.balancer.emptyBalancers', { defaultValue: 'No balancers' })}
        getRowId={(_, i) => String(i)}
        onRowClick={(_row, rowIndex) => {
          if (detailOpen && dialogMode === 'add' && draftBalancer !== null) {
            setBlockAddWhileDraftOpen(true)
            return
          }
          setDraftBalancer(null)
          setDialogMode('edit')
          setSelected(rowIndex)
          setDetailOpen(true)
        }}
        onRemoveRow={i => {
          updateXrayProfile(p => removeBalancer(p, i))
          setSelected(0)
        }}
        onBulkRemove={indices => {
          const rm = new Set(indices)
          updateXrayProfile(p => {
            const routing = p.routing ?? defaultRouting()
            const prev = [...(routing.balancers ?? [])]
            const next = prev.filter((_, idx) => !rm.has(idx))
            return {
              ...p,
              routing: {
                ...routing,
                balancers: next.length ? next : undefined,
              },
            }
          })
          setSelected(0)
        }}
        enableReorder
        onReorder={(from, to) => {
          updateXrayProfile(p => {
            const routing = p.routing ?? defaultRouting()
            const list = [...(routing.balancers ?? [])]
            return {
              ...p,
              routing: {
                ...routing,
                balancers: arrayMove(list, from, to),
              },
            }
          })
          setSelected(sel => remapIndexAfterArrayMove(sel, from, to))
        }}
      />

      <CoreEditorFormDialog
        isDialogOpen={detailOpen}
        onOpenChange={handleDetailOpenChange}
        leadingIcon={dialogMode === 'add' ? <Plus className="h-5 w-5 shrink-0" /> : <Pencil className="h-5 w-5 shrink-0" />}
        title={
          dialogMode === 'add'
            ? t('coreEditor.balancer.dialogTitleAdd', { defaultValue: 'Add balancer' })
            : t('coreEditor.balancer.dialogTitleEdit', { defaultValue: 'Edit balancer' })
        }
        size="md"
        footerExtra={
          dialogMode === 'add' && draftBalancer ? (
            <Button type="button" className="sm:min-w-[88px]" onClick={commitAddBalancer}>
              {t('coreEditor.balancer.addToList', { defaultValue: 'Add to list' })}
            </Button>
          ) : dialogMode === 'edit' && b ? (
            <Button type="button" className="sm:min-w-[88px]" onClick={commitEditBalancer}>
              {t('modify')}
            </Button>
          ) : undefined
        }
      >
        {b && (
          <Form {...form}>
            <form className="flex flex-col gap-4 pb-6" onSubmit={e => e.preventDefault()}>
              <div className="grid gap-4 sm:grid-cols-2">
                {dialogScalarBalancerFields.map(def => {
                  const jsonKey = def.json
                  const fullRow = inferParityFieldMode(def) === 'json'
                  const tagFullWidth = jsonKey === 'tag'
                  return (
                    <FormField
                      key={jsonKey}
                      control={form.control}
                      name={jsonKey}
                      render={({ field }) => (
                        <FormItem
                          className={cn(
                            'min-w-0 w-full',
                            (fullRow || tagFullWidth) && 'sm:col-span-2',
                          )}
                        >
                          <FormLabel className="text-xs font-medium">
                            {def.go || def.json}
                          </FormLabel>
                          <XrayParityFormControl
                            field={def}
                            value={field.value ?? ''}
                            profileTagOptions={profileTagOptions}
                            onChange={v => {
                              field.onChange(v)
                              try {
                                const { value } = parseRoutingRuleFieldValue(jsonKey, def, v)
                                if (jsonKey === 'tag') {
                                  const nextTag = String(value ?? '')
                                  patchBalancer({ tag: nextTag })
                                  const snap = useCoreEditorStore.getState().xrayProfile
                                  if (
                                    snap &&
                                    profileTagHasDuplicateUsage(
                                      snap,
                                      nextTag,
                                      dialogMode === 'edit' ? { owner: 'balancer', index: selected } : undefined,
                                    )
                                  ) {
                                    setDuplicateBalancerTagError(nextTag)
                                  } else {
                                    form.clearErrors('tag')
                                  }
                                }
                              } catch {
                                /* ignore */
                              }
                            }}
                          />
                          <FormMessage />
                        </FormItem>
                      )}
                    />
                  )
                })}

                <div className="min-w-0 flex flex-col gap-2.5 sm:col-span-2">
                  <Label className="text-xs font-medium">{selectorFieldLabel}</Label>
                  <StringTagPicker
                    mode="multi"
                    options={profileTagOptions.outboundTags}
                    valueMulti={b.selector ?? []}
                    onChangeMulti={next => patchBalancer({ selector: next })}
                    emptyHint={t('coreEditor.balancer.selectorEmpty', {
                      defaultValue: 'Add outbound tags to participate in this balancer.',
                    })}
                    placeholder={t('coreEditor.balancer.selectorPlaceholder', {
                      defaultValue: 'Select outbound tags…',
                    })}
                    clearAllLabel={t('coreEditor.balancer.clearSelectors', { defaultValue: 'Clear all' })}
                    addButtonLabel={t('coreEditor.balancer.addOutboundTag', { defaultValue: 'Add tag' })}
                  />
                  {selectorCommitError ? (
                    <p className="text-sm font-medium text-destructive" role="alert">
                      {selectorCommitError}
                    </p>
                  ) : null}
                </div>

                <div className="min-w-0 flex flex-col gap-2.5 sm:col-span-2">
                  <Label className="text-xs font-medium">{fallbackFieldLabel}</Label>
                  <StringTagPicker
                    mode="single"
                    options={profileTagOptions.outboundTags}
                    valueSingle={b.fallbackTag ?? ''}
                    onChangeSingle={next => patchBalancer({ fallbackTag: next.trim() || undefined })}
                    placeholder={t('coreEditor.balancer.fallbackPlaceholder', {
                      defaultValue: 'Choose fallback outbound…',
                    })}
                  />
                </div>

                <div className="min-w-0 flex flex-col gap-2.5 sm:col-span-2">
                  <Label className="text-xs font-medium">{strategyTypeLabel}</Label>
                  <Select
                    value={(() => {
                      const raw = b.strategy?.type?.trim() ?? ''
                      if (!raw) return '__none'
                      if (BALANCING_STRATEGY_TYPES.includes(raw as (typeof BALANCING_STRATEGY_TYPES)[number])) return raw
                      return raw
                    })()}
                    onValueChange={v => {
                      if (v === '__none') {
                        patchBalancer({ strategy: undefined })
                        return
                      }
                      patchBalancer({
                        strategy: {
                          type: v,
                          settings: b.strategy?.type === v ? b.strategy?.settings : undefined,
                        },
                      })
                    }}
                  >
                    <SelectTrigger className="h-10" dir="ltr">
                      <SelectValue
                        placeholder={t('coreEditor.balancer.strategyPlaceholder', { defaultValue: 'Strategy' })}
                      />
                    </SelectTrigger>
                    <SelectContent>
                      <SelectItem value="__none">
                        {t('coreEditor.balancer.strategyNone', { defaultValue: 'Default (none)' })}
                      </SelectItem>
                      {BALANCING_STRATEGY_TYPES.map(st => (
                        <SelectItem key={st} value={st}>
                          {st}
                        </SelectItem>
                      ))}
                      {(() => {
                        const raw = b.strategy?.type?.trim() ?? ''
                        if (
                          !raw ||
                          BALANCING_STRATEGY_TYPES.includes(raw as (typeof BALANCING_STRATEGY_TYPES)[number]) ||
                          raw === '__none'
                        ) {
                          return null
                        }
                        return (
                          <SelectItem value={raw}>
                            {raw}{' '}
                            <span className="text-muted-foreground">
                              ({t('coreEditor.balancer.strategyFromConfig', { defaultValue: 'from config' })})
                            </span>
                          </SelectItem>
                        )
                      })()}
                    </SelectContent>
                  </Select>
                </div>
              </div>
            </form>
          </Form>
        )}
      </CoreEditorFormDialog>

      <AlertDialog open={discardDraftOpen} onOpenChange={setDiscardDraftOpen}>
        <AlertDialogContent dir={dir}>
          <AlertDialogHeader>
            <AlertDialogTitle>
              {t('coreEditor.balancer.discardDraftTitle', { defaultValue: 'Discard new balancer?' })}
            </AlertDialogTitle>
            <AlertDialogDescription>
              {t('coreEditor.balancer.discardDraftDescription', {
                defaultValue: 'This balancer is not in the list yet. Closing without adding will discard your changes.',
              })}
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>{t('cancel')}</AlertDialogCancel>
            <AlertDialogAction
              onClick={() => {
                setDiscardDraftOpen(false)
                finalizeDetailClose()
              }}
            >
              {t('coreEditor.balancer.discardDraftAction', { defaultValue: 'Discard' })}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      <AlertDialog open={blockAddWhileDraftOpen} onOpenChange={setBlockAddWhileDraftOpen}>
        <AlertDialogContent dir={dir}>
          <AlertDialogHeader>
            <AlertDialogTitle>
              {t('coreEditor.balancer.finishCurrentTitle', { defaultValue: 'Finish the current balancer first' })}
            </AlertDialogTitle>
            <AlertDialogDescription>
              {t('coreEditor.balancer.finishCurrentDescription', {
                defaultValue: 'Add it to the list, or close the dialog and discard the draft, before starting another balancer.',
              })}
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogAction type="button" className="sm:min-w-[88px]" onClick={() => setBlockAddWhileDraftOpen(false)}>
              {t('close', { defaultValue: 'Close' })}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  )
}
