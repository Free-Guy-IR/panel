import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Checkbox } from '@/components/ui/checkbox'
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
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from '@/components/ui/collapsible'
import { Sheet, SheetClose, SheetContent, SheetFooter, SheetHeader, SheetTitle } from '@/components/ui/sheet'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Skeleton } from '@/components/ui/skeleton'
import { Switch } from '@/components/ui/switch'
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from '@/components/ui/tooltip'
import { useAdmin } from '@/hooks/use-admin'
import useDirDetection from '@/hooks/use-dir-detection'
import { cn } from '@/lib/utils'
import type { AdminDetails } from '@/service/api'
import { fetcher } from '@/service/http'
import { isOwner } from '@/utils/rbac'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  Baby,
  Bot,
  ChevronDown,
  ChevronRight,
  Clapperboard,
  Cloud,
  Dices,
  Gamepad2,
  Globe,
  Heart,
  Info,
  Laptop,
  Layers,
  Loader2,
  Megaphone,
  MessageCircle,
  Plus,
  RotateCw,
  Search,
  Server,
  ShieldAlert,
  ShieldBan,
  ShieldCheck,
  ShoppingBag,
  Smartphone,
  Trash2,
  Unlock,
  Users,
  X,
  Zap,
} from 'lucide-react'
import { type ReactNode, useCallback, useEffect, useMemo, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { toast } from 'sonner'
import {
  asCapabilityReason,
  useCapabilityIndex,
  useCapabilityReasonText,
  useContentFilterCapability,
  type CapabilityReason,
} from './content-filter-capability'

type CatalogService = { key: string; label: string; geosite: string | null; domains: number; icon?: string }
type CatalogGroup = { key: string; geosite: string | null; domains: number; services: CatalogService[] }
type ListEntry = { key: string; label: string; domains: number }
type ListGroup = { key: string; domains: number; entries: ListEntry[] }
type Catalog = { groups: CatalogGroup[]; protection: ListEntry[]; lists: ListGroup[] }
type Profile = {
  id: number
  name: string
  categories: string[]
  allow_list: string[]
  block_list: string[]
  strict_mode: boolean
  note: string | null
}
type TargetInbound = { tag: string; protocol: string; filterable: boolean; reason?: string | null }
type TargetNode = {
  id: number
  name: string
  status: string
  core_config_id: number
  routing_service: boolean
  reason?: string | null
  shares_core_with?: number[]
  pinned_stays_here?: boolean
  would_also_affect?: number[]
  scope_note?: string | null
  inbounds: TargetInbound[]
}
type Delivery = 'core' | 'live'
type OutcomeStatus = 'applied' | 'failed' | 'disabled' | 'skipped'
type ReloadPrompt = {
  reason: string
  message: string
  node_ids: number[]
  inbound_tags: string[]
  confirm_with: string
}
type AssignmentOutcome = {
  node_id: number | null
  inbound_tag: string
  created: boolean
  enforced: boolean
  detail?: string | null
  delivery?: Delivery | null
  status?: OutcomeStatus | null
  advisories?: string[]
  advisory_note?: string
  reason?: string | null
  reload?: ReloadPrompt | null
}
type BulkResult = {
  created: number
  updated?: number
  applied?: number
  failed?: number
  disabled?: number
  skipped: number
  outcomes: AssignmentOutcome[]
}

type ApplyBody = { profile_id: number; node_ids: number[]; inbound_tags: string[] }
type RestartRequest =
  | { kind: 'saveProfile'; profile: Profile }
  | { kind: 'deleteProfile'; id: number }
  | { kind: 'deleteAssignment'; id: number }
  | { kind: 'applyTargets'; body: ApplyBody }
type PendingRestart = { prompt: ReloadPrompt; request: RestartRequest }

type Assignment = {
  id: number
  profile_id: number
  node_id: number | null
  inbound_tag: string
  is_enabled: boolean
  enforced: boolean
  last_checked_at: string | null
  last_error: string | null
  delivery?: Delivery | null
}

type FleetEndpoint = {
  tag: string
  protocol: string
  nodeIds: number[]
  blockedNodeIds: number[]
  blockedReason: string | null
}

type SkippedNode = { id: number; reason: CapabilityReason | null; lost: number; kept: number }
type SkippedEndpoint = { tag: string; nodes: number; reason: CapabilityReason | null }
type UnfilterableEndpoint = { tag: string; targets: number; reason: CapabilityReason | null }

type ReachRow = {
  nodeId: number
  name: string
  status: string
  tags: string[]
  delivery: Delivery | null
  peers: { id: number; name: string }[]
  note: string | null
}

const BASE = '/api/content-filter'

const GROUP_ICON: Record<string, typeof ShieldBan> = {
  security: ShieldAlert,
  bypass: Unlock,
  devices: Smartphone,
  content: Clapperboard,
  regional: Globe,
  social_network: Users,
  messenger: MessageCircle,
  gaming: Gamepad2,
  gambling: Dices,
  dating: Heart,
  ai: Bot,
  shopping: ShoppingBag,
  hosting: Cloud,
  cdn: Cloud,
  software: Laptop,
  privacy: ShieldCheck,
  adult: Baby,
  social: Users,
  games: Gamepad2,
  streaming: Clapperboard,
  ads: Megaphone,
}

const GROUP_TONE = new Map<string, string>([
  ['security', 'text-rose-600 dark:text-rose-400'],
  ['bypass', 'text-amber-600 dark:text-amber-400'],
  ['devices', 'text-sky-600 dark:text-sky-400'],
  ['content', 'text-violet-600 dark:text-violet-400'],
  ['regional', 'text-teal-600 dark:text-teal-400'],
  ['social_network', 'text-sky-600 dark:text-sky-400'],
  ['messenger', 'text-cyan-600 dark:text-cyan-400'],
  ['gaming', 'text-violet-600 dark:text-violet-400'],
  ['gambling', 'text-red-600 dark:text-red-400'],
  ['dating', 'text-pink-600 dark:text-pink-400'],
  ['ai', 'text-indigo-600 dark:text-indigo-400'],
  ['shopping', 'text-orange-600 dark:text-orange-400'],
  ['hosting', 'text-slate-600 dark:text-slate-400'],
  ['cdn', 'text-slate-600 dark:text-slate-400'],
  ['software', 'text-slate-600 dark:text-slate-400'],
  ['privacy', 'text-emerald-600 dark:text-emerald-400'],
  ['adult', 'text-rose-600 dark:text-rose-400'],
  ['social', 'text-sky-600 dark:text-sky-400'],
  ['games', 'text-violet-600 dark:text-violet-400'],
  ['streaming', 'text-amber-600 dark:text-amber-400'],
  ['ads', 'text-emerald-600 dark:text-emerald-400'],
])

const STATUS_DOT: Record<string, string> = {
  connected: 'bg-emerald-500',
  connecting: 'bg-amber-500',
  error: 'bg-destructive',
  disabled: 'bg-muted-foreground/40',
  limited: 'bg-amber-500',
}

function useCatalog() {
  return useQuery({
    queryKey: ['content-filter', 'catalog'],
    queryFn: () => fetcher<Catalog>(`${BASE}/catalog`),
    staleTime: Infinity,
  })
}

function useProfiles() {
  return useQuery({ queryKey: ['content-filter', 'profiles'], queryFn: () => fetcher<Profile[]>(`${BASE}/profiles`) })
}

function useTargets() {
  return useQuery({ queryKey: ['content-filter', 'targets'], queryFn: () => fetcher<TargetNode[]>(`${BASE}/targets`) })
}

function useAssignments() {
  return useQuery({
    queryKey: ['content-filter', 'assignments'],
    queryFn: () => fetcher<Assignment[]>(`${BASE}/assignments`),
  })
}

function sameList(a: readonly string[] | readonly number[], b: readonly string[] | readonly number[]): boolean {
  return a.length === b.length && a.every((v, i) => v === b[i])
}

function sortedCopy<T>(values: readonly T[]): T[] {
  return [...values].sort()
}

function errorText(e: unknown, fallback: string): string {
  const detail = (e as { data?: { detail?: unknown } })?.data?.detail
  if (typeof detail === 'string') return detail
  if (Array.isArray(detail) && detail.length) {
    const first = detail[0] as { msg?: string }
    if (first?.msg) return first.msg
  }
  return (e as Error)?.message || fallback
}

function reloadPromptOf(e: unknown): ReloadPrompt | null {
  const detail = (e as { data?: { detail?: unknown } })?.data?.detail
  if (!detail || typeof detail !== 'object' || Array.isArray(detail)) return null
  const raw = detail as Record<string, unknown>
  if (raw.reason !== 'reload_required') return null
  return {
    reason: 'reload_required',
    message: typeof raw.message === 'string' ? raw.message : '',
    node_ids: Array.isArray(raw.node_ids) ? raw.node_ids.filter((x): x is number => typeof x === 'number') : [],
    inbound_tags: Array.isArray(raw.inbound_tags) ? raw.inbound_tags.filter((x): x is string => typeof x === 'string') : [],
    confirm_with: typeof raw.confirm_with === 'string' ? raw.confirm_with : 'confirm_restart',
  }
}

function mergePrompts(prompts: ReloadPrompt[]): ReloadPrompt | null {
  if (!prompts.length) return null
  const nodes = new Set<number>()
  const tags = new Set<string>()
  for (const prompt of prompts) {
    prompt.node_ids.forEach(id => nodes.add(id))
    prompt.inbound_tags.forEach(tag => tags.add(tag))
  }
  return {
    reason: 'reload_required',
    message: prompts.find(prompt => prompt.message)?.message ?? '',
    node_ids: [...nodes].sort((a, b) => a - b),
    inbound_tags: [...tags].sort(),
    confirm_with: prompts[0].confirm_with || 'confirm_restart',
  }
}

function ScopeLine({
  nodes,
  endpoints,
  fleetWide,
  spill,
  skippedByNode,
  skippedByEndpoint,
  pickedNodes,
  pickedTags,
}: {
  nodes: number
  endpoints: number
  fleetWide: boolean
  spill: number
  skippedByNode: number
  skippedByEndpoint: number
  pickedNodes: number
  pickedTags: number
}) {
  const { t } = useTranslation()
  if (!pickedNodes && !pickedTags) {
    return (
      <p className="rounded-lg border border-dashed px-3 py-2.5 text-sm text-muted-foreground">
        {t('contentFilter.scopeEmpty', { defaultValue: 'Nothing is picked yet, so this filter would go nowhere.' })}
      </p>
    )
  }
  return (
    <div className="flex flex-wrap items-baseline gap-x-2 gap-y-1 rounded-lg border border-primary/30 bg-primary/[0.04] px-3 py-2.5">
      <span className="text-sm text-foreground">
        {t('contentFilter.scopeReach', {
          endpoints,
          nodes: nodes + spill,
          defaultValue: 'Covers {{endpoints}} endpoints on {{nodes}} nodes.',
        })}
      </span>
      {spill > 0 ? (
        <span className="text-xs font-medium text-amber-700 dark:text-amber-400">
          {t('contentFilter.scopeSpill', {
            spill,
            defaultValue: '{{spill}} of them are included only because they share a configuration.',
          })}
        </span>
      ) : null}
      {skippedByEndpoint > 0 ? (
        <span className="break-words text-xs font-medium text-amber-700 dark:text-amber-400">
          {t('contentFilter.scopeSkippedEndpoints', {
            count: skippedByEndpoint,
            defaultValue: '{{count}} of them are skipped because no node anywhere can filter that endpoint.',
          })}
        </span>
      ) : null}
      {skippedByNode > 0 ? (
        <span className="break-words text-xs font-medium text-amber-700 dark:text-amber-400">
          {t('contentFilter.scopeSkippedNodes', {
            count: skippedByNode,
            defaultValue: '{{count}} of them are skipped because the node itself cannot enforce this filter.',
          })}
        </span>
      ) : null}
      {fleetWide ? (
        <span className="text-xs text-muted-foreground">
          {t('contentFilter.scopeFleetWide', {
            defaultValue: 'Any node that starts carrying them later is covered too.',
          })}
        </span>
      ) : null}
    </div>
  )
}

function PickerHeader({
  title,
  picked,
  onAll,
  onNone,
  allDisabled,
  noneDisabled,
}: {
  title: string
  picked: number
  onAll: () => void
  onNone: () => void
  allDisabled: boolean
  noneDisabled: boolean
}) {
  const { t } = useTranslation()
  return (
    <div className="flex flex-wrap items-center justify-between gap-2">
      <div className="flex min-w-0 items-center gap-2">
        <h4 className="truncate text-sm font-semibold text-foreground">{title}</h4>
        {picked > 0 ? (
          <Badge variant="secondary" className="h-5 shrink-0 px-1.5 text-xs font-normal tabular-nums">
            {picked}
          </Badge>
        ) : null}
      </div>
      <div className="inline-flex shrink-0 overflow-hidden rounded-md border">
        <Button
          type="button"
          variant="ghost"
          size="sm"
          className="h-7 rounded-none px-2.5 text-xs"
          disabled={allDisabled}
          onClick={onAll}
        >
          {t('contentFilter.selectAll', { defaultValue: 'All' })}
        </Button>
        <span aria-hidden className="w-px self-stretch bg-border" />
        <Button
          type="button"
          variant="ghost"
          size="sm"
          className="h-7 rounded-none px-2.5 text-xs"
          disabled={noneDisabled}
          onClick={onNone}
        >
          {t('contentFilter.selectNone', { defaultValue: 'None' })}
        </Button>
      </div>
    </div>
  )
}

function SectionHeading({ title, hint, action }: { title: string; hint?: string; action?: ReactNode }) {
  return (
    <div className="flex items-baseline gap-3 border-b pb-2 pt-2">
      <h3 className="shrink-0 text-sm font-semibold uppercase tracking-wide text-muted-foreground">{title}</h3>
      {hint ? <p className="truncate text-xs text-muted-foreground/80">{hint}</p> : null}
      {action ? <div className="ms-auto shrink-0 self-center">{action}</div> : null}
    </div>
  )
}


function DeliveryBadge({ delivery }: { delivery?: Delivery | null }) {
  const { t } = useTranslation()
  if (delivery !== 'core' && delivery !== 'live') return null
  const live = delivery === 'live'
  const Icon = live ? Zap : Layers
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <button
          type="button"
          className={cn(
            'inline-flex h-5 shrink-0 items-center gap-1 rounded-full border px-2 text-[11px] font-normal transition-colors',
            live
              ? 'border-sky-500/40 bg-sky-500/10 text-sky-700 hover:bg-sky-500/15 dark:text-sky-400'
              : 'border-border bg-muted text-muted-foreground hover:bg-muted/70',
          )}
        >
          <Icon className="size-3" />
          {live
            ? t('contentFilter.deliveryLive', { defaultValue: 'This node only' })
            : t('contentFilter.deliveryCore', { defaultValue: 'Shared config' })}
        </button>
      </TooltipTrigger>
      <TooltipContent className="max-w-[260px] leading-snug">
        {live
          ? t('contentFilter.deliveryLiveHint', {
              defaultValue:
                'Sent straight to this one node and put back automatically after it restarts. No other node is touched.',
            })
          : t('contentFilter.deliveryCoreHint', {
              defaultValue:
                'Written into the configuration file this node uses, so it survives a restart on its own — and every node sharing that configuration enforces it too.',
            })}
      </TooltipContent>
    </Tooltip>
  )
}

function AdvisoryList({ advisories, note }: { advisories?: string[]; note?: string }) {
  const { t } = useTranslation()
  const [open, setOpen] = useState(false)
  const all = advisories ?? []
  const lines = all.filter(line => line.trim().length > 0)
  if (!all.length) return null
  return (
    <Collapsible
      open={open}
      onOpenChange={setOpen}
      className="mt-2 rounded-lg border border-amber-500/40 bg-amber-500/[0.07] px-3 py-2"
    >
      <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
        <Info className="size-3.5 shrink-0 text-amber-600 dark:text-amber-400" />
        <span className="text-xs font-medium text-amber-700 dark:text-amber-400">
          {t('contentFilter.advisoriesTitle', { defaultValue: 'Rules already on this core' })}
        </span>
        <span className="text-[11px] tabular-nums text-muted-foreground">
          {t('contentFilter.advisoriesCount', { count: lines.length, defaultValue: '{{count}} notes' })}
        </span>
        <CollapsibleTrigger asChild>
          <Button variant="ghost" size="sm" className="ms-auto h-6 shrink-0 gap-1 px-2 text-[11px]">
            <ChevronDown className={cn('size-3.5 transition-transform', open && 'rotate-180')} />
            {open
              ? t('contentFilter.advisoriesHide', { defaultValue: 'Hide' })
              : t('contentFilter.advisoriesShow', { defaultValue: 'Show' })}
          </Button>
        </CollapsibleTrigger>
      </div>
      <CollapsibleContent className="mt-2 space-y-2">
        {note && note.trim() ? <p className="break-words text-[11px] leading-snug text-muted-foreground">{note}</p> : null}
        {lines.length ? (
          <ul className="space-y-1">
            {lines.map((line, index) => (
              <li key={`${index}:${line}`} className="flex gap-2 text-xs leading-snug">
                <span aria-hidden className="mt-1.5 size-1 shrink-0 rounded-full bg-amber-500" />
                <span className="min-w-0 break-words">{line}</span>
              </li>
            ))}
          </ul>
        ) : (
          <p className="text-xs leading-snug text-muted-foreground">
            {t('contentFilter.advisoriesEmpty', { defaultValue: 'Nothing else to look at here.' })}
          </p>
        )}
      </CollapsibleContent>
    </Collapsible>
  )
}

type RollupState = 'reload' | 'failed' | 'unconfirmed' | 'skipped' | 'disabled' | 'applied'
type RollupEndpoint = { tag: string; state: RollupState; outcome: AssignmentOutcome }
type RollupGroup = { state: RollupState; items: RollupEndpoint[] }
type RollupReload = { message: string; tags: string[]; nodeIds: number[]; withdraw: boolean }
type NodeRollup = {
  key: string
  nodeId: number | null
  name: string
  status: string | null
  delivery: Delivery | null
  endpoints: RollupEndpoint[]
  groups: RollupGroup[]
  worst: RollupState
  sole: RollupState | null
  attention: RollupEndpoint[]
  advisories: string[]
  advisoryNote: string | undefined
  reload: RollupReload | null
}

const ROLLUP_ORDER: RollupState[] = ['reload', 'failed', 'unconfirmed', 'skipped', 'disabled', 'applied']

const ROLLUP_ROW: Record<RollupState, string> = {
  reload: 'border-s-amber-500 bg-amber-500/[0.07]',
  failed: 'border-s-destructive bg-destructive/[0.07]',
  unconfirmed: 'border-s-destructive bg-destructive/[0.07]',
  skipped: 'border-s-amber-500 bg-amber-500/[0.07]',
  disabled: 'border-s-muted-foreground/40 bg-muted/40',
  applied: 'border-s-emerald-500/50 bg-muted/30',
}

