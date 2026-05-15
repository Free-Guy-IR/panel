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
import { Badge } from '@/components/ui/badge'
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Form, FormField, FormItem, FormLabel, FormMessage } from '@/components/ui/form'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { Switch } from '@/components/ui/switch'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { Textarea } from '@/components/ui/textarea'
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from '@/components/ui/tooltip'
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
import type { JsonObject, JsonValue, Profile, Routing, RoutingBalancer } from '@pasarguard/xray-config-kit'
import useDirDetection from '@/hooks/use-dir-detection'
import type { ColumnDef } from '@tanstack/react-table'
import { arrayMove } from '@dnd-kit/sortable'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useForm } from 'react-hook-form'
import { useTranslation } from 'react-i18next'
import { cn } from '@/lib/utils'
import { Eye, Pencil, Plus, Trash2 } from 'lucide-react'

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

type LeastLoadCostRow = {
  regexp: boolean
  match: string
  value: string
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function compactJsonObject(settings: Record<string, JsonValue | undefined>): JsonObject | undefined {
  const next: Record<string, JsonValue> = {}
  for (const [key, value] of Object.entries(settings)) {
    if (value === undefined) continue
    next[key] = value
  }
  return Object.keys(next).length > 0 ? next : undefined
}

function mutableJsonObject(value: JsonObject | undefined): Record<string, JsonValue | undefined> {
  return isRecord(value) ? { ...(value as Record<string, JsonValue | undefined>) } : {}
}

function parseOptionalNumber(raw: string): number | undefined {
  const value = raw.trim()
  if (!value) return undefined
  const parsed = Number(value)
  return Number.isFinite(parsed) ? parsed : undefined
}

function readJsonObject(value: unknown): JsonObject | undefined {
  return isRecord(value) ? (value as JsonObject) : undefined
}

function readTopLevelObject(profile: Profile, key: string): JsonObject | undefined {
  return readJsonObject(profile.raw?.topLevel?.[key])
}

function setTopLevelValue(profile: Profile, key: string, value: JsonValue | undefined): Profile {
  const topLevel: Record<string, JsonValue> = { ...(profile.raw?.topLevel ?? {}) }
  if (value === undefined) delete topLevel[key]
  else topLevel[key] = value

  return {
    ...profile,
    raw: {
      ...(profile.raw ?? {}),
      topLevel: Object.keys(topLevel).length > 0 ? topLevel : undefined,
    },
  } as Profile
}

function readStringProperty(obj: JsonObject | undefined, key: string, fallbackKey?: string): string {
  const value = obj?.[key]
  if (typeof value === 'string') return value
  const fallback = fallbackKey ? obj?.[fallbackKey] : undefined
  return typeof fallback === 'string' ? fallback : ''
}

function readNumberProperty(obj: JsonObject | undefined, key: string): string {
  const value = obj?.[key]
  return typeof value === 'number' && Number.isFinite(value) ? String(value) : ''
}

function readBooleanProperty(obj: JsonObject | undefined, key: string): boolean {
  return obj?.[key] === true
}

function readStringArrayProperty(obj: JsonObject | undefined, key: string): string[] {
  const value = obj?.[key]
  if (!Array.isArray(value)) return []
  return value.map(item => String(item).trim()).filter(Boolean)
}

function readStringListSetting(settings: JsonObject | undefined, key: string): string {
  const value = settings?.[key]
  return Array.isArray(value) ? value.map(item => String(item)).join('\n') : ''
}

function parseStringListSetting(raw: string): string[] | undefined {
  const values = raw
    .split(/[\n,]+/)
    .map(item => item.trim())
    .filter(Boolean)
  return values.length > 0 ? values : undefined
}

function readLeastLoadCosts(settings: JsonObject | undefined): LeastLoadCostRow[] {
  const raw = settings?.costs
  if (!Array.isArray(raw)) return []
  return raw
    .filter(isRecord)
    .map(item => ({
      regexp: item.regexp === true,
      match: typeof item.match === 'string' ? item.match : '',
      value: typeof item.value === 'number' && Number.isFinite(item.value) ? String(item.value) : '',
    }))
}

function costsToJson(rows: LeastLoadCostRow[]): JsonObject[] | undefined {
  const costs = rows
    .map(row => {
      const match = row.match.trim()
      const value = parseOptionalNumber(row.value)
      if (!match && value === undefined && row.regexp !== true) return null
      const cost: Record<string, JsonValue> = { regexp: row.regexp }
      if (match) cost.match = match
      if (value !== undefined) cost.value = value
      return cost as JsonObject
    })
    .filter((row): row is JsonObject => row !== null)
  return costs.length > 0 ? costs : undefined
}

function defaultObservatory(subjectSelector: string[] = []): JsonObject {
  return {
    subjectSelector,
    probeURL: 'https://www.google.com/generate_204',
    probeInterval: '10m',
    enableConcurrency: true,
  }
}

function defaultBurstObservatory(subjectSelector: string[] = []): JsonObject {
  return {
    subjectSelector,
    pingConfig: {
      destination: 'https://connectivitycheck.gstatic.com/generate_204',
      connectivity: '',
      interval: '1m',
      sampling: 10,
      timeout: '5s',
      httpMethod: 'HEAD',
    },
  }
}

function collectBalancerSelectors(profile: Profile): string[] {
  return uniqueNonEmptyTags((profile.routing?.balancers ?? []).flatMap(balancer => balancer.selector ?? []))
}

function balancerRequiresObservation(balancer: RoutingBalancer | undefined): boolean {
  const strategy = balancer?.strategy?.type
  return strategy === 'leastPing' || strategy === 'leastLoad'
}

function profileHasLeastPingBalancer(profile: Profile): boolean {
  return (profile.routing?.balancers ?? []).some(balancer => balancer.strategy?.type === 'leastPing')
}

function profileHasObservation(profile: Profile): boolean {
  return readTopLevelObject(profile, 'observatory') !== undefined || readTopLevelObject(profile, 'burstObservatory') !== undefined
}

function ensureObservationForProfile(profile: Profile, selector: string[]): Profile {
  if (profileHasObservation(profile)) return profile
  // Observatory needs a `leastPing` balancer somewhere in the profile to be useful;
  // otherwise default to `burstObservatory`, which works for any strategy that benefits from probes.
  const key: 'observatory' | 'burstObservatory' = profileHasLeastPingBalancer(profile) ? 'observatory' : 'burstObservatory'
  const value = key === 'observatory' ? defaultObservatory(selector) : defaultBurstObservatory(selector)
  return setTopLevelValue(profile, key, value)
}

type DialogMode = 'add' | 'edit'
type ObservationTab = 'observatory' | 'burstObservatory'

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
  const [observationTab, setObservationTab] = useState<ObservationTab>('observatory')
  const [observationDialogOpen, setObservationDialogOpen] = useState(false)
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
      const nextProfile = { ...p, routing: { ...routing, balancers: [...(routing.balancers ?? []), nextRow] } }
      // Every balancer benefits from observation data; enable a default source if none exists yet.
      return ensureObservationForProfile(nextProfile, selector)
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
    if (balancerRequiresObservation(b)) {
      updateXrayProfile(p => ensureObservationForProfile(p, b.selector ?? []))
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

  const patchStrategySettings = (patch: Record<string, JsonValue | undefined>) => {
    if (!b) return
    const type = b.strategy?.type?.trim() || 'leastLoad'
    const settings = mutableJsonObject(b.strategy?.settings)
    for (const [key, value] of Object.entries(patch)) {
      if (value === undefined) delete settings[key]
      else settings[key] = value
    }
    patchBalancer({
      strategy: {
        type,
        settings: compactJsonObject(settings),
      },
    })
  }

  const patchLeastLoadCost = (index: number, patch: Partial<LeastLoadCostRow>) => {
    const rows = readLeastLoadCosts(b?.strategy?.settings)
    rows[index] = { ...(rows[index] ?? { regexp: false, match: '', value: '' }), ...patch }
    patchStrategySettings({ costs: costsToJson(rows) })
  }

  const removeLeastLoadCost = (index: number) => {
    const rows = readLeastLoadCosts(b?.strategy?.settings).filter((_, rowIndex) => rowIndex !== index)
    patchStrategySettings({ costs: costsToJson(rows) })
  }

  const addLeastLoadCost = () => {
    const rows = [...readLeastLoadCosts(b?.strategy?.settings), { regexp: false, match: 'tag', value: '0.5' }]
    patchStrategySettings({ costs: costsToJson(rows) })
  }

  const observatory = readTopLevelObject(profile, 'observatory')
  const burstObservatory = readTopLevelObject(profile, 'burstObservatory')
  const defaultSubjectSelector = collectBalancerSelectors(profile)

  const setTopLevelObject = (key: 'observatory' | 'burstObservatory', value: JsonObject | undefined) => {
    updateXrayProfile(p => setTopLevelValue(p, key, value))
  }

  const patchTopLevelObject = (key: 'observatory' | 'burstObservatory', patch: Record<string, JsonValue | undefined>) => {
    updateXrayProfile(p => {
      const current = mutableJsonObject(readTopLevelObject(p, key))
      for (const [field, value] of Object.entries(patch)) {
        if (value === undefined) delete current[field]
        else current[field] = value
      }
      return setTopLevelValue(p, key, compactJsonObject(current))
    })
  }

  const patchBurstPingConfig = (patch: Record<string, JsonValue | undefined>) => {
    updateXrayProfile(p => {
      const burst = mutableJsonObject(readTopLevelObject(p, 'burstObservatory'))
      const pingConfig = mutableJsonObject(readJsonObject(burst.pingConfig))
      for (const [field, value] of Object.entries(patch)) {
        if (value === undefined) delete pingConfig[field]
        else pingConfig[field] = value
      }
      burst.pingConfig = compactJsonObject(pingConfig) ?? {}
      return setTopLevelValue(p, 'burstObservatory', compactJsonObject(burst))
    })
  }

  const observationEnabled = observatory !== undefined || burstObservatory !== undefined
  const hasLeastPingBalancer = useMemo(() => profileHasLeastPingBalancer(profile), [profile])
  const observatoryDisabledReason = !hasLeastPingBalancer && observatory === undefined
    ? t('coreEditor.balancer.observatoryRequiresLeastPing', {
        defaultValue: 'Observatory needs at least one balancer with the leastPing strategy. Use Burst observatory until then.',
      })
    : null
  const activeObservationTab: ObservationTab =
    observationTab === 'observatory' && observatory === undefined && burstObservatory !== undefined
      ? 'burstObservatory'
      : observationTab === 'burstObservatory' && burstObservatory === undefined && observatory !== undefined
        ? 'observatory'
        : observationTab

  return (
    <div className="space-y-6">
      <div className="min-w-0 flex flex-col gap-3 rounded-md border border-border bg-muted/10 p-3 sm:flex-row sm:items-center sm:justify-between">
        <div className="min-w-0 flex items-center gap-3">
          <Eye className="h-4 w-4 shrink-0 text-muted-foreground" />
          <div className="min-w-0">
            <h3 className="text-sm font-medium">
              {t('coreEditor.balancer.observationSources', { defaultValue: 'Observation sources' })}
            </h3>
            <p className="mt-0.5 text-xs text-muted-foreground">
              {observationEnabled
                ? t('coreEditor.balancer.observationActiveSummary', {
                    defaultValue: 'Active sources are configured for balancer probes.',
                  })
                : t('coreEditor.balancer.observationInactiveSummary', {
                    defaultValue: 'No probes are configured. Most strategies need at least one source.',
                  })}
            </p>
          </div>
        </div>
        <div className="flex shrink-0 items-center gap-2">
          <Badge variant={observatory ? 'green' : 'outline'} className="h-5">
            {t('coreEditor.balancer.observatoryShort', { defaultValue: 'Observatory' })}: {observatory ? t('enabled', { defaultValue: 'On' }) : t('disabled', { defaultValue: 'Off' })}
          </Badge>
          <Badge variant={burstObservatory ? 'green' : 'outline'} className="h-5">
            {t('coreEditor.balancer.burstShort', { defaultValue: 'Burst' })}: {burstObservatory ? t('enabled', { defaultValue: 'On' }) : t('disabled', { defaultValue: 'Off' })}
          </Badge>
          <Button type="button" variant="outline" size="sm" onClick={() => setObservationDialogOpen(true)}>
            {t('configure', { defaultValue: 'Configure' })}
          </Button>
        </div>
      </div>

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
                      if (dialogMode === 'edit' && (v === 'leastPing' || v === 'leastLoad')) {
                        updateXrayProfile(p => ensureObservationForProfile(p, b.selector ?? []))
                      }
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

                {b.strategy?.type === 'leastLoad' ? (() => {
                  const settings = b.strategy?.settings
                  const costs = readLeastLoadCosts(settings)
                  return (
                    <div className="min-w-0 space-y-4 rounded-md border border-border bg-muted/10 p-3 sm:col-span-2">
                      <div className="grid gap-3 sm:grid-cols-3">
                        <div className="min-w-0 space-y-1.5">
                          <Label className="text-xs font-medium">
                            {t('coreEditor.balancer.expected', { defaultValue: 'Expected nodes' })}
                          </Label>
                          <Input
                            dir="ltr"
                            inputMode="numeric"
                            className="h-9 font-mono text-xs"
                            placeholder="2"
                            value={readNumberProperty(settings, 'expected')}
                            onChange={e => patchStrategySettings({ expected: parseOptionalNumber(e.target.value) })}
                          />
                        </div>
                        <div className="min-w-0 space-y-1.5">
                          <Label className="text-xs font-medium">
                            {t('coreEditor.balancer.maxRTT', { defaultValue: 'Max RTT' })}
                          </Label>
                          <Input
                            dir="ltr"
                            className="h-9 font-mono text-xs"
                            placeholder="1s"
                            value={readStringProperty(settings, 'maxRTT')}
                            onChange={e => patchStrategySettings({ maxRTT: e.target.value.trim() || undefined })}
                          />
                        </div>
                        <div className="min-w-0 space-y-1.5">
                          <Label className="text-xs font-medium">
                            {t('coreEditor.balancer.tolerance', { defaultValue: 'Tolerance' })}
                          </Label>
                          <Input
                            dir="ltr"
                            inputMode="decimal"
                            className="h-9 font-mono text-xs"
                            placeholder="0.01"
                            value={readNumberProperty(settings, 'tolerance')}
                            onChange={e => patchStrategySettings({ tolerance: parseOptionalNumber(e.target.value) })}
                          />
                        </div>
                      </div>

                      <div className="min-w-0 space-y-1.5">
                        <Label className="text-xs font-medium">
                          {t('coreEditor.balancer.baselines', { defaultValue: 'Baselines' })}
                        </Label>
                        <Textarea
                          dir="ltr"
                          className="min-h-20 font-mono text-xs"
                          placeholder="1s"
                          value={readStringListSetting(settings, 'baselines')}
                          onChange={e => patchStrategySettings({ baselines: parseStringListSetting(e.target.value) })}
                        />
                      </div>

                      <div className="min-w-0 space-y-2">
                        <div className="flex items-center justify-between gap-3">
                          <Label className="text-xs font-medium">
                            {t('coreEditor.balancer.costs', { defaultValue: 'Costs' })}
                          </Label>
                          <Button type="button" variant="outline" size="sm" onClick={addLeastLoadCost}>
                            <Plus className="h-4 w-4" />
                            {t('coreEditor.balancer.addCost', { defaultValue: 'Add cost' })}
                          </Button>
                        </div>
                        {costs.length === 0 ? (
                          <p className="text-xs text-muted-foreground">
                            {t('coreEditor.balancer.noCosts', { defaultValue: 'No outbound weights configured.' })}
                          </p>
                        ) : (
                          <div className="space-y-2">
                            {costs.map((row, index) => (
                              <div key={index} className="grid gap-2 rounded-md border border-border/70 bg-background/60 p-2 sm:grid-cols-[auto_minmax(0,1fr)_7rem_auto]">
                                <div className="flex items-center gap-2">
                                  <Switch
                                    checked={row.regexp}
                                    onCheckedChange={checked => patchLeastLoadCost(index, { regexp: checked })}
                                  />
                                  <span className="text-xs text-muted-foreground">
                                    {t('coreEditor.balancer.regexp', { defaultValue: 'Regexp' })}
                                  </span>
                                </div>
                                <Input
                                  dir="ltr"
                                  className="h-9 font-mono text-xs"
                                  placeholder="tag"
                                  value={row.match}
                                  onChange={e => patchLeastLoadCost(index, { match: e.target.value })}
                                />
                                <Input
                                  dir="ltr"
                                  inputMode="decimal"
                                  className="h-9 font-mono text-xs"
                                  placeholder="0.5"
                                  value={row.value}
                                  onChange={e => patchLeastLoadCost(index, { value: e.target.value })}
                                />
                                <Button
                                  type="button"
                                  variant="ghost"
                                  size="icon-md"
                                  aria-label={t('delete', { defaultValue: 'Delete' })}
                                  onClick={() => removeLeastLoadCost(index)}
                                >
                                  <Trash2 className="h-4 w-4" />
                                </Button>
                              </div>
                            ))}
                          </div>
                        )}
                      </div>
                    </div>
                  )
                })() : null}
              </div>
            </form>
          </Form>
        )}
      </CoreEditorFormDialog>

      <Dialog open={observationDialogOpen} onOpenChange={setObservationDialogOpen}>
        <DialogContent dir={dir} onOpenAutoFocus={e => e.preventDefault()} className="h-auto w-full max-w-2xl gap-4">
          <DialogHeader>
            <DialogTitle className="flex items-center gap-2">
              <Eye className="h-5 w-5 shrink-0" />
              {t('coreEditor.balancer.observationSources', { defaultValue: 'Observation sources' })}
            </DialogTitle>
            <DialogDescription className="text-pretty">
              {t('coreEditor.balancer.observationSourcesHint', {
                defaultValue: 'This editor can keep both top-level observation objects when a config uses them. Most setups enable one; leastPing and leastLoad need observation data.',
              })}
            </DialogDescription>
          </DialogHeader>

          <div className="-me-4 max-h-[75dvh] space-y-4 overflow-y-auto overscroll-contain px-2 pe-4">
            <div className="grid gap-3 sm:grid-cols-2">
              <TooltipProvider delayDuration={200}>
                <div className={cn('flex min-w-0 items-start justify-between gap-3 rounded-md border border-border/70 bg-background/60 px-3 py-3', observatory && 'border-primary/30 bg-primary/5', observatoryDisabledReason && 'opacity-70')}>
                  <div className="min-w-0">
                    <Label className="text-xs font-medium">
                      {t('coreEditor.balancer.observatory', { defaultValue: 'Observatory' })}
                    </Label>
                    <p className="mt-1 text-xs leading-relaxed text-muted-foreground">
                      {t('coreEditor.balancer.observatoryHint', {
                        defaultValue: 'Fixed-interval background HTTP probes.',
                      })}
                    </p>
                  </div>
                  {observatoryDisabledReason ? (
                    <Tooltip>
                      <TooltipTrigger asChild>
                        <span className="mt-0.5 shrink-0">
                          <Switch
                            className=""
                            checked={false}
                            disabled
                            onCheckedChange={() => {}}
                          />
                        </span>
                      </TooltipTrigger>
                      <TooltipContent side="top" className="max-w-xs text-xs">
                        {observatoryDisabledReason}
                      </TooltipContent>
                    </Tooltip>
                  ) : (
                    <Switch
                      className="mt-0.5 shrink-0"
                      checked={observatory !== undefined}
                      onCheckedChange={checked => {
                        setTopLevelObject('observatory', checked ? defaultObservatory(defaultSubjectSelector) : undefined)
                        if (checked) setObservationTab('observatory')
                      }}
                    />
                  )}
                </div>
              </TooltipProvider>

              <div className={cn('flex min-w-0 items-start justify-between gap-3 rounded-md border border-border/70 bg-background/60 px-3 py-3', burstObservatory && 'border-primary/30 bg-primary/5')}>
                <div className="min-w-0">
                  <Label className="text-xs font-medium">
                    {t('coreEditor.balancer.burstObservatory', { defaultValue: 'Burst observatory' })}
                  </Label>
                  <p className="mt-1 text-xs leading-relaxed text-muted-foreground">
                    {t('coreEditor.balancer.burstObservatoryHint', {
                      defaultValue: 'Randomized probes configured through pingConfig.',
                    })}
                  </p>
                </div>
                <Switch
                  className="mt-0.5 shrink-0"
                  checked={burstObservatory !== undefined}
                  onCheckedChange={checked => {
                    setTopLevelObject('burstObservatory', checked ? defaultBurstObservatory(defaultSubjectSelector) : undefined)
                    if (checked) setObservationTab('burstObservatory')
                  }}
                />
              </div>
            </div>

            {observationEnabled ? (
              <Tabs value={activeObservationTab} onValueChange={value => setObservationTab(value as ObservationTab)} className="min-w-0">
                <TabsList className="grid w-full grid-cols-2 sm:w-[420px]">
                  <TabsTrigger value="observatory" disabled={observatory === undefined}>
                    {t('coreEditor.balancer.observatory', { defaultValue: 'Observatory' })}
                  </TabsTrigger>
                  <TabsTrigger value="burstObservatory" disabled={burstObservatory === undefined}>
                    {t('coreEditor.balancer.burstObservatory', { defaultValue: 'Burst observatory' })}
                  </TabsTrigger>
                </TabsList>

                {observatory ? (
                  <TabsContent value="observatory" className="mt-4 space-y-3">
                    <div className="space-y-1.5">
                      <Label className="text-xs font-medium">
                        {t('coreEditor.balancer.subjectSelector', { defaultValue: 'Subject selector' })}
                      </Label>
                      <StringTagPicker
                        mode="multi"
                        options={profileTagOptions.outboundTags}
                        valueMulti={readStringArrayProperty(observatory, 'subjectSelector')}
                        onChangeMulti={next => patchTopLevelObject('observatory', { subjectSelector: next })}
                        placeholder={t('coreEditor.balancer.subjectSelectorPlaceholder', {
                          defaultValue: 'Select outbound tag prefixes...',
                        })}
                        clearAllLabel={t('coreEditor.balancer.clearSelectors', { defaultValue: 'Clear all' })}
                        addButtonLabel={t('coreEditor.balancer.addOutboundTag', { defaultValue: 'Add tag' })}
                      />
                    </div>
                    <div className="grid gap-3 sm:grid-cols-2">
                      <div className="space-y-1.5">
                        <Label className="text-xs font-medium">probeURL</Label>
                        <Input
                          dir="ltr"
                          className="h-9 font-mono text-xs"
                          value={readStringProperty(observatory, 'probeURL', 'probeUrl')}
                          onChange={e => patchTopLevelObject('observatory', { probeURL: e.target.value.trim() || undefined, probeUrl: undefined })}
                          placeholder="https://www.google.com/generate_204"
                        />
                      </div>
                      <div className="space-y-1.5">
                        <Label className="text-xs font-medium">probeInterval</Label>
                        <Input
                          dir="ltr"
                          className="h-9 font-mono text-xs"
                          value={readStringProperty(observatory, 'probeInterval')}
                          onChange={e => patchTopLevelObject('observatory', { probeInterval: e.target.value.trim() || undefined })}
                          placeholder="10m"
                        />
                      </div>
                      <div className="flex min-w-0 items-center justify-between gap-3 rounded-md border border-border/70 bg-background/60 px-3 py-2 sm:col-span-2">
                        <Label className="min-w-0 text-xs font-medium">enableConcurrency</Label>
                        <Switch
                          checked={readBooleanProperty(observatory, 'enableConcurrency')}
                          onCheckedChange={checked => patchTopLevelObject('observatory', { enableConcurrency: checked })}
                        />
                      </div>
                    </div>
                  </TabsContent>
                ) : null}

                {burstObservatory ? (() => {
                  const pingConfig = readJsonObject(burstObservatory.pingConfig)
                  return (
                    <TabsContent value="burstObservatory" className="mt-4 space-y-3">
                      <div className="space-y-1.5">
                        <Label className="text-xs font-medium">
                          {t('coreEditor.balancer.subjectSelector', { defaultValue: 'Subject selector' })}
                        </Label>
                        <StringTagPicker
                          mode="multi"
                          options={profileTagOptions.outboundTags}
                          valueMulti={readStringArrayProperty(burstObservatory, 'subjectSelector')}
                          onChangeMulti={next => patchTopLevelObject('burstObservatory', { subjectSelector: next })}
                          placeholder={t('coreEditor.balancer.subjectSelectorPlaceholder', {
                            defaultValue: 'Select outbound tag prefixes...',
                          })}
                          clearAllLabel={t('coreEditor.balancer.clearSelectors', { defaultValue: 'Clear all' })}
                          addButtonLabel={t('coreEditor.balancer.addOutboundTag', { defaultValue: 'Add tag' })}
                        />
                      </div>
                      <div className="grid gap-3 sm:grid-cols-2">
                        <div className="space-y-1.5 sm:col-span-2">
                          <Label className="text-xs font-medium">destination</Label>
                          <Input
                            dir="ltr"
                            className="h-9 font-mono text-xs"
                            value={readStringProperty(pingConfig, 'destination')}
                            onChange={e => patchBurstPingConfig({ destination: e.target.value.trim() || undefined })}
                            placeholder="https://connectivitycheck.gstatic.com/generate_204"
                          />
                        </div>
                        <div className="space-y-1.5 sm:col-span-2">
                          <Label className="text-xs font-medium">connectivity</Label>
                          <Input
                            dir="ltr"
                            className="h-9 font-mono text-xs"
                            value={readStringProperty(pingConfig, 'connectivity')}
                            onChange={e => patchBurstPingConfig({ connectivity: e.target.value.trim() })}
                            placeholder="http://connectivitycheck.platform.hicloud.com/generate_204"
                          />
                        </div>
                        <div className="space-y-1.5">
                          <Label className="text-xs font-medium">interval</Label>
                          <Input
                            dir="ltr"
                            className="h-9 font-mono text-xs"
                            value={readStringProperty(pingConfig, 'interval')}
                            onChange={e => patchBurstPingConfig({ interval: e.target.value.trim() || undefined })}
                            placeholder="1m"
                          />
                        </div>
                        <div className="space-y-1.5">
                          <Label className="text-xs font-medium">timeout</Label>
                          <Input
                            dir="ltr"
                            className="h-9 font-mono text-xs"
                            value={readStringProperty(pingConfig, 'timeout')}
                            onChange={e => patchBurstPingConfig({ timeout: e.target.value.trim() || undefined })}
                            placeholder="5s"
                          />
                        </div>
                        <div className="space-y-1.5">
                          <Label className="text-xs font-medium">sampling</Label>
                          <Input
                            dir="ltr"
                            inputMode="numeric"
                            className="h-9 font-mono text-xs"
                            value={readNumberProperty(pingConfig, 'sampling')}
                            onChange={e => patchBurstPingConfig({ sampling: parseOptionalNumber(e.target.value) })}
                            placeholder="10"
                          />
                        </div>
                        <div className="space-y-1.5">
                          <Label className="text-xs font-medium">httpMethod</Label>
                          <Select
                            value={readStringProperty(pingConfig, 'httpMethod') || 'HEAD'}
                            onValueChange={value => patchBurstPingConfig({ httpMethod: value })}
                          >
                            <SelectTrigger className="h-9" dir="ltr">
                              <SelectValue />
                            </SelectTrigger>
                            <SelectContent>
                              {['HEAD', 'GET', 'POST', 'PUT', 'PATCH', 'DELETE', 'OPTIONS'].map(method => (
                                <SelectItem key={method} value={method}>
                                  {method}
                                </SelectItem>
                              ))}
                            </SelectContent>
                          </Select>
                        </div>
                      </div>
                    </TabsContent>
                  )
                })() : null}
              </Tabs>
            ) : (
              <div className="rounded-md border border-dashed border-border px-3 py-4 text-xs text-muted-foreground">
                {t('coreEditor.balancer.noObservationSources', {
                  defaultValue: 'Enable an observation source here when using leastPing or leastLoad, or when random/roundRobin should filter unavailable outbounds.',
                })}
              </div>
            )}
          </div>

          <div className="flex justify-end gap-2">
            <Button type="button" variant="outline" onClick={() => setObservationDialogOpen(false)}>
              {t('close', { defaultValue: 'Close' })}
            </Button>
          </div>
        </DialogContent>
      </Dialog>

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