const ROLLUP_CHIP: Record<RollupState, string> = {
  reload: 'border-amber-500/40 bg-amber-500/10 text-amber-700 dark:text-amber-400',
  failed: 'border-destructive/40 bg-destructive/10 text-destructive',
  unconfirmed: 'border-destructive/40 bg-destructive/10 text-destructive',
  skipped: 'border-amber-500/40 bg-amber-500/10 text-amber-700 dark:text-amber-400',
  disabled: 'border-border bg-muted text-muted-foreground',
  applied: 'border-emerald-500/40 bg-emerald-500/10 text-emerald-700 dark:text-emerald-400',
}

const ROLLUP_TAG: Record<RollupState, string> = {
  reload: 'border-amber-500/40 text-amber-700 dark:text-amber-400',
  failed: 'border-destructive/40 text-destructive',
  unconfirmed: 'border-destructive/40 text-destructive',
  skipped: 'border-amber-500/40 text-amber-700 dark:text-amber-400',
  disabled: 'border-border text-muted-foreground',
  applied: 'border-border text-foreground',
}

const ROLLUP_TEXT: Record<RollupState, string> = {
  reload: 'text-amber-700 dark:text-amber-400',
  failed: 'text-destructive',
  unconfirmed: 'text-destructive',
  skipped: 'text-amber-700 dark:text-amber-400',
  disabled: 'text-muted-foreground',
  applied: 'text-muted-foreground',
}

function outcomeState(outcome: AssignmentOutcome): RollupState {
  if (outcome.reload) return 'reload'
  const status = outcome.status
  if (status === 'applied' || (!status && outcome.created && outcome.enforced)) return 'applied'
  if (status === 'failed') return 'failed'
  if (status === 'disabled') return 'disabled'
  if (status === 'skipped' || (!status && !outcome.created)) return 'skipped'
  return 'unconfirmed'
}

type RollupDraft = {
  nodeId: number | null
  endpoints: RollupEndpoint[]
  deliveries: Set<Delivery>
  advisories: string[]
  advisoryNote: string | undefined
  reloadTags: Set<string>
  reloadNodes: Set<number>
  reloadMessage: string
  reloadWithdraw: boolean
  hasReload: boolean
}

function buildNodeRollups(
  outcomes: readonly AssignmentOutcome[],
  nodeName: (id: number) => string,
  nodeStatus: (id: number) => string | null,
  fleetName: string,
  wholeNodeName: string,
): NodeRollup[] {
  const drafts = new Map<string, RollupDraft>()
  for (const outcome of outcomes) {
    const key = outcome.node_id === null ? 'fleet' : String(outcome.node_id)
    const draft: RollupDraft = drafts.get(key) ?? {
      nodeId: outcome.node_id,
      endpoints: [],
      deliveries: new Set<Delivery>(),
      advisories: [],
      advisoryNote: undefined,
      reloadTags: new Set<string>(),
      reloadNodes: new Set<number>(),
      reloadMessage: '',
      reloadWithdraw: true,
      hasReload: false,
    }
    draft.endpoints.push({ tag: outcome.inbound_tag || wholeNodeName, state: outcomeState(outcome), outcome })
    if (outcome.delivery === 'core' || outcome.delivery === 'live') draft.deliveries.add(outcome.delivery)
    for (const line of outcome.advisories ?? []) {
      const text = line.trim()
      if (text && !draft.advisories.includes(text)) draft.advisories.push(text)
    }
    if (!draft.advisoryNote && outcome.advisory_note && outcome.advisory_note.trim()) {
      draft.advisoryNote = outcome.advisory_note
    }
    if (outcome.reload) {
      draft.hasReload = true
      outcome.reload.inbound_tags.forEach(tag => draft.reloadTags.add(tag))
      outcome.reload.node_ids.forEach(id => draft.reloadNodes.add(id))
      if (!draft.reloadMessage && outcome.reload.message) draft.reloadMessage = outcome.reload.message
      if (outcome.status !== 'disabled') draft.reloadWithdraw = false
    }
    drafts.set(key, draft)
  }

  const rollups: NodeRollup[] = []
  for (const [key, draft] of drafts) {
    const present = ROLLUP_ORDER.filter(state => draft.endpoints.some(item => item.state === state))
    const worst = present[0] ?? 'applied'
    rollups.push({
      key,
      nodeId: draft.nodeId,
      name: draft.nodeId === null ? fleetName : nodeName(draft.nodeId),
      status: draft.nodeId === null ? null : nodeStatus(draft.nodeId),
      delivery: draft.deliveries.size === 1 ? [...draft.deliveries][0] : null,
      endpoints: draft.endpoints,
      groups: present.map(state => ({ state, items: draft.endpoints.filter(item => item.state === state) })),
      worst,
      sole: present.length === 1 ? worst : null,
      attention: ROLLUP_ORDER.flatMap(state =>
        state === 'applied' ? [] : draft.endpoints.filter(item => item.state === state),
      ),
      advisories: draft.advisories,
      advisoryNote: draft.advisoryNote,
      reload: draft.hasReload
        ? {
            message: draft.reloadMessage,
            tags: [...draft.reloadTags].sort(),
            nodeIds: [...draft.reloadNodes].sort((a, b) => a - b),
            withdraw: draft.reloadWithdraw,
          }
        : null,
    })
  }
  rollups.sort((a, b) => {
    const rank = ROLLUP_ORDER.indexOf(a.worst) - ROLLUP_ORDER.indexOf(b.worst)
    return rank !== 0 ? rank : a.name.localeCompare(b.name)
  })
  return rollups
}

function NodeStatusDot({ status }: { status: string }) {
  const { t } = useTranslation()
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <button
          type="button"
          className={cn('size-2 shrink-0 rounded-full', STATUS_DOT[status] ?? 'bg-muted-foreground/40')}
          aria-label={t('contentFilter.nodeStatus', { status, defaultValue: 'Status: {{status}}' })}
        />
      </TooltipTrigger>
      <TooltipContent>{t('contentFilter.nodeStatus', { status, defaultValue: 'Status: {{status}}' })}</TooltipContent>
    </Tooltip>
  )
}

function useOutcomeSkipReason() {
  const reasonText = useCapabilityReasonText()
  return useCallback(
    (outcome: AssignmentOutcome): string | null => {
      if (outcome.reload) return null
      if (outcome.status === 'applied' || (!outcome.status && outcome.created && outcome.enforced)) return null
      const known = asCapabilityReason(outcome.reason)
      if (known) return reasonText(known)
      return typeof outcome.reason === 'string' && outcome.reason.trim() ? outcome.reason.trim() : null
    },
    [reasonText],
  )
}

function detailLines(items: readonly RollupEndpoint[], pick: (outcome: AssignmentOutcome) => string | null) {
  const found = new Map<string, string[]>()
  for (const item of items) {
    const value = pick(item.outcome)
    if (!value) continue
    const tags = found.get(value) ?? []
    tags.push(item.tag)
    found.set(value, tags)
  }
  return [...found.entries()].map(([text, tags]) => ({ text, tags, all: tags.length === items.length }))
}

function RollupCountChip({ state, count, sole }: { state: RollupState; count: number; sole: boolean }) {
  const { t } = useTranslation()
  const label = () => {
    switch (state) {
      case 'reload':
        return sole
          ? t('contentFilter.rollupAllReload', { count, defaultValue: 'All {{count}} endpoints need a restart' })
          : t('contentFilter.rollupSomeReload', { count, defaultValue: '{{count}} need a restart' })
      case 'failed':
        return sole
          ? t('contentFilter.rollupAllFailed', { count, defaultValue: 'All {{count}} endpoints failed' })
          : t('contentFilter.rollupSomeFailed', { count, defaultValue: '{{count}} failed' })
      case 'unconfirmed':
        return sole
          ? t('contentFilter.rollupAllUnconfirmed', {
              count,
              defaultValue: 'All {{count}} endpoints not confirmed',
            })
          : t('contentFilter.rollupSomeUnconfirmed', { count, defaultValue: '{{count}} not confirmed' })
      case 'skipped':
        return sole
          ? t('contentFilter.rollupAllSkipped', { count, defaultValue: 'All {{count}} endpoints skipped' })
          : t('contentFilter.rollupSomeSkipped', { count, defaultValue: '{{count}} skipped' })
      case 'disabled':
        return sole
          ? t('contentFilter.rollupAllDisabled', { count, defaultValue: 'All {{count}} endpoints not active' })
          : t('contentFilter.rollupSomeDisabled', { count, defaultValue: '{{count}} not active' })
      case 'applied':
        return sole
          ? t('contentFilter.rollupAllApplied', { count, defaultValue: 'All {{count}} endpoints in force' })
          : t('contentFilter.rollupSomeApplied', { count, defaultValue: '{{count}} in force' })
    }
  }
  return (
    <span
      className={cn(
        'inline-flex h-5 shrink-0 items-center rounded-full border px-2 text-[11px] font-normal tabular-nums',
        ROLLUP_CHIP[state],
      )}
    >
      {label()}
    </span>
  )
}

function useRollupGroupLabel() {
  const { t } = useTranslation()
  return (state: RollupState): string => {
    switch (state) {
      case 'reload':
        return t('contentFilter.reloadTitle', { defaultValue: 'Restart needed before this takes effect' })
      case 'failed':
        return t('contentFilter.groupFailed', { defaultValue: 'Could not be applied' })
      case 'unconfirmed':
        return t('contentFilter.groupUnconfirmed', { defaultValue: 'Saved but not confirmed' })
      case 'skipped':
        return t('contentFilter.groupSkipped', { defaultValue: 'Refused before anything was saved' })
      case 'disabled':
        return t('contentFilter.groupDisabled', { defaultValue: 'Saved but not active' })
      case 'applied':
        return t('contentFilter.groupApplied', { defaultValue: 'In force on the node' })
    }
  }
}

const ROLLUP_EXCEPTION_LIMIT = 3

function RollupNodeRow({
  rollup,
  open,
  onOpenChange,
  nodeLabel,
  fleetNodesLabel,
}: {
  rollup: NodeRollup
  open: boolean
  onOpenChange: (next: boolean) => void
  nodeLabel: (id: number) => string
  fleetNodesLabel: string
}) {
  const { t } = useTranslation()
  const skipReason = useOutcomeSkipReason()
  const groupLabel = useRollupGroupLabel()

  const summaryOf = (outcome: AssignmentOutcome): string | null => outcome.detail?.trim() || skipReason(outcome)

  const shown = rollup.attention.slice(0, ROLLUP_EXCEPTION_LIMIT)
  const hidden = rollup.attention.length - shown.length

  return (
    <Collapsible
      open={open}
      onOpenChange={onOpenChange}
      className={cn('rounded-lg border-y border-e border-s-2 px-3 py-2 transition-colors', ROLLUP_ROW[rollup.worst])}
    >
      <div className="flex flex-wrap items-center gap-2">
        <CollapsibleTrigger asChild>
          <Button
            type="button"
            variant="ghost"
            size="icon"
            className="-ms-1.5 size-6 shrink-0"
            aria-label={
              open
                ? t('contentFilter.rollupCollapseNode', { defaultValue: 'Hide the endpoints on this node' })
                : t('contentFilter.rollupExpandNode', { defaultValue: 'Show the endpoints on this node' })
            }
          >
            <ChevronDown className={cn('size-4 transition-transform', open && 'rotate-180')} />
          </Button>
        </CollapsibleTrigger>
        {rollup.nodeId === null ? (
          <Server className="size-3.5 shrink-0 text-muted-foreground" />
        ) : (
          <NodeStatusDot status={rollup.status ?? 'disabled'} />
        )}
        <span title={rollup.name} className="min-w-0 truncate text-sm font-medium">{rollup.name}</span>
        <span
          className={cn(
            'shrink-0 text-[11px] tabular-nums',
            rollup.nodeId === null ? 'text-amber-700 dark:text-amber-400' : 'text-muted-foreground',
          )}
        >
          {rollup.nodeId !== null
            ? t('contentFilter.nodeIdLabel', { id: rollup.nodeId, defaultValue: 'id {{id}}' })
            : fleetNodesLabel}
        </span>
        <DeliveryBadge delivery={rollup.delivery} />
        <span className="ms-auto flex min-w-0 flex-wrap items-center justify-end gap-1">
          {rollup.groups.map(group => (
            <RollupCountChip
              key={group.state}
              state={group.state}
              count={group.items.length}
              sole={rollup.sole === group.state}
            />
          ))}
        </span>
      </div>

      {!open && rollup.attention.length ? (
        <div className="mt-1.5 space-y-0.5">
          {rollup.sole && rollup.sole !== 'applied' ? (
            detailLines(rollup.endpoints, summaryOf).map(line => (
              <p key={line.text} className={cn('break-words text-[11px] leading-snug', ROLLUP_TEXT[rollup.worst])}>
                {line.all
                  ? line.text
                  : t('contentFilter.rollupDetailFor', {
                      endpoints: line.tags.join(' · '),
                      detail: line.text,
                      defaultValue: '{{endpoints}} — {{detail}}',
                    })}
              </p>
            ))
          ) : (
            <>
              {shown.map((item, index) => (
                <p key={`${index}:${item.tag}`} className={cn('break-words text-[11px] leading-snug', ROLLUP_TEXT[item.state])}>
                  {t('contentFilter.rollupDetailFor', {
                    endpoints: item.tag,
                    detail: summaryOf(item.outcome) ?? groupLabel(item.state),
                    defaultValue: '{{endpoints}} — {{detail}}',
                  })}
                </p>
              ))}
              {hidden > 0 ? (
                <p className="break-words text-[11px] leading-snug text-muted-foreground">
                  {t('contentFilter.rollupMoreExceptions', {
                    hidden,
                    defaultValue: '{{hidden}} more endpoints here still need a look',
                  })}
                </p>
              ) : null}
            </>
          )}
        </div>
      ) : null}

      <CollapsibleContent className="ms-1 mt-2 space-y-2.5 border-s-2 ps-3">
        {rollup.groups.map(group => (
          <div key={group.state} className="space-y-1">
            <p
              className={cn(
                'flex items-center gap-2 text-[11px] font-semibold uppercase tracking-wide',
                ROLLUP_TEXT[group.state],
              )}
            >
              {groupLabel(group.state)}
              <span className="font-normal tabular-nums">{group.items.length}</span>
            </p>
            <div className="flex flex-wrap gap-1">
              {group.items.map((item, index) => (
                <span
                  key={`${index}:${item.tag}`}
                  title={item.tag}
                  className={cn(
                    'inline-flex h-5 max-w-full items-center rounded border bg-background/60 px-1.5 text-[11px] font-normal',
                    ROLLUP_TAG[group.state],
                  )}
                >
                  <span className="min-w-0 truncate">{item.tag}</span>
                </span>
              ))}
            </div>
            {detailLines(group.items, outcome => outcome.detail?.trim() || null).map(line => (
              <p key={line.text} className="text-xs leading-snug text-muted-foreground">
                {line.all
                  ? line.text
                  : t('contentFilter.rollupDetailFor', {
                      endpoints: line.tags.join(' · '),
                      detail: line.text,
                      defaultValue: '{{endpoints}} — {{detail}}',
                    })}
              </p>
            ))}
            {detailLines(group.items, skipReason).map(line => (
              <p key={line.text} className="text-xs leading-snug text-amber-700 dark:text-amber-400">
                {t('contentFilter.outcomeSkipReason', {
                  reason: line.all
                    ? line.text
                    : t('contentFilter.rollupDetailFor', {
                        endpoints: line.tags.join(' · '),
                        detail: line.text,
                        defaultValue: '{{endpoints}} — {{detail}}',
                      }),
                  defaultValue: 'Left out — {{reason}}',
                })}
              </p>
            ))}
          </div>
        ))}
        {rollup.reload ? (
          <div className="space-y-0.5">
            <p className="text-xs leading-snug text-amber-700 dark:text-amber-400">
              {!rollup.reload.tags.length
                ? rollup.reload.message
                : rollup.reload.withdraw
                  ? t('contentFilter.reloadWithdrawWhy', {
                      tags: rollup.reload.tags.join(', '),
                      defaultValue:
                        'Putting your own setting back on {{tags}} only takes effect once the nodes below restart, and every session on them drops.',
                    })
                  : t('contentFilter.reloadWhy', {
                      tags: rollup.reload.tags.join(', '),
                      defaultValue:
                        'Name recovery has to change on {{tags}}. It only takes effect once the nodes below restart, and every session on them drops.',
                    })}
            </p>
            {rollup.reload.nodeIds.length ? (
              <p className="text-xs leading-snug text-muted-foreground">
                {t('contentFilter.reloadNodes', {
                  nodes: rollup.reload.nodeIds.map(id => nodeLabel(id)).join(', '),
                  defaultValue: 'Nodes that would restart: {{nodes}}',
                })}
              </p>
            ) : null}
          </div>
        ) : null}
        <AdvisoryList advisories={rollup.advisories} note={rollup.advisoryNote} />
      </CollapsibleContent>
    </Collapsible>
  )
}

type AssignMember = {
  key: string
  label: string
  state: RollupState
  assignmentId: number | null
  nodeId: number | null
  status: string | null
  error: string | null
}

type AssignGroup = {
  key: string
  kind: 'node' | 'endpoint'
  name: string
  nodeId: number | null
  status: string | null
  delivery: Delivery | null
  peers: string[]
  error: string | null
  members: AssignMember[]
  groups: { state: RollupState; items: AssignMember[] }[]
  worst: RollupState
  sole: RollupState | null
  attention: AssignMember[]
  assignmentIds: number[]
  nodeCount: number | null
}

const ASSIGN_ORDER: RollupState[] = ['failed', 'unconfirmed', 'applied']

function assignmentState(a: Assignment): RollupState {
  if (a.last_error && a.last_error.trim()) return 'failed'
  return a.enforced ? 'applied' : 'unconfirmed'
}

function buildAssignmentGroups(
  assignments: readonly Assignment[],
  nodeOf: (id: number) => TargetNode | undefined,
  nodeName: (id: number) => string,
  carriersOf: (tag: string) => number[] | null,
  wholeNodeName: string,
): AssignGroup[] {
  type NodeDraft = { nodeId: number; members: AssignMember[]; deliveries: Set<Delivery>; peers: string[] }
  const pinned = new Map<number, NodeDraft>()
  const endpointRows: AssignGroup[] = []

  for (const a of assignments) {
    const state = assignmentState(a)
    const error = a.last_error && a.last_error.trim() ? a.last_error.trim() : null

    if (a.node_id !== null) {
      const draft: NodeDraft = pinned.get(a.node_id) ?? {
        nodeId: a.node_id,
        members: [],
        deliveries: new Set<Delivery>(),
        peers: [],
      }
      draft.members.push({
        key: `a:${a.id}`,
        label: a.inbound_tag || wholeNodeName,
        state,
        assignmentId: a.id,
        nodeId: a.node_id,
        status: null,
        error,
      })
      if (a.delivery === 'core' || a.delivery === 'live') draft.deliveries.add(a.delivery)
      if (a.delivery === 'core') {
        for (const peer of nodeOf(a.node_id)?.shares_core_with ?? []) {
          const name = nodeName(peer)
          if (!draft.peers.includes(name)) draft.peers.push(name)
        }
      }
      pinned.set(a.node_id, draft)
      continue
    }

    const carriers = a.inbound_tag ? carriersOf(a.inbound_tag) : null
    const reached = new Set(carriers ?? [])
    const members: AssignMember[] = (carriers ?? [])
      .map(id => ({
        key: `a:${a.id}:n:${id}`,
        label: nodeName(id),
        state,
        assignmentId: null,
        nodeId: id,
        status: nodeOf(id)?.status ?? null,
        error: null,
      }))
      .sort((x, y) => x.label.localeCompare(y.label))
    const peers: string[] = []
    if (a.delivery === 'core') {
      for (const id of carriers ?? []) {
        for (const peer of nodeOf(id)?.shares_core_with ?? []) {
          if (reached.has(peer)) continue
          const name = nodeName(peer)
          if (!peers.includes(name)) peers.push(name)
        }
      }
    }
    endpointRows.push({
      key: `e:${a.id}`,
      kind: 'endpoint',
      name: a.inbound_tag || wholeNodeName,
      nodeId: null,
      status: null,
      delivery: a.delivery ?? null,
      peers,
      error,
      members,
      groups: [{ state, items: members }],
      worst: state,
      sole: state,
      attention: state === 'applied' ? [] : members,
      assignmentIds: [a.id],
      nodeCount: carriers === null ? null : carriers.length,
    })
  }

  const nodeRows: AssignGroup[] = []
  for (const draft of pinned.values()) {
    draft.members.sort((x, y) => x.label.localeCompare(y.label))
    const present = ASSIGN_ORDER.filter(state => draft.members.some(member => member.state === state))
    const worst = present[0] ?? 'applied'
    nodeRows.push({
      key: `n:${draft.nodeId}`,
      kind: 'node',
      name: nodeName(draft.nodeId),
      nodeId: draft.nodeId,
      status: nodeOf(draft.nodeId)?.status ?? null,
      delivery: draft.deliveries.size === 1 ? [...draft.deliveries][0] : null,
      peers: draft.peers,
      error: null,
      members: draft.members,
      groups: present.map(state => ({ state, items: draft.members.filter(member => member.state === state) })),
      worst,
      sole: present.length === 1 ? worst : null,
      attention: ASSIGN_ORDER.flatMap(state =>
        state === 'applied' ? [] : draft.members.filter(member => member.state === state),
      ),
      assignmentIds: draft.members.flatMap(member => (member.assignmentId === null ? [] : [member.assignmentId])),
      nodeCount: null,
    })
  }

  return [...nodeRows, ...endpointRows].sort((a, b) => {
    const rank = ASSIGN_ORDER.indexOf(a.worst) - ASSIGN_ORDER.indexOf(b.worst)
    return rank !== 0 ? rank : a.name.localeCompare(b.name)
  })
}

function assignErrorLines(items: readonly AssignMember[]) {
  const found = new Map<string, string[]>()
  for (const item of items) {
    if (!item.error) continue
    const names = found.get(item.error) ?? []
    names.push(item.label)
    found.set(item.error, names)
  }
  return [...found.entries()].map(([text, names]) => ({ text, names, all: names.length === items.length }))
}

function useAssignStateLabel() {
  const { t } = useTranslation()
  return (state: RollupState): string => {
    if (state === 'failed') return t('contentFilter.assignFailed', { defaultValue: 'Failed' })
    if (state === 'applied') return t('contentFilter.enforced', { defaultValue: 'In force' })
    return t('contentFilter.notEnforced', { defaultValue: 'Not confirmed' })
  }
}

function AssignmentGroupRow({
  row,
  open,
  onOpenChange,
  onRemoveMember,
  onRemoveAll,
}: {
  row: AssignGroup
  open: boolean
  onOpenChange: (next: boolean) => void
  onRemoveMember: (id: number) => void
  onRemoveAll: () => void
}) {
  const { t } = useTranslation()
  const groupLabel = useRollupGroupLabel()
  const stateLabel = useAssignStateLabel()
  const isNode = row.kind === 'node'
  const shown = row.attention.slice(0, ROLLUP_EXCEPTION_LIMIT)
  const hidden = row.attention.length - shown.length

  return (
    <Collapsible
      open={open}
      onOpenChange={onOpenChange}
      className={cn('rounded-lg border-y border-e border-s-2 px-3 py-2 transition-colors', ROLLUP_ROW[row.worst])}
    >
      <div className="flex flex-wrap items-center gap-2">
        <CollapsibleTrigger asChild>
          <Button
            type="button"
            variant="ghost"
            size="icon"
            className="-ms-1.5 size-6 shrink-0"
            aria-label={
              isNode
                ? open
                  ? t('contentFilter.rollupCollapseNode', { defaultValue: 'Hide the endpoints on this node' })
                  : t('contentFilter.rollupExpandNode', { defaultValue: 'Show the endpoints on this node' })
                : open
                  ? t('contentFilter.assignCollapseEndpoint', { defaultValue: 'Hide the nodes this endpoint reaches' })
                  : t('contentFilter.assignExpandEndpoint', { defaultValue: 'Show the nodes this endpoint reaches' })
            }
          >
            <ChevronDown className={cn('size-4 transition-transform', open && 'rotate-180')} />
          </Button>
        </CollapsibleTrigger>
        {isNode ? (
          <NodeStatusDot status={row.status ?? 'disabled'} />
        ) : (
          <Globe className="size-3.5 shrink-0 text-amber-700 dark:text-amber-400" />
        )}
        <span title={row.name} className="min-w-0 truncate text-sm font-medium">
          {row.name}
        </span>
        <span
          className={cn(
            'inline-flex h-5 shrink-0 items-center rounded-full border px-2 text-[11px] font-normal',
            isNode ? 'border-border text-muted-foreground' : 'border-amber-500/40 text-amber-700 dark:text-amber-400',
          )}
        >
          {isNode
            ? t('contentFilter.assignKindNode', { defaultValue: 'Node' })
            : t('contentFilter.assignKindEndpoint', { defaultValue: 'Endpoint' })}
        </span>
        <span
          className={cn(
            'shrink-0 text-[11px] tabular-nums',
            isNode ? 'text-muted-foreground' : 'text-amber-700 dark:text-amber-400',
          )}
        >
          {isNode && row.nodeId !== null
            ? t('contentFilter.nodeIdLabel', { id: row.nodeId, defaultValue: 'id {{id}}' })
            : row.nodeCount === null
              ? t('contentFilter.rollupFleetNodesOpen', { defaultValue: 'node count unknown' })
              : t('contentFilter.rollupFleetNodes', { count: row.nodeCount, defaultValue: '{{count}} nodes right now' })}
        </span>
        <DeliveryBadge delivery={row.delivery} />
        <span className="ms-auto flex min-w-0 flex-wrap items-center justify-end gap-1">
          {isNode ? (
            row.groups.map(group => (
              <RollupCountChip
                key={group.state}
                state={group.state}
                count={group.items.length}
                sole={row.sole === group.state}
              />
            ))
          ) : (
            <span
              className={cn(
                'inline-flex h-5 shrink-0 items-center rounded-full border px-2 text-[11px] font-normal',
                ROLLUP_CHIP[row.worst],
              )}
            >
              {stateLabel(row.worst)}
            </span>
          )}
          <Button
            type="button"
            size="icon"
            variant="ghost"
            className="-me-1 size-7 shrink-0"
            onClick={onRemoveAll}
            aria-label={
              isNode
                ? t('contentFilter.assignLiftNodeAria', {
                    name: row.name,
                    defaultValue: 'Lift this profile from every endpoint on {{name}}',
                  })
                : t('contentFilter.assignLiftEndpointAria', {
                    name: row.name,
                    defaultValue: 'Lift this profile from {{name}} on every node',
                  })
            }
          >
            <Trash2 className="size-4" />
          </Button>
        </span>
      </div>

      {row.peers.length ? (
        <p className="mt-1 break-words text-[11px] leading-snug text-amber-700 dark:text-amber-400">
          {t('contentFilter.alsoEnforcedOn', {
            names: row.peers.join(', '),
            defaultValue: 'Shared configuration, so it is enforced on {{names}} as well.',
          })}
        </p>
      ) : null}

      {!open && row.worst !== 'applied' ? (
        <div className="mt-1.5 space-y-0.5">
          {isNode ? (
            <>
              {shown.map(member => (
                <p key={member.key} className={cn('break-words text-[11px] leading-snug', ROLLUP_TEXT[member.state])}>
                  {t('contentFilter.rollupDetailFor', {
                    endpoints: member.label,
                    detail: member.error ?? stateLabel(member.state),
                    defaultValue: '{{endpoints}} — {{detail}}',
                  })}
                </p>
              ))}
              {hidden > 0 ? (
                <p className="break-words text-[11px] leading-snug text-muted-foreground">
                  {t('contentFilter.rollupMoreExceptions', {
                    hidden,
                    defaultValue: '{{hidden}} more endpoints here still need a look',
                  })}
                </p>
              ) : null}
            </>
          ) : (
            <p className={cn('break-words text-[11px] leading-snug', ROLLUP_TEXT[row.worst])}>
              {row.error ?? stateLabel(row.worst)}
            </p>
          )}
        </div>
      ) : null}

      <CollapsibleContent className="ms-1 mt-2 space-y-2.5 border-s-2 ps-3">
        {isNode ? (
          row.groups.map(group => (
            <div key={group.state} className="space-y-1">
              <p
                className={cn(
                  'flex items-center gap-2 text-[11px] font-semibold uppercase tracking-wide',
                  ROLLUP_TEXT[group.state],
                )}
              >
                {groupLabel(group.state)}
                <span className="font-normal tabular-nums">{group.items.length}</span>
              </p>
              <div className="flex flex-wrap gap-1">
                {group.items.map(member => (
                  <span
                    key={member.key}
                    title={member.label}
                    className={cn(
                      'inline-flex h-6 max-w-full items-center gap-0.5 rounded border bg-background/60 ps-1.5 text-[11px] font-normal',
                      ROLLUP_TAG[group.state],
                    )}
                  >
                    <span className="min-w-0 truncate">{member.label}</span>
                    <Button
                      type="button"
                      variant="ghost"
                      size="icon"
                      className="size-5 shrink-0 rounded-sm"
                      disabled={member.assignmentId === null}
                      onClick={() => {
                        if (member.assignmentId !== null) onRemoveMember(member.assignmentId)
                      }}
                      aria-label={t('contentFilter.assignLiftMemberAria', {
                        name: member.label,
                        defaultValue: 'Lift this profile from {{name}}',
                      })}
                    >
                      <X className="size-3" />
                    </Button>
                  </span>
                ))}
              </div>
              {assignErrorLines(group.items).map(line => (
                <p key={line.text} className="break-words text-xs leading-snug text-destructive">
                  {line.all
                    ? line.text
                    : t('contentFilter.rollupDetailFor', {
                        endpoints: line.names.join(' · '),
                        detail: line.text,
                        defaultValue: '{{endpoints}} — {{detail}}',
                      })}
                </p>
              ))}
            </div>
          ))
        ) : (
          <div className="space-y-1">
            <p className="flex items-center gap-2 text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">
              {t('contentFilter.assignReaches', { defaultValue: 'Nodes it reaches' })}
              {row.nodeCount === null ? null : <span className="font-normal tabular-nums">{row.nodeCount}</span>}
            </p>
            {row.members.length ? (
              <div className="flex flex-wrap gap-1">
                {row.members.map(member => (
                  <span
                    key={member.key}
                    title={member.label}
                    className={cn(
                      'inline-flex h-5 max-w-full items-center gap-1 rounded border bg-background/60 px-1.5 text-[11px] font-normal',
                      ROLLUP_TAG[row.worst],
                    )}
                  >
                    {member.status ? <NodeStatusDot status={member.status} /> : null}
                    <span className="min-w-0 truncate">{member.label}</span>
                  </span>
                ))}
              </div>
            ) : (
              <p className="break-words text-xs leading-snug text-muted-foreground">
                {t('contentFilter.assignReachesUnknown', {
                  defaultValue: 'The panel cannot say which nodes carry this endpoint right now.',
                })}
              </p>
            )}
            {row.error ? <p className="break-words text-xs leading-snug text-destructive">{row.error}</p> : null}
            <p className="break-words text-xs leading-snug text-muted-foreground">
              {t('contentFilter.assignEndpointWhole', {
                defaultValue: 'Pinned to no node, so it is lifted from every node at once.',
              })}
            </p>
          </div>
        )}
      </CollapsibleContent>
    </Collapsible>
  )
}

type PickEntry = { key: string; label: string; domains: number; icon?: string }

function ServiceIcon({ svg, active }: { svg?: string; active: boolean }) {
  if (!svg) return <span className={cn('size-8 shrink-0 rounded-lg', active ? 'bg-primary/15' : 'bg-muted')} />
  return (
    <span
      className={cn(
        'flex size-8 shrink-0 items-center justify-center rounded-lg p-1.5 transition-colors [&>svg]:size-full',
        active ? 'bg-primary/15 text-primary' : 'bg-muted text-muted-foreground',
      )}
      dangerouslySetInnerHTML={{ __html: svg }}
    />
  )
}

function PickerSheet({
  open,
  onOpenChange,
  groupKey,
  title,
  entries,
  selected,
  locked,
  onToggle,
  onAll,
  onNone,
}: {
  open: boolean
  onOpenChange: (v: boolean) => void
  groupKey: string
  title: string
  entries: PickEntry[]
  selected: Set<string>
  locked?: boolean
  onToggle: (key: string, on: boolean) => void
  onAll: () => void
  onNone: () => void
}) {
  const { t } = useTranslation()
  const dir = useDirDetection()
  const [query, setQuery] = useState('')
  const [onlyPicked, setOnlyPicked] = useState(false)
  const GIcon = GROUP_ICON[groupKey] ?? ShieldBan
  const picked = entries.filter(e => selected.has(e.key)).length
  const q = query.trim().toLowerCase()
  const shown = entries.filter(e => (!q || e.label.toLowerCase().includes(q)) && (!onlyPicked || selected.has(e.key)))

  return (
    <Sheet open={open} onOpenChange={onOpenChange}>
      <SheetContent side={dir === 'rtl' ? 'left' : 'right'} className="flex w-full flex-col gap-0 p-0 sm:max-w-md">
        <SheetHeader className="space-y-0 border-b p-5 text-start">
          <div className="flex items-center gap-3">
            <span className="rounded-xl bg-primary/10 p-2.5">
              <GIcon className={cn('size-5', GROUP_TONE.get(groupKey))} />
            </span>
            <div className="min-w-0">
              <SheetTitle className="text-base leading-tight">{title}</SheetTitle>
              <p className="mt-0.5 text-xs tabular-nums text-muted-foreground">
                {picked === 0
                  ? t('contentFilter.pickedNone', { total: entries.length, defaultValue: 'None of {{total}} blocked' })
                  : picked === entries.length
                    ? t('contentFilter.pickedAll', { total: entries.length, defaultValue: 'All {{total}} blocked' })
                    : t('contentFilter.pickedSome', { picked, total: entries.length, defaultValue: '{{picked}} of {{total}} blocked' })}
              </p>
            </div>
          </div>
          <div className="relative mt-4">
            <Search className="pointer-events-none absolute top-1/2 size-4 -translate-y-1/2 text-muted-foreground ltr:left-3 rtl:right-3" />
            <Input
              value={query}
              onChange={e => setQuery(e.target.value)}
              placeholder={t('contentFilter.searchServices', { defaultValue: 'Search services' })}
              className="h-9 ltr:pl-9 rtl:pr-9"
            />
          </div>
          <div className="mt-3 flex items-center justify-between">
            <button
              type="button"
              onClick={() => setOnlyPicked(v => !v)}
              className={cn(
                'rounded-full border px-2.5 py-1 text-xs transition-colors',
                onlyPicked ? 'border-primary bg-primary/10 text-primary' : 'text-muted-foreground hover:bg-muted',
              )}
            >
              {t('contentFilter.onlySelected', { defaultValue: 'Selected only' })}
            </button>
            <span className="flex gap-1">
              <Button variant="ghost" size="sm" className="h-7 px-2 text-xs" disabled={locked} onClick={onAll}>
                {t('contentFilter.selectAll', { defaultValue: 'All' })}
              </Button>
              <Button variant="ghost" size="sm" className="h-7 px-2 text-xs" disabled={locked} onClick={onNone}>
                {t('contentFilter.selectNone', { defaultValue: 'None' })}
              </Button>
            </span>
          </div>
        </SheetHeader>

        <div className="min-h-0 flex-1 overflow-y-auto p-3">
          {locked ? (
            <div className="rounded-lg border border-primary/30 bg-primary/5 px-3 py-3 text-sm">
              {t('contentFilter.wholeGroupNote', { defaultValue: 'The whole group is blocked, so every service in it is already covered.' })}
            </div>
          ) : shown.length === 0 ? (
            <p className="px-3 py-8 text-center text-sm text-muted-foreground">
              {t('contentFilter.noMatch', { defaultValue: 'Nothing matches that.' })}
            </p>
          ) : (
            <div className="space-y-1">
              {shown.map(entry => {
                const on = selected.has(entry.key)
                return (
                  <label
                    key={entry.key}
                    className={cn(
                      'flex cursor-pointer items-center gap-3 rounded-lg border px-3 py-2 transition-colors',
                      on ? 'border-primary/40 bg-primary/[0.05]' : 'border-transparent hover:bg-muted/60',
                    )}
                  >
                    <ServiceIcon svg={entry.icon} active={on} />
                    <span className="min-w-0 flex-1">
                      <span className="block truncate text-sm font-medium">{entry.label}</span>
                      <span className="block text-[11px] tabular-nums text-muted-foreground">
                        {t('contentFilter.domainCount', { count: entry.domains, defaultValue: '{{count}} sites' })}
                      </span>
                    </span>
                    <Checkbox checked={on} onCheckedChange={v => onToggle(entry.key, v === true)} className="size-5" />
                  </label>
                )
              })}
            </div>
          )}
        </div>

        <SheetFooter className="border-t p-4">
          <SheetClose asChild>
            <Button className="w-full">{t('contentFilter.done', { defaultValue: 'Done' })}</Button>
          </SheetClose>
        </SheetFooter>
      </SheetContent>
    </Sheet>
  )
}

function GroupTile({
  groupKey,
  size,
  picked,
  total,
  whole,
  onWhole,
  onOpen,
}: {
  groupKey: string
  size: number
  picked: number
  total: number
  whole: boolean
  onWhole?: (v: boolean) => void
  onOpen: () => void
}) {
  const { t } = useTranslation()
  const Icon = GROUP_ICON[groupKey] ?? ShieldBan
  const partial = !whole && picked > 0
  const active = whole || partial

  return (
    <div
      className={cn(
        'group relative flex flex-col justify-between gap-3 rounded-xl border bg-card p-4 transition-all',
        active ? 'border-primary/50 shadow-sm ring-1 ring-primary/10' : 'hover:border-foreground/20',
      )}
    >
      <div className="flex items-start justify-between gap-3">
        <div className="flex min-w-0 items-start gap-2.5">
          <span className={cn('mt-0.5 rounded-lg p-1.5', active ? 'bg-primary/10' : 'bg-muted')}>
            <Icon className={cn('size-4', GROUP_TONE.get(groupKey))} />
          </span>
          <div className="min-w-0">
            <p className="truncate text-sm font-semibold leading-tight">
              {t(`contentFilter.groups.${groupKey}`, { defaultValue: groupKey })}
            </p>
            <p className="mt-1 text-xs tabular-nums text-muted-foreground">
              {t('contentFilter.domainCount', { count: size, defaultValue: '{{count}} sites' })}
            </p>
          </div>
        </div>
        {onWhole ? <Switch checked={whole} onCheckedChange={onWhole} /> : null}
      </div>

      <div className="flex items-center justify-between gap-2">
        {whole ? (
          <Badge className="h-5 border-primary/30 bg-primary/10 px-2 text-[11px] font-normal text-primary hover:bg-primary/10">
            {t('contentFilter.allBlocked', { defaultValue: 'All blocked' })}
          </Badge>
        ) : partial ? (
          <Badge variant="secondary" className="h-5 px-2 text-[11px] font-normal">
            {t('contentFilter.partial', { picked, total, defaultValue: '{{picked}} of {{total}}' })}
          </Badge>
        ) : (
          <span className="text-[11px] text-muted-foreground">{t('contentFilter.none', { defaultValue: 'Not blocked' })}</span>
        )}
        {total > 0 ? (
          <Button variant="ghost" size="sm" className="-me-1 h-7 gap-1 px-2 text-xs" onClick={onOpen}>
            {t('contentFilter.choose', { defaultValue: 'Choose' })}
            <ChevronRight className="size-3.5 rtl:rotate-180" />
          </Button>
        ) : null}
      </div>
    </div>
  )
}


function DomainList({
  label,
  hint,
  values,
  tone,
  onChange,
}: {
  label: string
  hint: string
  values: string[]
  tone: 'allow' | 'block'
  onChange: (next: string[]) => void
}) {
  const { t } = useTranslation()
  const [draft, setDraft] = useState('')

  const add = () => {
    const value = draft.trim().toLowerCase().replace(/^https?:\/\//, '').split('/')[0]
    if (!value) return
    if (!values.includes(value)) onChange([...values, value])
    setDraft('')
  }

  return (
    <div className="space-y-2">
      <div>
        <Label className="text-sm">{label}</Label>
        <p className="text-xs text-muted-foreground">{hint}</p>
      </div>
      <div className="flex gap-2">
        <Input
          value={draft}
          onChange={e => setDraft(e.target.value)}
          onKeyDown={e => {
            if (e.key === 'Enter') {
              e.preventDefault()
              add()
            }
          }}
          placeholder={t('contentFilter.domainPlaceholder', { defaultValue: 'example.com' })}
          className="h-9"
        />
        <Button type="button" variant="secondary" size="sm" className="h-9 shrink-0" onClick={add}>
          {t('contentFilter.add', { defaultValue: 'Add' })}
        </Button>
      </div>
      {values.length ? (
        <div className="flex flex-wrap gap-1.5">
          {values.map(value => (
            <Badge
              key={value}
              variant="outline"
              className={cn(
                'gap-1 font-normal',
                tone === 'allow'
                  ? 'border-emerald-500/40 bg-emerald-500/10 text-emerald-700 dark:text-emerald-400'
                  : 'border-destructive/40 bg-destructive/10 text-destructive',
              )}
            >
              {value}
              <button
                type="button"
                onClick={() => onChange(values.filter(v => v !== value))}
                className="opacity-60 hover:opacity-100"
                aria-label={t('contentFilter.remove', { defaultValue: 'Remove' })}
              >
                <X className="size-3" />
              </button>
            </Badge>
          ))}
        </div>
      ) : null}
    </div>
  )
}

export default function ContentFilterPage() {
  const { t } = useTranslation()
  const dir = useDirDetection()
  const queryClient = useQueryClient()
  const { admin } = useAdmin()
  const panelOwner = isOwner(admin as unknown as AdminDetails | null)

  const catalog = useCatalog()
  const profiles = useProfiles()
  const targets = useTargets()
  const assignments = useAssignments()
  const capability = useContentFilterCapability(BASE)
  const caps = useCapabilityIndex(capability.data)
  const reasonText = useCapabilityReasonText()

  const [activeId, setActiveId] = useState<number | null>(null)
  const [draft, setDraft] = useState<Profile | null>(null)
  const [assignOpen, setAssignOpen] = useState(false)
  const [nameOpen, setNameOpen] = useState(false)
  const [nameDraft, setNameDraft] = useState('')
  const [deleteOpen, setDeleteOpen] = useState(false)
  const [picker, setPicker] = useState<string | null>(null)
  const [assignNodes, setAssignNodes] = useState<number[]>([])
  const [assignTags, setAssignTags] = useState<string[]>([])
  const [nodeQuery, setNodeQuery] = useState('')
  const [endpointQuery, setEndpointQuery] = useState('')
  const [bulkResult, setBulkResult] = useState<BulkResult | null>(null)
  const [bulkRequest, setBulkRequest] = useState<ApplyBody | null>(null)
  const [rollupOpen, setRollupOpen] = useState<Record<string, boolean>>({})
  const [groupOpen, setGroupOpen] = useState<Record<string, boolean>>({})
  const [liftGroup, setLiftGroup] = useState<{
    kind: 'node' | 'endpoint'
    name: string
    ids: number[]
    nodes: number | null
    peers: string[]
  } | null>(null)
  const [skipConfirmOpen, setSkipConfirmOpen] = useState(false)
  const [pendingRestart, setPendingRestart] = useState<PendingRestart | null>(null)
  const [probeDomain, setProbeDomain] = useState('')
  const [probeResult, setProbeResult] = useState<{ domain: string; blocked: boolean; outbound: string } | null>(null)

  useEffect(() => {
    if (activeId === null && profiles.data?.length) setActiveId(profiles.data[0].id)
  }, [profiles.data, activeId])

  useEffect(() => {
    const found = profiles.data?.find(p => p.id === activeId) ?? null
    setDraft(found ? { ...found, categories: [...found.categories], allow_list: [...found.allow_list], block_list: [...found.block_list] } : null)
  }, [activeId, profiles.data])

  useEffect(() => {
    if (!caps.available) return
    setAssignNodes(prev => {
      const next = prev.filter(id => caps.nodeSupport(id).supported)
      return next.length === prev.length ? prev : next
    })
    setAssignTags(prev => {
      const next = prev.filter(tag => caps.tagSupport(tag).supported)
      return next.length === prev.length ? prev : next
    })
  }, [caps])

  const selected = useMemo(() => new Set(draft?.categories ?? []), [draft])
  const everyListPicked = useMemo(() => {
    const entries = (catalog.data?.lists ?? []).flatMap(g => g.entries.map(e => e.key))
    return entries.length > 0 && entries.every(key => selected.has(key))
  }, [catalog.data, selected])

  const invalidate = () => {
    queryClient.invalidateQueries({ queryKey: ['content-filter'] })
  }

  const createProfile = useMutation({
    mutationFn: (name: string) =>
      fetcher<Profile>(`${BASE}/profiles`, {
        method: 'POST',
        body: { name, categories: [], allow_list: [], block_list: [], strict_mode: true, note: null },
      }),
    onSuccess: created => {
      toast.success(t('contentFilter.profileCreated', { defaultValue: 'Profile created' }))
      invalidate()
      setActiveId(created.id)
    },
    onError: e => toast.error(errorText(e, t('contentFilter.saveFailed', { defaultValue: 'Could not save' }))),
  })

  const saveProfile = useMutation({
    mutationFn: ({ profile, confirm }: { profile: Profile; confirm: boolean }) =>
      fetcher<Profile>(`${BASE}/profiles/${profile.id}${confirm ? '?confirm_restart=true' : ''}`, {
        method: 'PUT',
        body: {
          name: profile.name,
          categories: profile.categories,
          allow_list: profile.allow_list,
          block_list: profile.block_list,
          strict_mode: profile.strict_mode,
          note: profile.note,
        },
      }),
    onSuccess: () => {
      toast.success(t('contentFilter.saved', { defaultValue: 'Saved and pushed to every endpoint it covers' }))
      invalidate()
    },
    onError: (e, variables) => {
      const prompt = reloadPromptOf(e)
      if (prompt) {
        setPendingRestart({ prompt, request: { kind: 'saveProfile', profile: variables.profile } })
        return
      }
      toast.error(errorText(e, t('contentFilter.saveFailed', { defaultValue: 'Could not save' })))
    },
  })

  const removeProfile = useMutation({
    mutationFn: ({ id, confirm }: { id: number; confirm: boolean }) =>
      fetcher<void>(`${BASE}/profiles/${id}${confirm ? '?confirm_restart=true' : ''}`, { method: 'DELETE' }),
    onSuccess: () => {
      toast.success(t('contentFilter.profileDeleted', { defaultValue: 'Profile removed' }))
      setActiveId(null)
      invalidate()
    },
    onError: (e, variables) => {
      const prompt = reloadPromptOf(e)
      if (prompt) {
        setPendingRestart({ prompt, request: { kind: 'deleteProfile', id: variables.id } })
        return
      }
      toast.error(errorText(e, t('contentFilter.deleteFailed', { defaultValue: 'Could not remove' })))
    },
  })

  const applyTargets = useMutation({
    mutationFn: ({ body, confirm }: { body: ApplyBody; confirm: boolean }) =>
      fetcher<BulkResult>(`${BASE}/assignments/bulk`, {
        method: 'POST',
        body: { ...body, is_enabled: true, confirm_restart: confirm },
      }),
    onSuccess: (result, variables) => {
      setBulkResult(result)
      setBulkRequest(variables.body)
      const held = result.outcomes.filter(outcome => outcome.reload)
      const applied = result.applied ?? 0
      const failed = Math.max((result.failed ?? 0) - held.filter(outcome => outcome.status === 'failed').length, 0)
      const skipped = Math.max((result.skipped ?? 0) - held.filter(outcome => outcome.status === 'skipped').length, 0)
      if (held.length > 0) {
        toast.warning(
          t('contentFilter.reloadBulkPending', {
            count: held.length,
            defaultValue: '{{count}} endpoints are waiting for you to confirm a restart',
          }),
        )
      }
      if (failed > 0) {
        toast.error(
          t('contentFilter.bulkSomeFailed', {
            applied,
            failed,
            defaultValue: '{{applied}} in force, {{failed}} could not be applied',
          }),
        )
      } else if (held.length === 0 && skipped === 0) {
        toast.success(
          t('contentFilter.bulkEnforcedAll', {
            applied,
            defaultValue: 'In force on all {{applied}} endpoints',
          }),
        )
      } else if (held.length === 0) {
        toast.warning(
          t('contentFilter.bulkEnforcedPartly', {
            applied,
            skipped,
            defaultValue: '{{applied}} in force, {{skipped}} skipped',
          }),
        )
      }
      invalidate()
    },
    onError: (e, variables) => {
      const prompt = reloadPromptOf(e)
      if (prompt) {
        setPendingRestart({ prompt, request: { kind: 'applyTargets', body: variables.body } })
        return
      }
      toast.error(errorText(e, t('contentFilter.applyFailed', { defaultValue: 'Could not apply' })))
    },
  })

  const removeAssignment = useMutation({
    mutationFn: ({ id, confirm }: { id: number; confirm: boolean }) =>
      fetcher<void>(`${BASE}/assignments/${id}${confirm ? '?confirm_restart=true' : ''}`, { method: 'DELETE' }),
    onSuccess: () => {
      toast.success(t('contentFilter.lifted', { defaultValue: 'Filter lifted from that endpoint' }))
      invalidate()
    },
    onError: (e, variables) => {
      const prompt = reloadPromptOf(e)
      if (prompt) {
        setPendingRestart({ prompt, request: { kind: 'deleteAssignment', id: variables.id } })
        return
      }
      toast.error(errorText(e, t('contentFilter.liftFailed', { defaultValue: 'Could not lift' })))
    },
  })

  const runProbe = useMutation({
    mutationFn: (payload: { node_id: number; inbound_tag: string; domain: string }) =>
      fetcher<{ domain: string; outbound: string; blocked: boolean }>(`${BASE}/test`, { method: 'POST', body: payload }),
    onSuccess: r => setProbeResult(r),
    onError: e => toast.error(errorText(e, t('contentFilter.probeFailed', { defaultValue: 'Could not check' }))),
  })

  const toggleGroup = (key: string, on: boolean) => {
    if (!draft) return
    const group = catalog.data?.groups.find(g => g.key === key)
    const children = group?.services.map(s => s.key) ?? []
    const next = new Set(draft.categories)
    if (on) {
      children.forEach(c => next.delete(c))
      if (group?.geosite) next.add(key)
      else children.forEach(c => next.add(c))
    } else {
      next.delete(key)
      children.forEach(c => next.delete(c))
    }
    setDraft({ ...draft, categories: [...next] })
  }

  const toggleList = (key: string, on: boolean) => {
    if (!draft) return
    const group = catalog.data?.lists.find(g => g.key === key)
    const children = group?.entries.map(e => e.key) ?? []
    const next = new Set(draft.categories)
    children.forEach(c => (on ? next.add(c) : next.delete(c)))
    setDraft({ ...draft, categories: [...next] })
  }

  const toggleEveryList = (on: boolean) => {
    if (!draft) return
    const children = (catalog.data?.lists ?? []).flatMap(g => g.entries.map(e => e.key))
    const next = new Set(draft.categories)
    children.forEach(c => (on ? next.add(c) : next.delete(c)))
    setDraft({ ...draft, categories: [...next] })
  }

  const toggleService = (key: string, on: boolean) => {
    if (!draft) return
    const next = new Set(draft.categories)
    if (on) next.add(key)
    else next.delete(key)
    setDraft({ ...draft, categories: [...next] })
  }

  const profileAssignments = (assignments.data ?? []).filter(a => a.profile_id === activeId)
  const nodeById = useMemo(() => new Map((targets.data ?? []).map(n => [n.id, n])), [targets.data])
  const nodeLabel = (id: number) => nodeById.get(id)?.name ?? `#${id}`

  const fleetEndpoints = useMemo<FleetEndpoint[]>(() => {
    const seen = new Map<string, FleetEndpoint>()
    for (const node of targets.data ?? []) {
      for (const inbound of node.inbounds) {
        const entry = seen.get(inbound.tag) ?? {
          tag: inbound.tag,
          protocol: inbound.protocol,
          nodeIds: [],
          blockedNodeIds: [],
          blockedReason: null,
        }
        if (inbound.filterable && !node.reason) {
          entry.nodeIds.push(node.id)
        } else {
          entry.blockedNodeIds.push(node.id)
          if (!entry.blockedReason) entry.blockedReason = inbound.reason || node.reason || null
        }
        seen.set(inbound.tag, entry)
      }
    }
    return [...seen.values()].sort((a, b) => a.tag.localeCompare(b.tag))
  }, [targets.data])

  const pickedNodes = useMemo(() => new Set(assignNodes), [assignNodes])
  const pickedTags = useMemo(() => new Set(assignTags), [assignTags])

  const visibleNodes = useMemo(() => {
    const q = nodeQuery.trim().toLowerCase()
    if (!q) return targets.data ?? []
    return (targets.data ?? []).filter(node => node.name.toLowerCase().includes(q) || String(node.id).includes(q))
  }, [targets.data, nodeQuery])

  const visibleEndpoints = useMemo(() => {
    const q = endpointQuery.trim().toLowerCase()
    if (!q) return fleetEndpoints
    return fleetEndpoints.filter(
      entry => entry.tag.toLowerCase().includes(q) || entry.protocol.toLowerCase().includes(q),
    )
  }, [fleetEndpoints, endpointQuery])

  const selectableNodes = useMemo(
    () => visibleNodes.filter(node => !node.reason && caps.nodeSupport(node.id).supported),
    [visibleNodes, caps],
  )
  const selectableEndpoints = useMemo(
    () => visibleEndpoints.filter(entry => entry.nodeIds.length > 0 && caps.tagSupport(entry.tag).supported),
    [visibleEndpoints, caps],
  )
  const everyNodePicked = selectableNodes.length > 0 && selectableNodes.every(node => pickedNodes.has(node.id))
  const everyEndpointPicked = selectableEndpoints.length > 0 && selectableEndpoints.every(entry => pickedTags.has(entry.tag))

  const skippedCarriers = useCallback(
    (tag: string, nodeIds: readonly number[]) => {
      const tagCap = caps.tagSupport(tag)
      const unsupported = new Set(tagCap.unsupportedNodeIds)
      const ids = nodeIds.filter(id => unsupported.has(id) || !caps.nodeSupport(id).supported)
      const reason = ids.length ? (tagCap.reason ?? ids.map(id => caps.nodeSupport(id).reason).find(Boolean) ?? null) : null
      return { ids, reason }
    },
    [caps],
  )

  const applySkips = useMemo(() => {
    const empty = {
      nodes: [] as SkippedNode[],
      endpoints: [] as SkippedEndpoint[],
      unfilterable: [] as UnfilterableEndpoint[],
      nodeTargets: 0,
      unfilterableTargets: 0,
      total: 0,
    }
    if (!caps.available) return empty
    if (!assignNodes.length && !assignTags.length) return empty
    const nodeFilter = assignNodes.length ? pickedNodes : null
    const tagFilter = assignTags.length ? pickedTags : null
    const nodes = new Map<number, { reason: CapabilityReason | null; lost: number; kept: number }>()
    const endpoints = new Map<string, { nodes: number; reason: CapabilityReason | null }>()
    const unfilterable = new Map<string, { targets: number; reason: CapabilityReason | null }>()
    let nodeTargets = 0
    let unfilterableTargets = 0
    for (const node of targets.data ?? []) {
      if (node.reason) continue
      if (nodeFilter && !nodeFilter.has(node.id)) continue
      const nodeCap = caps.nodeSupport(node.id)
      for (const inbound of node.inbounds) {
        if (!inbound.filterable) continue
        if (tagFilter && !tagFilter.has(inbound.tag)) continue
        const tagCap = caps.tagSupport(inbound.tag)
        if (!tagCap.supported) {
          unfilterableTargets += 1
          const row = unfilterable.get(inbound.tag) ?? { targets: 0, reason: tagCap.reason }
          row.targets += 1
          if (!row.reason) row.reason = tagCap.reason
          unfilterable.set(inbound.tag, row)
          continue
        }
        const entry = nodes.get(node.id) ?? { reason: null as CapabilityReason | null, lost: 0, kept: 0 }
        const blockedByNode = !nodeCap.supported
        if (!blockedByNode && !tagCap.unsupportedNodeIds.includes(node.id)) {
          entry.kept += 1
          nodes.set(node.id, entry)
          continue
        }
        const reason = blockedByNode ? nodeCap.reason : (tagCap.reason ?? nodeCap.reason)
        nodeTargets += 1
        entry.lost += 1
        if (!entry.reason) entry.reason = reason
        nodes.set(node.id, entry)
        const row = endpoints.get(inbound.tag) ?? { nodes: 0, reason }
        row.nodes += 1
        if (!row.reason) row.reason = reason
        endpoints.set(inbound.tag, row)
      }
    }
    return {
      nodes: [...nodes.entries()]
        .filter(([, row]) => row.lost > 0)
        .map(([id, row]) => ({ id, reason: row.reason, lost: row.lost, kept: row.kept })),
      endpoints: [...endpoints.entries()].map(([tag, row]) => ({ tag, nodes: row.nodes, reason: row.reason })),
      unfilterable: [...unfilterable.entries()].map(([tag, row]) => ({ tag, targets: row.targets, reason: row.reason })),
      nodeTargets,
      unfilterableTargets,
      total: nodeTargets + unfilterableTargets,
    }
  }, [caps, assignNodes, assignTags, pickedNodes, pickedTags, targets.data])

  const reach = useMemo(() => {
    const all = targets.data ?? []
    const named = (id: number) => all.find(node => node.id === id)?.name ?? `#${id}`
    const scoped = assignNodes.length ? all.filter(node => pickedNodes.has(node.id)) : all
    const rows: ReachRow[] = []
    for (const node of scoped) {
      if (node.reason) continue
      const tags = node.inbounds
        .filter(inbound => inbound.filterable && (pickedTags.size ? pickedTags.has(inbound.tag) : assignNodes.length > 0))
        .map(inbound => inbound.tag)
      if (!tags.length) continue
      const delivery: Delivery | null =
        node.pinned_stays_here === true ? 'live' : node.pinned_stays_here === false ? 'core' : null
      rows.push({
        nodeId: node.id,
        name: node.name,
        status: node.status,
        tags,
        delivery,
        peers: delivery === 'core' ? (node.would_also_affect ?? []).map(id => ({ id, name: named(id) })) : [],
        note: node.scope_note ?? null,
      })
    }
    const reached = new Set(rows.map(row => row.nodeId))
    const spill = new Map<number, string[]>()
    for (const row of rows) {
      if (row.delivery !== 'core') continue
      for (const peer of row.peers) {
        if (reached.has(peer.id)) continue
        const causes = spill.get(peer.id) ?? []
        if (!causes.includes(row.name)) causes.push(row.name)
        spill.set(peer.id, causes)
      }
    }
    return {
      rows,
      endpoints: rows.reduce((sum, row) => sum + row.tags.length, 0),
      fleetWide: assignNodes.length === 0 && pickedTags.size > 0,
      unknown: rows.filter(row => row.delivery === null).map(row => row.name),
      spill: [...spill.entries()].map(([id, causes]) => ({ id, name: named(id), causes })),
    }
  }, [targets.data, assignNodes, pickedNodes, pickedTags])

  const unreachedTags = useMemo(() => {
    if (!pickedTags.size) return [] as string[]
    const covered = new Set(reach.rows.flatMap(row => row.tags))
    return assignTags.filter(tag => !covered.has(tag))
  }, [assignTags, pickedTags, reach.rows])

  const heldOutcomes = useMemo(() => (bulkResult?.outcomes ?? []).filter(outcome => outcome.reload), [bulkResult])

  const applyBodyStale = useCallback(
    (body: ApplyBody) =>
      body.profile_id !== activeId ||
      !sameList(sortedCopy(body.node_ids), sortedCopy(assignNodes)) ||
      !sameList(sortedCopy(body.inbound_tags), sortedCopy(assignTags)),
    [activeId, assignNodes, assignTags],
  )

  const profileStale = useCallback(
    (profile: Profile) =>
      !draft ||
      draft.id !== profile.id ||
      draft.name !== profile.name ||
      draft.strict_mode !== profile.strict_mode ||
      (draft.note ?? '') !== (profile.note ?? '') ||
      !sameList(sortedCopy(draft.categories), sortedCopy(profile.categories)) ||
      !sameList(sortedCopy(draft.allow_list), sortedCopy(profile.allow_list)) ||
      !sameList(sortedCopy(draft.block_list), sortedCopy(profile.block_list)),
    [draft],
  )

  const bulkSnapshotStale = useMemo(
    () => (bulkRequest ? applyBodyStale(bulkRequest) : false),
    [applyBodyStale, bulkRequest],
  )

  const restartStale = useMemo(() => {
    const request = pendingRestart?.request
    if (!request) return false
    if (request.kind === 'applyTargets') return applyBodyStale(request.body)
    if (request.kind === 'saveProfile') return profileStale(request.profile)
    return false
  }, [applyBodyStale, pendingRestart, profileStale])

  const bulkPrompt = useMemo(
    () => mergePrompts(heldOutcomes.map(outcome => outcome.reload).filter((p): p is ReloadPrompt => Boolean(p))),
    [heldOutcomes],
  )

  const bulkStats = useMemo(() => {
    const held = (status: OutcomeStatus) => heldOutcomes.filter(outcome => outcome.status === status).length
    return {
      applied: Math.max((bulkResult?.applied ?? 0) - held('applied'), 0),
      failed: Math.max((bulkResult?.failed ?? 0) - held('failed'), 0),
      disabled: Math.max((bulkResult?.disabled ?? 0) - held('disabled'), 0),
      skipped: Math.max((bulkResult?.skipped ?? 0) - held('skipped'), 0),
    }
  }, [bulkResult, heldOutcomes])

  const outcomeRollups = useMemo(
    () =>
      buildNodeRollups(
        bulkResult?.outcomes ?? [],
        id => nodeById.get(id)?.name ?? `#${id}`,
        id => nodeById.get(id)?.status ?? null,
        t('contentFilter.rollupFleetNode', { defaultValue: 'Every node that carries it' }),
        t('contentFilter.wholeNode', { defaultValue: 'whole node' }),
      ),
    [bulkResult, nodeById, t],
  )

  const carriersOfTag = useCallback(
    (tag: string): number[] | null => {
      const supported = caps.tagSupport(tag).supportedNodeIds
      if (supported.length) return supported
      const entry = fleetEndpoints.find(item => item.tag === tag)
      return entry ? entry.nodeIds : null
    },
    [caps, fleetEndpoints],
  )

  const outcomeTotals = useMemo(() => {
    const pinned = new Set<number>()
    const fleetNodes = new Set<number>()
    let endpoints = 0
    let fleetTargets = 0
    let fleetUnresolved = 0
    for (const rollup of outcomeRollups) {
      endpoints += rollup.endpoints.length
      if (rollup.nodeId !== null) {
        pinned.add(rollup.nodeId)
        continue
      }
      fleetTargets += rollup.endpoints.length
      for (const item of rollup.endpoints) {
        const tag = item.outcome.inbound_tag
        const carriers = tag ? carriersOfTag(tag) : null
        if (carriers === null) {
          fleetUnresolved += 1
          continue
        }
        carriers.forEach(id => fleetNodes.add(id))
      }
    }
    const resolution: 'known' | 'partial' | 'unknown' =
      fleetTargets === 0 || fleetUnresolved === 0 ? 'known' : fleetNodes.size > 0 ? 'partial' : 'unknown'
    return {
      endpoints,
      nodes: new Set([...pinned, ...fleetNodes]).size,
      pinnedNodes: pinned.size,
      fleetTargets,
      fleetNodes: fleetNodes.size,
      fleetUnresolved,
      resolution,
    }
  }, [outcomeRollups, carriersOfTag])

  const outcomeTotalsText =
    outcomeTotals.resolution === 'known'
      ? t('contentFilter.rollupTotals', {
          endpoints: outcomeTotals.endpoints,
          nodes: outcomeTotals.nodes,
          defaultValue: '{{endpoints}} endpoints on {{nodes}} nodes',
        })
      : outcomeTotals.resolution === 'partial'
        ? t('contentFilter.rollupTotalsPartial', {
            endpoints: outcomeTotals.endpoints,
            nodes: outcomeTotals.nodes,
            defaultValue: '{{endpoints}} endpoints on at least {{nodes}} nodes — the panel cannot resolve the rest',
          })
        : outcomeTotals.pinnedNodes > 0
          ? t('contentFilter.rollupTotalsMixedOpen', {
              endpoints: outcomeTotals.endpoints,
              nodes: outcomeTotals.pinnedNodes,
              defaultValue: '{{endpoints}} endpoints — {{nodes}} nodes by name, plus every node that carries the rest',
            })
          : t('contentFilter.rollupTotalsOpen', {
              count: outcomeTotals.endpoints,
              defaultValue: '{{count}} endpoints, on every node that carries them',
            })

  const fleetSpanText =
    outcomeTotals.resolution === 'known'
      ? t('contentFilter.rollupFleetSpan', {
          count: outcomeTotals.fleetTargets,
          nodes: outcomeTotals.fleetNodes,
          defaultValue:
            '{{count}} of them are pinned to no node — they apply on every node that carries them, {{nodes}} right now.',
        })
      : outcomeTotals.resolution === 'partial'
        ? t('contentFilter.rollupFleetPartial', {
            count: outcomeTotals.fleetTargets,
            nodes: outcomeTotals.fleetNodes,
            defaultValue:
              '{{count}} of them are pinned to no node — they apply on every node that carries them, at least {{nodes}} right now, and the panel cannot resolve the rest.',
          })
        : t('contentFilter.rollupFleetOpen', {
            count: outcomeTotals.fleetTargets,
            defaultValue:
              '{{count}} of them are pinned to no node — they apply on every node that carries them, and the panel cannot say how many that is.',
          })

  const fleetNodesLabel =
    outcomeTotals.resolution === 'known'
      ? t('contentFilter.rollupFleetNodes', { count: outcomeTotals.fleetNodes, defaultValue: '{{count}} nodes right now' })
      : outcomeTotals.resolution === 'partial'
        ? t('contentFilter.rollupFleetNodesAtLeast', {
            count: outcomeTotals.fleetNodes,
            defaultValue: 'at least {{count}} nodes right now',
          })
        : t('contentFilter.rollupFleetNodesOpen', { defaultValue: 'node count unknown' })

  const assignGroups = useMemo(
    () =>
      buildAssignmentGroups(
        profileAssignments,
        id => nodeById.get(id),
        id => nodeById.get(id)?.name ?? `#${id}`,
        carriersOfTag,
        t('contentFilter.wholeNode', { defaultValue: 'whole node' }),
      ),
    [profileAssignments, nodeById, carriersOfTag, t],
  )

  const assignTotals = useMemo(() => {
    const pinnedNodes = new Set<number>()
    const fleetNodes = new Set<number>()
    let endpoints = 0
    let fleetTargets = 0
    let fleetUnresolved = 0
    for (const row of assignGroups) {
      if (row.kind === 'node') {
        endpoints += row.members.length
        if (row.nodeId !== null) pinnedNodes.add(row.nodeId)
        continue
      }
      endpoints += 1
      fleetTargets += 1
      if (row.nodeCount === null) {
        fleetUnresolved += 1
        continue
      }
      for (const member of row.members) if (member.nodeId !== null) fleetNodes.add(member.nodeId)
    }
    const resolution: 'known' | 'partial' | 'unknown' =
      fleetTargets === 0 || fleetUnresolved === 0 ? 'known' : fleetNodes.size > 0 ? 'partial' : 'unknown'
    return {
      endpoints,
      nodes: new Set([...pinnedNodes, ...fleetNodes]).size,
      pinnedNodes: pinnedNodes.size,
      resolution,
    }
  }, [assignGroups])

  const assignTotalsText =
    assignTotals.resolution === 'known'
      ? t('contentFilter.rollupTotals', {
          endpoints: assignTotals.endpoints,
          nodes: assignTotals.nodes,
          defaultValue: '{{endpoints}} endpoints on {{nodes}} nodes',
        })
      : assignTotals.resolution === 'partial'
        ? t('contentFilter.rollupTotalsPartial', {
            endpoints: assignTotals.endpoints,
            nodes: assignTotals.nodes,
            defaultValue: '{{endpoints}} endpoints on at least {{nodes}} nodes — the panel cannot resolve the rest',
          })
        : assignTotals.pinnedNodes > 0
          ? t('contentFilter.rollupTotalsMixedOpen', {
              endpoints: assignTotals.endpoints,
              nodes: assignTotals.pinnedNodes,
              defaultValue: '{{endpoints}} endpoints — {{nodes}} nodes by name, plus every node that carries the rest',
            })
          : t('contentFilter.rollupTotalsOpen', {
              count: assignTotals.endpoints,
              defaultValue: '{{count}} endpoints, on every node that carries them',
            })

  useEffect(() => {
    setGroupOpen(prev => (Object.keys(prev).length ? {} : prev))
  }, [activeId])

  const groupIsOpen = (row: AssignGroup) => groupOpen[row.key] ?? row.worst !== 'applied'

  const setEveryGroup = (next: boolean) => setGroupOpen(Object.fromEntries(assignGroups.map(row => [row.key, next])))

  const everyGroupOpen = assignGroups.length > 0 && assignGroups.every(row => groupIsOpen(row))
  const everyGroupClosed = assignGroups.length > 0 && assignGroups.every(row => !groupIsOpen(row))

  useEffect(() => {
    setRollupOpen(prev => (Object.keys(prev).length ? {} : prev))
  }, [bulkResult])

  const rollupIsOpen = (rollup: NodeRollup) => rollupOpen[rollup.key] ?? rollup.sole !== 'applied'

  const setEveryRollup = (next: boolean) =>
    setRollupOpen(Object.fromEntries(outcomeRollups.map(rollup => [rollup.key, next])))

  const everyRollupOpen = outcomeRollups.length > 0 && outcomeRollups.every(rollup => rollupIsOpen(rollup))
  const everyRollupClosed = outcomeRollups.length > 0 && outcomeRollups.every(rollup => !rollupIsOpen(rollup))

  const unreachedNodes = useMemo(() => {
    if (!assignNodes.length) return [] as string[]
    const reached = new Set(reach.rows.map(row => row.nodeId))
    return assignNodes.filter(id => !reached.has(id)).map(id => nodeById.get(id)?.name ?? `#${id}`)
  }, [assignNodes, reach.rows, nodeById])

  const toggleAssignNode = (id: number, on: boolean) =>
    setAssignNodes(prev => (on ? [...new Set([...prev, id])] : prev.filter(x => x !== id)))

  const toggleAssignTag = (tag: string, on: boolean) =>
    setAssignTags(prev => (on ? [...new Set([...prev, tag])] : prev.filter(x => x !== tag)))

  const addCarriers = (ids: number[]) => setAssignNodes(prev => [...new Set([...prev, ...ids])])

  const resetAssign = () => {
    setAssignNodes([])
    setAssignTags([])
    setBulkResult(null)
    setBulkRequest(null)
    setNodeQuery('')
    setEndpointQuery('')
    setSkipConfirmOpen(false)
  }

  const probeTarget = useMemo(() => {
    for (const a of profileAssignments) {
      if (a.node_id !== null) return { nodeId: a.node_id, tag: a.inbound_tag }
      const carrier = (targets.data ?? []).find(
        n => n.routing_service && n.inbounds.some(i => i.tag === a.inbound_tag && i.filterable),
      )
      if (carrier) return { nodeId: carrier.id, tag: a.inbound_tag }
    }
    return null
  }, [profileAssignments, targets.data])

  const targetCount = reach.endpoints

  const sendTargets = () => {
    if (activeId === null) return
    applyTargets.mutate({ body: { profile_id: activeId, node_ids: assignNodes, inbound_tags: assignTags }, confirm: false })
  }

  const submitTargets = () => {
    if (activeId === null) return
    if (applySkips.total > 0) {
      setSkipConfirmOpen(true)
      return
    }
    sendTargets()
  }

  const runRestart = (request: RestartRequest) => {
    if (request.kind === 'saveProfile') saveProfile.mutate({ profile: request.profile, confirm: true })
    else if (request.kind === 'deleteProfile') removeProfile.mutate({ id: request.id, confirm: true })
    else if (request.kind === 'deleteAssignment') removeAssignment.mutate({ id: request.id, confirm: true })
    else applyTargets.mutate({ body: request.body, confirm: true })
  }

  const restartIsWithdrawal =
    pendingRestart?.request.kind === 'deleteProfile' || pendingRestart?.request.kind === 'deleteAssignment'
  const restartNodes = (pendingRestart?.prompt.node_ids ?? []).map(id => nodeLabel(id)).join(', ')
  const restartTags = (pendingRestart?.prompt.inbound_tags ?? []).join(', ')

  const loading = catalog.isLoading || profiles.isLoading

  if (!panelOwner) return null

  return (
    <TooltipProvider delayDuration={150}>
      <div dir={dir} className="w-full space-y-6 px-4 pt-4 pb-10 sm:px-6 sm:pt-6 lg:px-8 lg:pt-8">
        <div className="flex flex-col gap-2 sm:flex-row sm:items-end sm:justify-between">
          <div>
            <h2 className="flex items-center gap-2 text-xl font-semibold">
              <ShieldBan className="size-5 text-primary" />
              {t('contentFilter.title', { defaultValue: 'Content filtering' })}
            </h2>
            <p className="mt-1 max-w-2xl text-sm text-muted-foreground">
              {t('contentFilter.subtitle', {
                defaultValue:
                  'Build a profile from ready-made categories and apply it to one endpoint, so the config you hand out is restricted while nobody else is affected.',
              })}
            </p>
          </div>
          <Button
            size="sm"
            className="gap-1.5"
            onClick={() => {
              setNameDraft('')
              setNameOpen(true)
            }}
          >
            <Plus className="size-4" />
            {t('contentFilter.newProfile', { defaultValue: 'New profile' })}
          </Button>
        </div>

        {loading ? (
          <div className="space-y-3">
            <Skeleton className="h-24 w-full" />
            <Skeleton className="h-64 w-full" />
          </div>
        ) : !profiles.data?.length ? (
          <Card className="border-dashed">
            <CardContent className="flex flex-col items-center gap-3 py-14 text-center">
              <ShieldBan className="size-10 text-muted-foreground/50" />
              <div>
                <p className="font-medium">{t('contentFilter.emptyTitle', { defaultValue: 'No filter profiles yet' })}</p>
                <p className="mx-auto mt-1 max-w-md text-sm text-muted-foreground">
                  {t('contentFilter.emptyBody', {
                    defaultValue:
                      'A profile is a set of categories to block. Create one, tick what should be unreachable, then choose which endpoint it applies to.',
                  })}
                </p>
              </div>
              <Button
                className="gap-1.5"
                onClick={() => {
                  setNameDraft('')
                  setNameOpen(true)
                }}
              >
                <Plus className="size-4" />
                {t('contentFilter.createFirst', { defaultValue: 'Create the first profile' })}
              </Button>
            </CardContent>
          </Card>
        ) : (
          <div className="space-y-6">
            <div className="flex flex-wrap items-center gap-2">
              {profiles.data.map(profile => {
                const count = (assignments.data ?? []).filter(a => a.profile_id === profile.id).length
                const active = profile.id === activeId
                return (
                  <button
                    key={profile.id}
                    onClick={() => setActiveId(profile.id)}
                    className={cn(
                      'flex items-center gap-2 rounded-full border px-3.5 py-1.5 text-sm transition-colors',
                      active ? 'border-primary bg-primary text-primary-foreground' : 'hover:bg-muted/60',
                    )}
                  >
                    <span className="font-medium">{profile.name}</span>
                    <span className={cn('text-xs tabular-nums', active ? 'opacity-75' : 'text-muted-foreground')}>
                      {profile.categories.length}
                      {count ? ` · ${count}` : ''}
                    </span>
                  </button>
                )
              })}
            </div>

            {draft ? (
              <div className="space-y-6">
                <div className="flex items-center gap-2 rounded-lg border bg-card px-3 py-2">
                  <Label htmlFor="cf-name" className="shrink-0 text-xs text-muted-foreground">
                    {t('contentFilter.name', { defaultValue: 'Profile name' })}
                  </Label>
                  <Input
                    id="cf-name"
                    value={draft.name}
                    onChange={e => setDraft({ ...draft, name: e.target.value })}
                    className="h-8 flex-1 border-0 bg-transparent px-2 text-sm font-medium shadow-none focus-visible:ring-1"
                  />
                  <Button
                    variant="ghost"
                    size="icon"
                    className="size-8 shrink-0 text-muted-foreground hover:text-destructive"
                    aria-label={t('contentFilter.delete', { defaultValue: 'Delete' })}
                    onClick={() => setDeleteOpen(true)}
                  >
                    <Trash2 className="size-4" />
                  </Button>
                </div>

                <SectionHeading
                  title={t('contentFilter.section.categories', { defaultValue: 'What to block' })}
                  hint={t('contentFilter.section.categoriesHint', {
                    defaultValue: 'Switch a whole group off, or open one and pick the services inside it.',
                  })}
                />

                <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
                  {(catalog.data?.groups ?? []).map(group => {
                    const umbrella = selected.has(group.key)
                    const picked = group.services.filter(x => selected.has(x.key)).length
                    const whole = umbrella || (group.services.length > 0 && picked === group.services.length)
                    return (
                      <GroupTile
                        key={group.key}
                        groupKey={group.key}
                        size={group.domains}
                        picked={picked}
                        total={group.services.length}
                        whole={whole}
                        onWhole={on => toggleGroup(group.key, on)}
                        onOpen={() => setPicker(`g:${group.key}`)}
                      />
                    )
                  })}
                </div>

                <SectionHeading
                  title={t('contentFilter.section.lists', { defaultValue: 'Ready-made lists' })}
                  hint={t('contentFilter.section.listsHint', {
                    defaultValue: 'Curated blocklists that ship with the node — picking one costs the rule nothing extra.',
                  })}
                  action={
                    <label className="flex items-center gap-2 text-xs font-normal text-muted-foreground">
                      {t('contentFilter.selectAll', { defaultValue: 'Select all' })}
                      <Switch checked={everyListPicked} onCheckedChange={toggleEveryList} />
                    </label>
                  }
                />

                <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
                  {(catalog.data?.lists ?? []).map(group => (
                    <GroupTile
                      key={group.key}
                      groupKey={group.key}
                      size={group.domains}
                      picked={group.entries.filter(x => selected.has(x.key)).length}
                      total={group.entries.length}
                      whole={group.entries.length > 0 && group.entries.every(x => selected.has(x.key))}
                      onWhole={on => toggleList(group.key, on)}
                      onOpen={() => setPicker(`l:${group.key}`)}
                    />
                  ))}
                </div>

                <SectionHeading
                  title={t('contentFilter.section.hardening', { defaultValue: 'How strict to be' })}
                  hint={t('contentFilter.section.hardeningHint', {
                    defaultValue: 'Two settings that decide what happens beyond the categories above.',
                  })}
                />

                <Card>
                  <CardHeader className="pb-3">
                    <div className="flex flex-row items-start justify-between gap-4 space-y-0">
                      <div>
                        <CardTitle className="flex items-center gap-2 text-base">
                          <ShieldAlert className="size-4 text-amber-600 dark:text-amber-400" />
                          {t('contentFilter.strictTitle', { defaultValue: 'Block what cannot be identified' })}
                        </CardTitle>
                        <CardDescription className="mt-1.5 max-w-xl">
                          {t('contentFilter.strictBody', {
                            defaultValue:
                              'Some connections hide their destination name, so no category can match them. With this on they are refused — which also blocks the occasional legitimate app that connects by address. With it off, they pass, and that is a way around the filter.',
                          })}
                        </CardDescription>
                      </div>
                      <Switch
                        checked={draft.strict_mode}
                        onCheckedChange={on => setDraft({ ...draft, strict_mode: on })}
                      />
                    </div>
                  </CardHeader>
                </Card>

                <Collapsible>
                  <CollapsibleTrigger asChild>
                    <Button variant="ghost" size="sm" className="gap-1.5 text-muted-foreground">
                      <ChevronDown className="size-4" />
                      {t('contentFilter.exceptions', { defaultValue: 'Exceptions for single sites' })}
                    </Button>
                  </CollapsibleTrigger>
                  <CollapsibleContent>
                    <Card className="mt-2">
                      <CardContent className="grid gap-6 pt-6 md:grid-cols-2">
                        <DomainList
                          label={t('contentFilter.allowTitle', { defaultValue: 'Always allowed' })}
                          hint={t('contentFilter.allowHint', { defaultValue: 'Reachable even when a category above covers it.' })}
                          values={draft.allow_list}
                          tone="allow"
                          onChange={next => setDraft({ ...draft, allow_list: next })}
                        />
                        <DomainList
                          label={t('contentFilter.blockTitle', { defaultValue: 'Always blocked' })}
                          hint={t('contentFilter.blockHint', { defaultValue: 'Blocked even when no category covers it.' })}
                          values={draft.block_list}
                          tone="block"
                          onChange={next => setDraft({ ...draft, block_list: next })}
                        />
                      </CardContent>
                    </Card>
                  </CollapsibleContent>
                </Collapsible>

                <div className="flex justify-end">
                  <Button onClick={() => saveProfile.mutate({ profile: draft, confirm: false })} disabled={saveProfile.isPending} className="gap-1.5">
                    {saveProfile.isPending ? <Loader2 className="size-4 animate-spin" /> : <ShieldCheck className="size-4" />}
                    {t('contentFilter.save', { defaultValue: 'Save and apply' })}
                  </Button>
                </div>

                <Card>
                  <CardHeader className="flex flex-row flex-wrap items-center justify-between gap-2 space-y-0 pb-3">
                    <div>
                      <CardTitle className="text-base">{t('contentFilter.whereTitle', { defaultValue: 'Where it applies' })}</CardTitle>
                      <CardDescription className="mt-1">
                        {t('contentFilter.whereBody', {
                          defaultValue: 'Only customers whose config uses one of these endpoints are affected.',
                        })}
                      </CardDescription>
                    </div>
                    <Button size="sm" variant="secondary" className="gap-1.5" onClick={() => setAssignOpen(true)}>
                      <Plus className="size-4" />
                      {t('contentFilter.applyTo', { defaultValue: 'Choose where it applies' })}
                    </Button>
                  </CardHeader>
                  <CardContent>
                    {profileAssignments.length === 0 ? (
                      <p className="py-6 text-center text-sm text-muted-foreground">
                        {t('contentFilter.noEndpoints', {
                          defaultValue: 'Not applied anywhere yet, so nobody is affected by this profile.',
                        })}
                      </p>
                    ) : (
                      <div className="space-y-3">
                        <div className="flex flex-wrap items-center justify-between gap-2">
                          <span className="break-words text-xs tabular-nums text-muted-foreground">
                            {assignTotalsText}
                          </span>
                          <div className="inline-flex shrink-0 overflow-hidden rounded-md border">
                            <Button
                              type="button"
                              variant="ghost"
                              size="sm"
                              className="h-7 rounded-none px-2.5 text-xs"
                              disabled={everyGroupOpen}
                              onClick={() => setEveryGroup(true)}
                            >
                              {t('contentFilter.rollupExpandAll', { defaultValue: 'Expand all' })}
                            </Button>
                            <span aria-hidden className="w-px self-stretch bg-border" />
                            <Button
                              type="button"
                              variant="ghost"
                              size="sm"
                              className="h-7 rounded-none px-2.5 text-xs"
                              disabled={everyGroupClosed}
                              onClick={() => setEveryGroup(false)}
                            >
                              {t('contentFilter.rollupCollapseAll', { defaultValue: 'Collapse all' })}
                            </Button>
                          </div>
                        </div>
                        <div className="space-y-1.5">
                          {assignGroups.map(row => (
                            <AssignmentGroupRow
                              key={row.key}
                              row={row}
                              open={groupIsOpen(row)}
                              onOpenChange={next => setGroupOpen(prev => ({ ...prev, [row.key]: next }))}
                              onRemoveMember={id => removeAssignment.mutate({ id, confirm: false })}
                              onRemoveAll={() =>
                                setLiftGroup({
                                  kind: row.kind,
                                  name: row.name,
                                  ids: row.assignmentIds,
                                  nodes: row.nodeCount,
                                  peers: row.peers,
                                })
                              }
                            />
                          ))}
                        </div>
                      </div>
                    )}
                  </CardContent>
                </Card>

                {profileAssignments.length > 0 ? (
                  <Card>
                    <CardHeader className="pb-3">
                      <CardTitle className="flex items-center gap-2 text-base">
                        <Search className="size-4" />
                        {t('contentFilter.checkTitle', { defaultValue: 'Check a site' })}
                      </CardTitle>
                      <CardDescription className="mt-1">
                        {t('contentFilter.checkBody', {
                          defaultValue: 'Ask the node what it would do with one address, without connecting anything.',
                        })}
                      </CardDescription>
                    </CardHeader>
                    <CardContent className="space-y-3">
                      <div className="flex flex-wrap gap-2">
                        <Input
                          value={probeDomain}
                          onChange={e => setProbeDomain(e.target.value)}
                          placeholder={t('contentFilter.domainPlaceholder', { defaultValue: 'example.com' })}
                          className="h-9 min-w-[200px] flex-1"
                          onKeyDown={e => {
                            if (e.key === 'Enter' && probeDomain.trim() && probeTarget) {
                              runProbe.mutate({ node_id: probeTarget.nodeId, inbound_tag: probeTarget.tag, domain: probeDomain.trim() })
                            }
                          }}
                        />
                        <Button
                          variant="secondary"
                          className="h-9"
                          disabled={!probeDomain.trim() || runProbe.isPending || !probeTarget}
                          onClick={() => {
                            if (probeTarget)
                              runProbe.mutate({ node_id: probeTarget.nodeId, inbound_tag: probeTarget.tag, domain: probeDomain.trim() })
                          }}
                        >
                          {runProbe.isPending ? <Loader2 className="size-4 animate-spin" /> : t('contentFilter.check', { defaultValue: 'Check' })}
                        </Button>
                      </div>
                      {probeTarget ? (
                        <p className="text-xs text-muted-foreground">
                          {t('contentFilter.probeVia', {
                            node: nodeById.get(probeTarget.nodeId)?.name ?? `#${probeTarget.nodeId}`,
                            endpoint: probeTarget.tag,
                            defaultValue: 'Asking {{node}} about {{endpoint}}.',
                          })}
                        </p>
                      ) : (
                        <p className="text-xs text-muted-foreground">
                          {t('contentFilter.noProbeNode', {
                            defaultValue: 'No connected node currently carries this endpoint, so there is nothing to ask.',
                          })}
                        </p>
                      )}
                      {probeResult ? (
                        <div
                          className={cn(
                            'rounded-lg border px-3 py-2.5 text-sm',
                            probeResult.blocked
                              ? 'border-destructive/40 bg-destructive/10 text-destructive'
                              : 'border-emerald-500/40 bg-emerald-500/10 text-emerald-700 dark:text-emerald-400',
                          )}
                        >
                          <span className="font-medium">{probeResult.domain}</span>
                          {' — '}
                          {probeResult.blocked
                            ? t('contentFilter.wouldBlock', { defaultValue: 'blocked on this endpoint' })
                            : t('contentFilter.wouldAllow', { defaultValue: 'reachable on this endpoint' })}
                        </div>
                      ) : null}
                    </CardContent>
                  </Card>
                ) : null}
              </div>
            ) : null}
          </div>
        )}

        <Dialog open={nameOpen} onOpenChange={setNameOpen}>
          <DialogContent dir={dir} className="sm:max-w-sm">
            <DialogHeader>
              <DialogTitle>{t('contentFilter.newProfile', { defaultValue: 'New profile' })}</DialogTitle>
              <DialogDescription>
                {t('contentFilter.namePromptBody', {
                  defaultValue: 'A short name you will recognise later, like Kids or Teen.',
                })}
              </DialogDescription>
            </DialogHeader>
            <form
              onSubmit={e => {
                e.preventDefault()
                const name = nameDraft.trim()
                if (!name) return
                createProfile.mutate(name)
                setNameOpen(false)
              }}
              className="space-y-4"
            >
              <div className="space-y-1.5">
                <Label htmlFor="cf-new-name">{t('contentFilter.name', { defaultValue: 'Profile name' })}</Label>
                <Input
                  id="cf-new-name"
                  autoFocus
                  value={nameDraft}
                  onChange={e => setNameDraft(e.target.value)}
                  maxLength={64}
                  placeholder={t('contentFilter.namePlaceholder', { defaultValue: 'Kids' })}
                />
              </div>
              <DialogFooter>
                <Button type="button" variant="ghost" onClick={() => setNameOpen(false)}>
                  {t('contentFilter.cancel', { defaultValue: 'Cancel' })}
                </Button>
                <Button type="submit" disabled={!nameDraft.trim() || createProfile.isPending}>
                  {createProfile.isPending ? <Loader2 className="size-4 animate-spin" /> : null}
                  {t('contentFilter.create', { defaultValue: 'Create' })}
                </Button>
              </DialogFooter>
            </form>
          </DialogContent>
        </Dialog>

        <AlertDialog open={deleteOpen} onOpenChange={setDeleteOpen}>
          <AlertDialogContent dir={dir}>
            <AlertDialogHeader>
              <AlertDialogTitle>{t('contentFilter.deleteTitle', { defaultValue: 'Remove this profile?' })}</AlertDialogTitle>
              <AlertDialogDescription>
                {t('contentFilter.confirmDelete', { defaultValue: 'Remove this profile and lift it everywhere it applies?' })}
              </AlertDialogDescription>
            </AlertDialogHeader>
            <AlertDialogFooter>
              <AlertDialogCancel>{t('contentFilter.cancel', { defaultValue: 'Cancel' })}</AlertDialogCancel>
              <AlertDialogAction
                className="bg-destructive text-destructive-foreground hover:bg-destructive/90"
                onClick={() => {
                  if (draft) removeProfile.mutate({ id: draft.id, confirm: false })
                  setDeleteOpen(false)
                }}
              >
                {t('contentFilter.delete', { defaultValue: 'Delete' })}
              </AlertDialogAction>
            </AlertDialogFooter>
          </AlertDialogContent>
        </AlertDialog>

        <AlertDialog
          open={liftGroup !== null}
          onOpenChange={next => {
            if (!next) setLiftGroup(null)
          }}
        >
          <AlertDialogContent dir={dir}>
            <AlertDialogHeader>
              <AlertDialogTitle className="break-words">
                {t('contentFilter.assignLiftTitle', {
                  name: liftGroup?.name ?? '',
                  defaultValue: 'Lift this profile from {{name}}?',
                })}
              </AlertDialogTitle>
              <AlertDialogDescription className="break-words">
                {liftGroup?.kind === 'node'
                  ? t('contentFilter.assignLiftNodeBody', {
                      count: liftGroup?.ids.length ?? 0,
                      defaultValue: '{{count}} endpoints on this node lose the filter.',
                    })
                  : liftGroup?.nodes === null || liftGroup?.nodes === undefined
                    ? t('contentFilter.assignLiftEndpointBodyOpen', {
                        defaultValue:
                          'This endpoint loses the filter on every node that carries it, and the panel cannot say how many that is.',
                      })
                    : t('contentFilter.assignLiftEndpointBody', {
                        count: liftGroup.nodes,
                        defaultValue:
                          'This endpoint loses the filter on every node that carries it — {{count}} nodes right now.',
                      })}{' '}
                {liftGroup?.peers.length
                  ? t('contentFilter.assignLiftAlsoLoses', {
                      names: liftGroup.peers.join(', '),
                      defaultValue: 'Its core config is shared, so {{names}} lose this filter too.',
                    })
                  : t('contentFilter.assignLiftNothingElse', { defaultValue: 'Nothing else is touched.' })}
              </AlertDialogDescription>
            </AlertDialogHeader>
            <AlertDialogFooter>
              <AlertDialogCancel>{t('contentFilter.cancel', { defaultValue: 'Cancel' })}</AlertDialogCancel>
              <AlertDialogAction
                className="bg-destructive text-destructive-foreground hover:bg-destructive/90"
                onClick={() => {
                  for (const id of liftGroup?.ids ?? []) removeAssignment.mutate({ id, confirm: false })
                  setLiftGroup(null)
                }}
              >
                {t('contentFilter.remove', { defaultValue: 'Remove' })}
              </AlertDialogAction>
            </AlertDialogFooter>
          </AlertDialogContent>
        </AlertDialog>

        {(() => {
          if (!picker || !draft) return null
          const [kind, key] = [picker.slice(0, 1), picker.slice(2)]
          const group = kind === 'g' ? catalog.data?.groups.find(g => g.key === key) : undefined
          const list = kind === 'l' ? catalog.data?.lists.find(g => g.key === key) : undefined
          const entries = group
            ? group.services.map(x => ({ key: x.key, label: x.label, domains: x.domains, icon: x.icon }))
            : (list?.entries ?? [])
          const keys = entries.map(x => x.key)
          const locked = Boolean(group && selected.has(group.key))
          return (
            <PickerSheet
              open
              onOpenChange={v => !v && setPicker(null)}
              groupKey={key}
              title={t(`contentFilter.groups.${key}`, { defaultValue: key })}
              entries={entries}
              selected={selected}
              locked={locked}
              onToggle={toggleService}
              onAll={() => setDraft({ ...draft, categories: [...new Set([...draft.categories, ...keys])] })}
              onNone={() => setDraft({ ...draft, categories: draft.categories.filter(k => !keys.includes(k)) })}
            />
          )
        })()}

        <Dialog
          open={assignOpen}
          onOpenChange={next => {
            setAssignOpen(next)
            if (!next) resetAssign()
          }}
        >
          <DialogContent
            dir={dir}
            className="flex max-h-[85vh] w-[calc(100vw_-_1.5rem)] max-w-none flex-col gap-0 overflow-hidden p-0 sm:w-[calc(100vw_-_4rem)] lg:max-w-5xl xl:max-w-6xl"
          >
            <DialogHeader className="shrink-0 border-b px-5 pb-4 pe-12 pt-5">
              <DialogTitle>{t('contentFilter.bulkTitle', { defaultValue: 'Apply to nodes and endpoints' })}</DialogTitle>
              <DialogDescription className="break-words">
                {t('contentFilter.bulkBody', {
                  defaultValue:
                    'Tick as many nodes and endpoints as you like. Nodes with nothing ticked in the endpoint list take every endpoint they carry, and endpoints with no node ticked apply on every node that has them.',
                })}
              </DialogDescription>
            </DialogHeader>

            <div className="min-h-0 min-w-0 flex-1 overflow-y-auto overflow-x-hidden px-5 py-4">
            {bulkResult ? (
              <div className="space-y-3">
                <div className="flex flex-wrap gap-2">
                  <Badge className="gap-1 border-emerald-500/40 bg-emerald-500/10 text-emerald-700 hover:bg-emerald-500/10 dark:text-emerald-400">
                    <ShieldCheck className="size-3" />
                    {t('contentFilter.bulkStatApplied', {
                      applied: bulkStats.applied,
                      defaultValue: 'In force: {{applied}}',
                    })}
                  </Badge>
                  {bulkStats.failed > 0 ? (
                    <Badge variant="outline" className="gap-1 border-destructive/40 bg-destructive/10 text-destructive">
                      <ShieldAlert className="size-3" />
                      {t('contentFilter.bulkStatFailed', {
                        failed: bulkStats.failed,
                        defaultValue: 'Failed: {{failed}}',
                      })}
                    </Badge>
                  ) : null}
                  {bulkStats.disabled > 0 ? (
                    <Badge variant="secondary" className="gap-1 font-normal">
                      {t('contentFilter.bulkStatDisabled', {
                        disabled: bulkStats.disabled,
                        defaultValue: 'Not active: {{disabled}}',
                      })}
                    </Badge>
                  ) : null}
                  {bulkStats.skipped > 0 ? (
                    <Badge
                      variant="outline"
                      className="gap-1 border-amber-500/40 bg-amber-500/10 text-amber-700 dark:text-amber-400"
                    >
                      <ShieldAlert className="size-3" />
                      {t('contentFilter.bulkStatSkipped', {
                        skipped: bulkStats.skipped,
                        defaultValue: 'Skipped: {{skipped}}',
                      })}
                    </Badge>
                  ) : null}
                </div>

                {bulkPrompt ? (
                  <div className="flex flex-wrap items-center gap-x-3 gap-y-2 rounded-lg border border-amber-500/40 bg-amber-500/[0.07] px-3 py-2.5">
                    <RotateCw className="size-4 shrink-0 text-amber-600 dark:text-amber-400" />
                    <p className="min-w-0 flex-1 text-xs leading-snug text-amber-700 dark:text-amber-400">
                      {bulkSnapshotStale
                        ? t('contentFilter.reloadStale', {
                            defaultValue:
                              'Your selection changed since this result. Apply again to get a fresh restart confirmation.',
                          })
                        : t('contentFilter.reloadBulkPending', {
                            count: heldOutcomes.length,
                            defaultValue: '{{count}} endpoints are waiting for you to confirm a restart',
                          })}
                    </p>
                    <Button
                      size="sm"
                      variant="secondary"
                      className="h-7 shrink-0 gap-1.5 px-2.5 text-xs"
                      disabled={!bulkRequest || bulkSnapshotStale || applyTargets.isPending}
                      onClick={() => {
                        if (!bulkRequest || bulkSnapshotStale) return
                        setPendingRestart({
                          prompt: bulkPrompt,
                          request: { kind: 'applyTargets', body: bulkRequest },
                        })
                      }}
                    >
                      {applyTargets.isPending ? <Loader2 className="size-3.5 animate-spin" /> : null}
                      {t('contentFilter.reloadConfirm', { defaultValue: 'Restart and apply' })}
                    </Button>
                  </div>
                ) : null}
                <p className="text-xs text-muted-foreground">
                  {t('contentFilter.bulkStatWrites', {
                    created: bulkResult.created,
                    updated: bulkResult.updated ?? 0,
                    defaultValue: '{{created}} new and {{updated}} updated in the database',
                  })}
                </p>
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <div className="flex min-w-0 flex-wrap items-baseline gap-x-2 gap-y-1">
                    <span className="break-words text-xs tabular-nums text-muted-foreground">{outcomeTotalsText}</span>
                    {outcomeTotals.fleetTargets > 0 ? (
                      <span className="break-words text-xs font-medium text-amber-700 dark:text-amber-400">
                        {fleetSpanText}
                      </span>
                    ) : null}
                  </div>
                  <div className="inline-flex shrink-0 overflow-hidden rounded-md border">
                    <Button
                      type="button"
                      variant="ghost"
                      size="sm"
                      className="h-7 rounded-none px-2.5 text-xs"
                      disabled={everyRollupOpen}
                      onClick={() => setEveryRollup(true)}
                    >
                      {t('contentFilter.rollupExpandAll', { defaultValue: 'Expand all' })}
                    </Button>
                    <span aria-hidden className="w-px self-stretch bg-border" />
                    <Button
                      type="button"
                      variant="ghost"
                      size="sm"
                      className="h-7 rounded-none px-2.5 text-xs"
                      disabled={everyRollupClosed}
                      onClick={() => setEveryRollup(false)}
                    >
                      {t('contentFilter.rollupCollapseAll', { defaultValue: 'Collapse all' })}
                    </Button>
                  </div>
                </div>
                <div className="max-h-72 space-y-1.5 overflow-y-auto pe-1">
                  {outcomeRollups.map(rollup => (
                    <RollupNodeRow
                      key={rollup.key}
                      rollup={rollup}
                      open={rollupIsOpen(rollup)}
                      onOpenChange={next => setRollupOpen(prev => ({ ...prev, [rollup.key]: next }))}
                      nodeLabel={nodeLabel}
                      fleetNodesLabel={fleetNodesLabel}
                    />
                  ))}
                </div>
              </div>
            ) : (
              <div className="space-y-4">
                <ScopeLine
                  nodes={reach.rows.length}
                  endpoints={reach.endpoints}
                  fleetWide={reach.fleetWide}
                  spill={reach.spill.length}
                  skippedByNode={applySkips.nodeTargets}
                  skippedByEndpoint={applySkips.unfilterableTargets}
                  pickedNodes={assignNodes.length}
                  pickedTags={assignTags.length}
                />

                <div className="grid min-w-0 gap-4 lg:grid-cols-2">
                <section className="min-w-0 space-y-2 rounded-xl border bg-card/40 p-3">
                  <PickerHeader
                    title={t('contentFilter.bulkNodes', { defaultValue: 'Nodes' })}
                    picked={assignNodes.length}
                    onAll={() => addCarriers(selectableNodes.map(node => node.id))}
                    onNone={() => setAssignNodes([])}
                    allDisabled={selectableNodes.length === 0 || everyNodePicked}
                    noneDisabled={assignNodes.length === 0}
                  />
                  <p className="text-xs leading-relaxed text-muted-foreground">
                    {t('contentFilter.bulkNodesHint', {
                      defaultValue: 'A ticked node with no endpoint ticked below covers every endpoint it carries.',
                    })}{' '}
                    {t('contentFilter.bulkSelectScope', {
                      defaultValue: 'All and None only touch the rows listed right now.',
                    })}
                  </p>
                  <div className="relative">
                    <Search className="pointer-events-none absolute start-3 top-1/2 size-3.5 -translate-y-1/2 text-muted-foreground" />
                    <Input
                      value={nodeQuery}
                      onChange={e => setNodeQuery(e.target.value)}
                      placeholder={t('contentFilter.searchNodes', { defaultValue: 'Search nodes by name or id' })}
                      className="h-8 w-full min-w-0 ps-9 text-xs"
                    />
                  </div>
                  <div className="h-64 space-y-0.5 overflow-y-auto pe-1">
                    {(targets.data ?? []).length === 0 ? (
                      <p className="py-4 text-center text-sm text-muted-foreground">
                        {t('contentFilter.noNodes', { defaultValue: 'No node is registered yet.' })}
                      </p>
                    ) : visibleNodes.length === 0 ? (
                      <p className="py-4 text-center text-sm text-muted-foreground">
                        {t('contentFilter.noMatch', { defaultValue: 'Nothing matches that.' })}
                      </p>
                    ) : (
                      visibleNodes.map(node => {
                        const support = caps.nodeSupport(node.id)
                        const blocked = Boolean(node.reason) || !support.supported
                        const blockedText = node.reason ?? reasonText(support.reason)
                        const on = pickedNodes.has(node.id)
                        const peers = node.shares_core_with ?? []
                        const filterable = node.inbounds.filter(inbound => inbound.filterable).length
                        const rowId = `cf-node-${node.id}`
                        return (
                          <div
                            key={node.id}
                            className={cn(
                              'flex items-start gap-3 rounded-md border-s-2 px-3 py-2 transition-colors',
                              blocked
                                ? 'border-s-transparent opacity-55'
                                : on
                                  ? 'border-s-primary bg-primary/[0.06]'
                                  : 'border-s-transparent hover:border-s-border hover:bg-muted/50',
                            )}
                          >
                            <Checkbox
                              id={rowId}
                              className="mt-0.5 size-5"
                              checked={on}
                              disabled={blocked}
                              onCheckedChange={v => toggleAssignNode(node.id, v === true)}
                            />
                            <div className="min-w-0 flex-1">
                              <div className="flex flex-wrap items-center gap-2">
                                <NodeStatusDot status={node.status} />
                                <label
                                  htmlFor={rowId}
                                  title={node.name}
                                  className={cn(
                                    'min-w-0 truncate text-sm font-medium',
                                    blocked ? 'cursor-not-allowed' : 'cursor-pointer',
                                  )}
                                >
                                  {node.name}
                                </label>
                                <span className="shrink-0 text-xs tabular-nums text-muted-foreground">
                                  {t('contentFilter.nodeIdLabel', { id: node.id, defaultValue: 'id {{id}}' })}
                                </span>
                                {peers.length ? (
                                  <Tooltip>
                                    <TooltipTrigger asChild>
                                      <button
                                        type="button"
                                        className="inline-flex h-5 shrink-0 items-center gap-1 rounded-full border border-amber-500/40 bg-amber-500/10 px-1.5 text-[11px] font-normal text-amber-700 hover:bg-amber-500/15 dark:text-amber-400"
                                      >
                                        <Layers className="size-3" />
                                        {t('contentFilter.sharesCore', {
                                          peers: peers.length,
                                          defaultValue: 'shared core · {{peers}}',
                                        })}
                                      </button>
                                    </TooltipTrigger>
                                    <TooltipContent className="max-w-[280px] leading-snug">
                                      {node.scope_note ??
                                        t('contentFilter.sharesCoreWith', {
                                          names: peers.map(id => nodeLabel(id)).join(', '),
                                          defaultValue: 'Uses the same configuration as {{names}}.',
                                        })}
                                    </TooltipContent>
                                  </Tooltip>
                                ) : null}
                              </div>
                              <p className="mt-0.5 break-words text-xs leading-snug text-muted-foreground">
                                {blocked
                                  ? t('contentFilter.capBlockedNode', {
                                      reason: blockedText ?? t('contentFilter.noRouting', { defaultValue: 'cannot filter' }),
                                      defaultValue: 'Cannot be used — {{reason}}',
                                    })
                                  : t('contentFilter.nodeFilterableCount', {
                                      filterable,
                                      total: node.inbounds.length,
                                      defaultValue: '{{filterable}} of {{total}} endpoints can be filtered',
                                    })}
                              </p>
                            </div>
                          </div>
                        )
                      })
                    )}
                  </div>
                </section>

                <section className="min-w-0 space-y-2 rounded-xl border bg-card/40 p-3">
                  <PickerHeader
                    title={t('contentFilter.bulkEndpoints', { defaultValue: 'Endpoints' })}
                    picked={assignTags.length}
                    onAll={() =>
                      setAssignTags(prev => [...new Set([...prev, ...selectableEndpoints.map(entry => entry.tag)])])
                    }
                    onNone={() => setAssignTags([])}
                    allDisabled={selectableEndpoints.length === 0 || everyEndpointPicked}
                    noneDisabled={assignTags.length === 0}
                  />
                  <p className="text-xs leading-relaxed text-muted-foreground">
                    {t('contentFilter.bulkEndpointsHint', {
                      defaultValue: 'With no node ticked above, each endpoint here is filtered on every node that carries it.',
                    })}{' '}
                    {t('contentFilter.endpointAcrossNodes', {
                      defaultValue: 'To use one endpoint on several nodes, tick it here and tick those nodes above.',
                    })}
                  </p>
                  <div className="relative">
                    <Search className="pointer-events-none absolute start-3 top-1/2 size-3.5 -translate-y-1/2 text-muted-foreground" />
                    <Input
                      value={endpointQuery}
                      onChange={e => setEndpointQuery(e.target.value)}
                      placeholder={t('contentFilter.searchEndpoints', { defaultValue: 'Search endpoints by tag or protocol' })}
                      className="h-8 w-full min-w-0 ps-9 text-xs"
                    />
                  </div>
                  <div className="h-64 space-y-0.5 overflow-y-auto pe-1">
                    {fleetEndpoints.length === 0 ? (
                      <p className="py-4 text-center text-sm text-muted-foreground">
                        {t('contentFilter.noFleetEndpoints', { defaultValue: 'No filterable endpoint exists on any node yet.' })}
                      </p>
                    ) : visibleEndpoints.length === 0 ? (
                      <p className="py-4 text-center text-sm text-muted-foreground">
                        {t('contentFilter.noMatch', { defaultValue: 'Nothing matches that.' })}
                      </p>
                    ) : (
                      visibleEndpoints.map((entry, index) => {
                        const on = pickedTags.has(entry.tag)
                        const tagCap = caps.tagSupport(entry.tag)
                        const usable = entry.nodeIds.length > 0 && tagCap.supported
                        const partial = usable ? skippedCarriers(entry.tag, entry.nodeIds) : null
                        const elsewhere = on && assignNodes.length > 0 && !entry.nodeIds.some(id => pickedNodes.has(id))
                        const rowId = `cf-endpoint-${index}`
                        return (
                          <div
                            key={entry.tag}
                            className={cn(
                              'flex items-start gap-3 rounded-md border-s-2 px-3 py-2 transition-colors',
                              !usable
                                ? 'border-s-transparent opacity-55'
                                : on
                                  ? 'border-s-primary bg-primary/[0.06]'
                                  : 'border-s-transparent hover:border-s-border hover:bg-muted/50',
                            )}
                          >
                            <Checkbox
                              id={rowId}
                              className="mt-0.5 size-5"
                              checked={on}
                              disabled={!usable}
                              onCheckedChange={v => toggleAssignTag(entry.tag, v === true)}
                            />
                            <div className="min-w-0 flex-1">
                              <div className="flex flex-wrap items-center gap-2">
                                <label
                                  htmlFor={rowId}
                                  title={entry.tag}
                                  className={cn(
                                    'min-w-0 truncate text-sm font-medium',
                                    usable ? 'cursor-pointer' : 'cursor-not-allowed',
                                  )}
                                >
                                  {entry.tag}
                                </label>
                                <Badge variant="secondary" className="h-5 shrink-0 px-1.5 text-[11px] font-normal">
                                  {entry.protocol}
                                </Badge>
                                {usable ? (
                                  <Tooltip>
                                    <TooltipTrigger asChild>
                                      <button
                                        type="button"
                                        className="shrink-0 rounded text-[11px] tabular-nums text-muted-foreground underline-offset-2 hover:underline"
                                      >
                                        {t('contentFilter.onNodes', {
                                          count: entry.nodeIds.length,
                                          defaultValue: 'on {{count}} nodes',
                                        })}
                                      </button>
                                    </TooltipTrigger>
                                    <TooltipContent className="max-w-[260px] leading-snug">
                                      {t('contentFilter.carriedBy', {
                                        names: entry.nodeIds.map(id => nodeLabel(id)).join(', '),
                                        defaultValue: 'Carried by {{names}}.',
                                      })}
                                    </TooltipContent>
                                  </Tooltip>
                                ) : null}
                                {usable ? (
                                  <Button
                                    type="button"
                                    variant="ghost"
                                    size="sm"
                                    className="h-6 shrink-0 gap-1 px-2 text-[11px]"
                                    onClick={() => addCarriers(entry.nodeIds)}
                                  >
                                    <Server className="size-3" />
                                    {t('contentFilter.pickCarriers', {
                                      nodes: entry.nodeIds.length,
                                      defaultValue: 'Tick its {{nodes}} nodes',
                                    })}
                                  </Button>
                                ) : null}
                              </div>
                              {!usable && !tagCap.supported ? (
                                <p className="mt-0.5 break-words text-[11px] leading-snug text-muted-foreground">
                                  {t('contentFilter.capBlockedEndpoint', {
                                    reason:
                                      reasonText(tagCap.reason) ??
                                      entry.blockedReason ??
                                      t('contentFilter.noRouting', { defaultValue: 'cannot filter' }),
                                    defaultValue: 'Cannot be filtered — {{reason}}',
                                  })}
                                </p>
                              ) : !usable ? (
                                <p className="mt-0.5 break-words text-[11px] leading-snug text-muted-foreground">
                                  {t('contentFilter.endpointBlocked', {
                                    reason: entry.blockedReason ?? t('contentFilter.noRouting', { defaultValue: 'cannot filter' }),
                                    defaultValue: 'No node can filter this endpoint — {{reason}}',
                                  })}
                                </p>
                              ) : entry.blockedNodeIds.length ? (
                                <p className="mt-0.5 break-words text-[11px] leading-snug text-muted-foreground">
                                  {t('contentFilter.endpointPartlyBlocked', {
                                    nodes: entry.blockedNodeIds.length,
                                    reason: entry.blockedReason ?? t('contentFilter.noRouting', { defaultValue: 'cannot filter' }),
                                    defaultValue: '{{nodes}} nodes that carry it are left out — {{reason}}',
                                  })}
                                </p>
                              ) : null}
                              {partial && partial.ids.length ? (
                                <p className="mt-0.5 break-words text-[11px] leading-snug text-amber-700 dark:text-amber-400">
                                  {t('contentFilter.endpointCapPartly', {
                                    nodes: partial.ids.length,
                                    reason:
                                      reasonText(partial.reason) ??
                                      t('contentFilter.noRouting', { defaultValue: 'cannot filter' }),
                                    defaultValue: '{{nodes}} of its nodes will be skipped — {{reason}}',
                                  })}
                                </p>
                              ) : null}
                              {elsewhere ? (
                                <p className="mt-0.5 break-words text-[11px] leading-snug text-amber-700 dark:text-amber-400">
                                  {t('contentFilter.endpointOffPicked', {
                                    defaultValue: 'None of the ticked nodes carries this endpoint, so it will be skipped.',
                                  })}
                                </p>
                              ) : null}
                            </div>
                          </div>
                        )
                      })
                    )}
                  </div>
                </section>
                </div>

                <div className="rounded-lg border bg-muted/30 px-3 py-3">
                  <p className="flex items-center gap-2 text-sm font-semibold">
                    <Server className="size-4 shrink-0 text-primary" />
                    {t('contentFilter.reachTitle', { defaultValue: 'Exactly where this lands' })}
                  </p>
                  {reach.rows.length === 0 ? (
                    <p className="mt-1 text-xs leading-relaxed text-muted-foreground">
                      {t('contentFilter.reachEmpty', {
                        defaultValue: 'Nothing is covered yet — tick a node, an endpoint, or both.',
                      })}
                    </p>
                  ) : (
                    <>
                      <p className="mt-1 text-xs tabular-nums text-muted-foreground">
                        {t('contentFilter.reachSummary', {
                          nodes: reach.rows.length,
                          endpoints: reach.endpoints,
                          defaultValue: '{{nodes}} nodes · {{endpoints}} endpoints in total',
                        })}
                      </p>
                      {reach.fleetWide ? (
                        <p className="mt-1 text-xs leading-relaxed text-muted-foreground">
                          {t('contentFilter.reachFleetWide', {
                            defaultValue:
                              'No node is ticked, so every node that carries a ticked endpoint is included — they are all listed here.',
                          })}
                        </p>
                      ) : null}
                      <div className="mt-2 max-h-44 space-y-1 overflow-y-auto pe-1">
                        {reach.rows.map(row => (
                          <div key={row.nodeId} className="rounded-md border bg-background px-2.5 py-1.5">
                            <div className="flex flex-wrap items-center gap-2">
                              <NodeStatusDot status={row.status} />
                              <span title={row.name} className="min-w-0 truncate text-xs font-medium">{row.name}</span>
                              <span className="shrink-0 text-[11px] tabular-nums text-muted-foreground">
                                {t('contentFilter.nodeIdLabel', { id: row.nodeId, defaultValue: 'id {{id}}' })}
                              </span>
                              <DeliveryBadge delivery={row.delivery} />
                              <span className="ms-auto shrink-0 text-[11px] tabular-nums text-muted-foreground">
                                {t('contentFilter.endpointCount', { count: row.tags.length, defaultValue: '{{count}} endpoints' })}
                              </span>
                            </div>
                            <p title={row.tags.join(' · ')} className="mt-0.5 truncate text-[11px] text-muted-foreground">{row.tags.join(' · ')}</p>
                            {row.note ? (
                              <p
                                className={cn(
                                  'mt-0.5 break-words text-[11px] leading-snug',
                                  row.delivery === 'core' ? 'text-amber-700 dark:text-amber-400' : 'text-muted-foreground',
                                )}
                              >
                                {row.note}
                              </p>
                            ) : row.peers.length ? (
                              <p className="mt-0.5 break-words text-[11px] leading-snug text-amber-700 dark:text-amber-400">
                                {t('contentFilter.reachCoreSpill', {
                                  names: row.peers.map(peer => peer.name).join(', '),
                                  defaultValue: 'Goes into a configuration shared with {{names}}, so they enforce it as well.',
                                })}
                              </p>
                            ) : null}
                            {row.delivery === null ? (
                              <p className="mt-0.5 break-words text-[11px] leading-snug text-amber-700 dark:text-amber-400">
                                {t('contentFilter.reachUnknown', {
                                  defaultValue:
                                    'The panel cannot tell whether this stays on this node, so treat every node on the same core configuration as affected.',
                                })}
                              </p>
                            ) : null}
                          </div>
                        ))}
                      </div>
                    </>
                  )}
                </div>

                {reach.spill.length ? (
                  <div className="rounded-lg border border-amber-500/50 bg-amber-500/10 px-3 py-3">
                    <p className="flex items-center gap-2 text-sm font-semibold text-amber-700 dark:text-amber-400">
                      <ShieldAlert className="size-4 shrink-0" />
                      {t('contentFilter.sharedCoreTitle', { defaultValue: 'This reaches nodes you did not tick' })}
                    </p>
                    <p className="mt-1 break-words text-xs leading-relaxed text-amber-800/90 dark:text-amber-300/90">
                      {t('contentFilter.sharedCoreBody', {
                        sources: [...new Set(reach.spill.flatMap(peer => peer.causes))].join(', '),
                        defaultValue:
                          '{{sources}} write this rule into a core configuration that other nodes share, so those nodes enforce it too.',
                      })}
                    </p>
                    <div className="mt-1.5 space-y-0.5">
                      {reach.spill.slice(0, 8).map(peer => (
                        <p key={peer.id} className="break-words text-[11px] font-medium leading-snug text-amber-800 dark:text-amber-300">
                          {t('contentFilter.spillRow', {
                            name: peer.name,
                            id: peer.id,
                            sources: peer.causes.join(', '),
                            defaultValue: '{{name}} (id {{id}}) — carried over from {{sources}}',
                          })}
                        </p>
                      ))}
                      {reach.spill.length > 8 ? (
                        <p className="text-[11px] font-medium text-amber-800 dark:text-amber-300">
                          {t('contentFilter.andMore', { rest: reach.spill.length - 8, defaultValue: '+{{rest}} more' })}
                        </p>
                      ) : null}
                    </div>
                  </div>
                ) : null}

                {reach.unknown.length ? (
                  <div className="rounded-lg border border-amber-500/50 bg-amber-500/10 px-3 py-2.5 text-xs leading-relaxed break-words text-amber-800 dark:text-amber-300">
                    {t('contentFilter.reachUnknownNodes', {
                      names: reach.unknown.join(', '),
                      defaultValue:
                        'The panel cannot tell where the filter lands for {{names}} — assume every node sharing their core configuration is affected.',
                    })}
                  </div>
                ) : null}

                {unreachedTags.length ? (
                  <div className="rounded-lg border border-amber-500/50 bg-amber-500/10 px-3 py-2.5 text-xs leading-relaxed break-words text-amber-800 dark:text-amber-300">
                    {t('contentFilter.unreachedTags', {
                      tags: unreachedTags.join(', '),
                      defaultValue: 'No ticked node carries {{tags}}, so nothing is applied for it.',
                    })}
                  </div>
                ) : null}

                {unreachedNodes.length ? (
                  <div className="rounded-lg border border-amber-500/50 bg-amber-500/10 px-3 py-2.5 text-xs leading-relaxed break-words text-amber-800 dark:text-amber-300">
                    {t('contentFilter.unreachedNodes', {
                      names: unreachedNodes.join(', '),
                      defaultValue: 'Nothing lands on {{names}} — they carry none of the ticked endpoints.',
                    })}
                  </div>
                ) : null}

                <div className="flex flex-wrap items-center justify-between gap-2 rounded-lg border bg-muted/40 px-3 py-2 text-xs text-muted-foreground">
                  <span>
                    {t('contentFilter.bulkPicked', {
                      nodes: assignNodes.length,
                      endpoints: assignTags.length,
                      defaultValue: '{{nodes}} nodes · {{endpoints}} endpoints',
                    })}
                  </span>
                  <span className="tabular-nums">
                    {targetCount > 0
                      ? t('contentFilter.bulkTargets', { targets: targetCount, defaultValue: '{{targets}} targets' })
                      : assignNodes.length || assignTags.length
                        ? t('contentFilter.bulkNoMatch', {
                            defaultValue: 'Those ticks do not meet on any endpoint — applying would change nothing.',
                          })
                        : t('contentFilter.bulkNothing', { defaultValue: 'Tick at least one node or one endpoint.' })}
                  </span>
                </div>
              </div>
            )}
            </div>

            <DialogFooter className="shrink-0 gap-2 border-t bg-muted/30 px-5 py-4 sm:items-center">
              {bulkResult ? (
                <>
                  <Button variant="ghost" onClick={() => {
                      setBulkResult(null)
                      setBulkRequest(null)
                    }}>
                    {t('contentFilter.bulkAgain', { defaultValue: 'Pick more targets' })}
                  </Button>
                  <Button onClick={() => setAssignOpen(false)}>{t('contentFilter.done', { defaultValue: 'Done' })}</Button>
                </>
              ) : (
                <>
                  <Button variant="ghost" onClick={() => setAssignOpen(false)}>
                    {t('contentFilter.cancel', { defaultValue: 'Cancel' })}
                  </Button>
                  <Button
                    disabled={(assignNodes.length === 0 && assignTags.length === 0) || applyTargets.isPending || activeId === null}
                    onClick={() => submitTargets()}
                  >
                    {applyTargets.isPending ? <Loader2 className="size-4 animate-spin" /> : null}
                    {t('contentFilter.apply', { defaultValue: 'Apply' })}
                  </Button>
                </>
              )}
            </DialogFooter>
          </DialogContent>
        </Dialog>

        <AlertDialog open={skipConfirmOpen} onOpenChange={setSkipConfirmOpen}>
          <AlertDialogContent
            dir={dir}
            className="flex max-h-[85vh] w-[calc(100vw_-_1.5rem)] flex-col gap-4 overflow-hidden sm:w-full"
          >
            <AlertDialogHeader className="min-h-0 min-w-0 flex-1 overflow-y-auto overflow-x-hidden">
              <AlertDialogTitle className="flex items-center gap-2 break-words">
                <ShieldAlert className="size-4 shrink-0 text-amber-600 dark:text-amber-400" />
                {t('contentFilter.skipConfirmTitle', { defaultValue: 'Some targets will be skipped' })}
              </AlertDialogTitle>
              <AlertDialogDescription asChild>
                <div className="space-y-3">
                  <p className="break-words">
                    {t('contentFilter.skipConfirmBody', {
                      count: applySkips.total,
                      defaultValue:
                        '{{count}} of the targets you ticked will not be applied. Everything else is applied.',
                    })}
                  </p>
                  {applySkips.unfilterable.length ? (
                    <div className="space-y-0.5 rounded-lg border bg-muted/30 px-3 py-2">
                      <p className="text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">
                        {t('contentFilter.skipConfirmUnfilterable', { defaultValue: 'Endpoints no node can filter' })}
                      </p>
                      <p className="break-words pb-1 text-xs leading-snug text-muted-foreground">
                        {t('contentFilter.skipConfirmUnfilterableBody', {
                          count: applySkips.unfilterable.length,
                          defaultValue:
                            'No node anywhere can filter these, so they are left out everywhere. This is not a problem with any node.',
                        })}
                      </p>
                      {applySkips.unfilterable.slice(0, 8).map(item => (
                        <p key={item.tag} className="break-words text-xs leading-snug text-foreground">
                          {t('contentFilter.skipUnfilterableRow', {
                            tag: item.tag,
                            reason:
                              reasonText(item.reason) ?? t('contentFilter.noRouting', { defaultValue: 'cannot filter' }),
                            defaultValue: '{{tag}} — {{reason}}',
                          })}
                        </p>
                      ))}
                      {applySkips.unfilterable.length > 8 ? (
                        <p className="text-xs text-muted-foreground">
                          {t('contentFilter.andMore', {
                            rest: applySkips.unfilterable.length - 8,
                            defaultValue: '+{{rest}} more',
                          })}
                        </p>
                      ) : null}
                    </div>
                  ) : null}
                  {applySkips.nodes.length ? (
                    <div className="space-y-0.5 rounded-lg border border-amber-500/40 bg-amber-500/[0.07] px-3 py-2">
                      <p className="text-[11px] font-semibold uppercase tracking-wide text-amber-700 dark:text-amber-400">
                        {t('contentFilter.skipConfirmNodes', { defaultValue: 'Nodes that are the obstacle' })}
                      </p>
                      <p className="break-words pb-1 text-xs leading-snug text-muted-foreground">
                        {t('contentFilter.skipConfirmNodesBody', {
                          defaultValue: 'These nodes are what stands in the way, so fixing them brings their endpoints back.',
                        })}
                      </p>
                      {applySkips.nodes.slice(0, 8).map(item => (
                        <p key={item.id} className="break-words text-xs leading-snug text-foreground">
                          {item.kept > 0
                            ? t('contentFilter.skipNodeRowKept', {
                                name: nodeLabel(item.id),
                                id: item.id,
                                lost: item.lost,
                                kept: item.kept,
                                reason:
                                  reasonText(item.reason) ??
                                  t('contentFilter.noRouting', { defaultValue: 'cannot filter' }),
                                defaultValue:
                                  '{{name}} (id {{id}}) — loses {{lost}} endpoints ({{reason}}) and still gets the other {{kept}}',
                              })
                            : t('contentFilter.skipNodeRow', {
                                name: nodeLabel(item.id),
                                id: item.id,
                                reason:
                                  reasonText(item.reason) ??
                                  t('contentFilter.noRouting', { defaultValue: 'cannot filter' }),
                                defaultValue: '{{name}} (id {{id}}) — {{reason}}, so it gets none of the ticked endpoints',
                              })}
                        </p>
                      ))}
                      {applySkips.nodes.length > 8 ? (
                        <p className="text-xs text-muted-foreground">
                          {t('contentFilter.andMore', {
                            rest: applySkips.nodes.length - 8,
                            defaultValue: '+{{rest}} more',
                          })}
                        </p>
                      ) : null}
                    </div>
                  ) : null}
                  {applySkips.endpoints.length ? (
                    <div className="space-y-0.5">
                      <p className="text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">
                        {t('contentFilter.skipConfirmEndpoints', { defaultValue: 'Endpoints that lose nodes' })}
                      </p>
                      {applySkips.endpoints.slice(0, 8).map(item => (
                        <p key={item.tag} className="break-words text-xs leading-snug text-foreground">
                          {t('contentFilter.skipEndpointRow', {
                            tag: item.tag,
                            nodes: item.nodes,
                            reason:
                              reasonText(item.reason) ?? t('contentFilter.noRouting', { defaultValue: 'cannot filter' }),
                            defaultValue: '{{tag}} — {{nodes}} of its nodes are skipped ({{reason}})',
                          })}
                        </p>
                      ))}
                      {applySkips.endpoints.length > 8 ? (
                        <p className="text-xs text-muted-foreground">
                          {t('contentFilter.andMore', {
                            rest: applySkips.endpoints.length - 8,
                            defaultValue: '+{{rest}} more',
                          })}
                        </p>
                      ) : null}
                    </div>
                  ) : null}
                </div>
              </AlertDialogDescription>
            </AlertDialogHeader>
            <AlertDialogFooter className="shrink-0">
              <AlertDialogCancel>{t('contentFilter.cancel', { defaultValue: 'Cancel' })}</AlertDialogCancel>
              <AlertDialogAction
                disabled={activeId === null || applyTargets.isPending}
                onClick={() => {
                  setSkipConfirmOpen(false)
                  sendTargets()
                }}
              >
                {t('contentFilter.skipConfirmAction', { defaultValue: 'Apply to the rest' })}
              </AlertDialogAction>
            </AlertDialogFooter>
          </AlertDialogContent>
        </AlertDialog>

        <AlertDialog
          open={pendingRestart !== null}
          onOpenChange={next => {
            if (!next) setPendingRestart(null)
          }}
        >
          <AlertDialogContent
            dir={dir}
            className="flex max-h-[85vh] w-[calc(100vw_-_1.5rem)] flex-col gap-4 overflow-hidden sm:w-full"
          >
            <AlertDialogHeader className="min-h-0 min-w-0 flex-1 overflow-y-auto overflow-x-hidden">
              <AlertDialogTitle className="flex items-center gap-2 break-words">
                <RotateCw className="size-4 shrink-0 text-amber-600 dark:text-amber-400" />
                {t('contentFilter.reloadTitle', { defaultValue: 'Restart needed before this takes effect' })}
              </AlertDialogTitle>
              <AlertDialogDescription asChild>
                <div className="space-y-2 break-words">
                  {restartStale ? (
                    <p className="font-medium text-amber-700 dark:text-amber-400">
                      {t('contentFilter.reloadStaleRequest', {
                        defaultValue:
                          'What you have on screen changed after this request. Close this and do it again to get a fresh restart confirmation.',
                      })}
                    </p>
                  ) : null}
                  {restartTags ? (
                    <p>
                      {restartIsWithdrawal
                        ? t('contentFilter.reloadWithdrawWhy', {
                            tags: restartTags,
                            defaultValue:
                              'Putting your own setting back on {{tags}} only takes effect once the nodes below restart, and every session on them drops.',
                          })
                        : t('contentFilter.reloadWhy', {
                            tags: restartTags,
                            defaultValue:
                              'Name recovery has to change on {{tags}}. It only takes effect once the nodes below restart, and every session on them drops.',
                          })}
                    </p>
                  ) : null}
                  {restartNodes ? (
                    <p className="font-medium text-foreground">
                      {t('contentFilter.reloadNodes', {
                        nodes: restartNodes,
                        defaultValue: 'Nodes that would restart: {{nodes}}',
                      })}
                    </p>
                  ) : null}
                  {!restartTags && !restartNodes && pendingRestart?.prompt.message ? (
                    <p>{pendingRestart.prompt.message}</p>
                  ) : null}
                </div>
              </AlertDialogDescription>
            </AlertDialogHeader>
            <AlertDialogFooter className="shrink-0">
              <AlertDialogCancel>{t('contentFilter.reloadCancel', { defaultValue: 'Leave it as it is' })}</AlertDialogCancel>
              <AlertDialogAction
                className="bg-destructive text-destructive-foreground hover:bg-destructive/90"
                disabled={restartStale}
                onClick={() => {
                  const request = pendingRestart?.request
                  if (!request || restartStale) return
                  setPendingRestart(null)
                  runRestart(request)
                }}
              >
                {t('contentFilter.reloadConfirm', { defaultValue: 'Restart and apply' })}
              </AlertDialogAction>
            </AlertDialogFooter>
          </AlertDialogContent>
        </AlertDialog>
      </div>
    </TooltipProvider>
  )
}
