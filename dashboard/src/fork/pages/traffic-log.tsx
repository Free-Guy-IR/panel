import { Alert, AlertDescription } from '@/components/ui/alert'
import { AlertDialog, AlertDialogAction, AlertDialogCancel, AlertDialogContent, AlertDialogDescription, AlertDialogFooter, AlertDialogHeader, AlertDialogTitle } from '@/components/ui/alert-dialog'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Checkbox } from '@/components/ui/checkbox'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Popover, PopoverAnchor, PopoverContent } from '@/components/ui/popover'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { Skeleton } from '@/components/ui/skeleton'
import { Switch } from '@/components/ui/switch'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from '@/components/ui/tooltip'
import { useAdmin } from '@/hooks/use-admin'
import { useDebouncedSearch } from '@/hooks/use-debounced-search'
import useDirDetection from '@/hooks/use-dir-detection'
import { cn } from '@/lib/utils'
import { useGetNodesSimple, type AdminDetails, type NodesSimpleResponse, type UsersResponse } from '@/service/api'
import { fetcher } from '@/service/http'
import { getAuthToken } from '@/utils/authStorage'
import { hasPermission, isOwner } from '@/utils/rbac'
import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { logDayTime, logShort, logTime } from '@/fork/lib/log-time'
import { EventSource } from 'eventsource'
import type { TFunction } from 'i18next'
import { Activity, History, Loader2, Pause, Play, Radio, RefreshCw, Trash2, X } from 'lucide-react'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { toast } from 'sonner'

const BASE = '/api/traffic-log'
const USERS_URL = '/api/users'
const ALL_NODES = 'all'
const MAX_LIVE_ROWS = 500
const LIVE_FLUSH_MS = 150
const STATUS_INTERVAL_MS = 5000
const SUGGEST_DEBOUNCE_MS = 250
const SUGGEST_LIMIT = 8
const HISTORY_LIMIT = 100
const PURGE_MAX_ROUNDS = 12
const HOUR_MS = 60 * 60 * 1000
const DEFAULT_RETENTION_HOURS = 48
const CLOCK_MARGIN_MS = 120 * 1000
const STATUS_KEY = ['traffic-log', 'status']
const HISTORY_KEY = ['traffic-log', 'history']
const SUMMARY_KEY = ['traffic-log', 'summary']

type Mode = 'live' | 'history'

type LiveEvent = {
  at: string
  user_id: number | null
  username: string | null
  node_id: number
  node: string | null
  inbound: string
  host: string
  port: number
  protocol: string
  route: string
  refused: boolean
}

type LiveRow = LiveEvent & { key: number }

type ControlMessage = { control: 'dropped'; count: number } | { control: 'paused' } | { control: 'unavailable'; reason: string | null } | { control: 'unknown_user' } | { control: 'revoked' }

type HistoryItem = {
  id: number
  bucket_start: string
  first_seen: string
  last_seen: string
  hits: number
  user_id: number | null
  username: string | null
  user_deleted: boolean
  node_id: number
  node: string | null
  inbound: string
  host: string
  port: number
  protocol: string
  route: string
  refused: boolean
}

type HistoryPage = { items: HistoryItem[]; next_cursor: string | null }

type TrafficSummary = {
  connections: number
  destinations: number
  users: number
  refused: number
  top_users: { user_id: number | null; username: string | null; hits: number }[]
  top_destinations: { host: string; hits: number; refused: number }[]
}

type NodeCollectionStatus = {
  node_id: number
  node: string | null
  state: string
  since: string | null
  lines: number
  events: number
  dropped: number
  records: number
  last_event_at: string | null
  detail: string | null
}

type TrafficStatus = {
  enabled: boolean
  available: boolean
  reason: string | null
  retention_hours: number
  max_records: number
  ceiling_active: boolean
  last_purge_at: string | null
  purged_expired: number
  purged_over_ceiling: number
  nodes: NodeCollectionStatus[]
}

type PurgeResult = {
  removed: number
  incomplete: boolean
  remaining: number
  retention_hours: number
  reclaimed: boolean
  freed_bytes: number | null
}

type PurgeRequest = { hours: number | null; reclaim: boolean }

type PurgeScopeId = 'all' | 'h1' | 'h6' | 'd1' | 'd2' | 'd7'

type PresetId = '15m' | '1h' | '6h' | '24h' | '48h' | 'custom'

type RangeState = { kind: 'ready'; start: string; end: string } | { kind: 'incomplete' } | { kind: 'invalid' } | { kind: 'tooOld' }

const PRESET_MS: Record<Exclude<PresetId, 'custom'>, number> = {
  '15m': 15 * 60 * 1000,
  '1h': 60 * 60 * 1000,
  '6h': 6 * 60 * 60 * 1000,
  '24h': 24 * 60 * 60 * 1000,
  '48h': 48 * 60 * 60 * 1000,
}

const PRESETS: { id: PresetId; label: string; fallback: string }[] = [
  { id: '15m', label: 'trafficLog.presets.m15', fallback: '15 min' },
  { id: '1h', label: 'trafficLog.presets.h1', fallback: '1 hour' },
  { id: '6h', label: 'trafficLog.presets.h6', fallback: '6 hours' },
  { id: '24h', label: 'trafficLog.presets.h24', fallback: '24 hours' },
  { id: '48h', label: 'trafficLog.presets.h48', fallback: '48 hours' },
  { id: 'custom', label: 'trafficLog.presets.custom', fallback: 'Custom' },
]

const RETENTION_CHOICES: { hours: number; label: string; fallback: string }[] = [
  { hours: 1, label: 'trafficLog.retention.h1', fallback: '1 hour' },
  { hours: 3, label: 'trafficLog.retention.h3', fallback: '3 hours' },
  { hours: 6, label: 'trafficLog.retention.h6', fallback: '6 hours' },
  { hours: 12, label: 'trafficLog.retention.h12', fallback: '12 hours' },
  { hours: 24, label: 'trafficLog.retention.d1', fallback: '1 day' },
  { hours: 48, label: 'trafficLog.retention.d2', fallback: '2 days' },
  { hours: 72, label: 'trafficLog.retention.d3', fallback: '3 days' },
  { hours: 168, label: 'trafficLog.retention.d7', fallback: '7 days' },
  { hours: 336, label: 'trafficLog.retention.d14', fallback: '14 days' },
  { hours: 720, label: 'trafficLog.retention.d30', fallback: '30 days' },
]

const PURGE_CHOICES: { id: PurgeScopeId; hours: number | null; label: string; fallback: string }[] = [
  { id: 'all', hours: null, label: 'trafficLog.purge.all', fallback: 'Everything' },
  { id: 'h1', hours: 1, label: 'trafficLog.purge.h1', fallback: 'Older than 1 hour' },
  { id: 'h6', hours: 6, label: 'trafficLog.purge.h6', fallback: 'Older than 6 hours' },
  { id: 'd1', hours: 24, label: 'trafficLog.purge.d1', fallback: 'Older than 1 day' },
  { id: 'd2', hours: 48, label: 'trafficLog.purge.d2', fallback: 'Older than 2 days' },
  { id: 'd7', hours: 168, label: 'trafficLog.purge.d7', fallback: 'Older than 7 days' },
]

const STATE_DOT: Record<string, string> = {
  collecting: 'bg-emerald-500',
  attaching: 'bg-amber-500',
  error: 'bg-destructive',
  detached: 'bg-muted-foreground/40',
  paused: 'bg-muted-foreground/40',
  no_reports: 'bg-amber-500',
  unavailable: 'bg-destructive',
}

const STATE_FALLBACK: Record<string, string> = {
  collecting: 'Collecting',
  attaching: 'Attaching',
  error: 'Error',
  detached: 'Not collecting',
  paused: 'Paused',
  no_reports: 'No destination reports',
  unavailable: 'Unavailable',
}

const apiBaseUrl = () =>
  import.meta.env.VITE_BASE_API && typeof import.meta.env.VITE_BASE_API === 'string' && import.meta.env.VITE_BASE_API.trim() !== '/' && import.meta.env.VITE_BASE_API.startsWith('http')
    ? import.meta.env.VITE_BASE_API
    : window.location.origin

const refusedStatus = (error: unknown): number | null => {
  const status = (error as { status?: unknown })?.status ?? (error as { response?: { status?: unknown } })?.response?.status
  return typeof status === 'number' ? status : null
}


const errorText = (error: unknown, fallback: string): string => {
  const detail = (error as { data?: { detail?: unknown } })?.data?.detail
  if (typeof detail === 'string') return detail
  if (Array.isArray(detail) && detail.length) {
    const first = detail[0] as { msg?: string }
    if (first?.msg) return first.msg
  }
  return (error as Error)?.message || fallback
}

const stateLabel = (state: string, t: TFunction) => t(`trafficLog.state.${state}`, { defaultValue: STATE_FALLBACK[state] ?? state })

const clampToRetention = (iso: string, retentionMs: number) => {
  const floor = Date.now() - retentionMs + CLOCK_MARGIN_MS
  const value = new Date(iso).getTime()
  return value >= floor ? iso : new Date(floor).toISOString()
}

const userLabel = (username: string | null, userId: number | null) => username ?? (userId != null ? `#${userId}` : '—')

const fitsRetention = (id: PresetId, retentionMs: number) => id === 'custom' || PRESET_MS[id] <= retentionMs

const windowLabel = (hours: number, t: TFunction) => {
  const choice = RETENTION_CHOICES.find(entry => entry.hours === hours)
  return choice ? t(choice.label, { defaultValue: choice.fallback }) : t('trafficLog.retention.hours', { hours, defaultValue: '{{hours}} hours' })
}

const freedLabel = (bytes: number, t: TFunction) => {
  const megabytes = bytes / (1024 * 1024)
  return megabytes >= 1024
    ? t('trafficLog.purge.freedGb', { value: (megabytes / 1024).toFixed(1), defaultValue: '{{value}} GB' })
    : t('trafficLog.purge.freedMb', { value: megabytes.toFixed(1), defaultValue: '{{value}} MB' })
}

function OutcomeChip({ refused, route }: { refused: boolean; route: string }) {
  const { t } = useTranslation()
  if (refused) {
    return (
      <Badge variant="outline" className="border-destructive/40 bg-destructive/10 text-destructive font-medium">
        {t('trafficLog.refused', { defaultValue: 'Refused' })}
      </Badge>
    )
  }
  return (
    <div className="flex items-center gap-1.5">
      <Badge variant="outline" className="border-emerald-500/40 bg-emerald-500/10 font-medium text-emerald-700 dark:text-emerald-400">
        {t('trafficLog.accepted', { defaultValue: 'Accepted' })}
      </Badge>
      <span className="text-muted-foreground/70 font-mono text-[10px]">{route}</span>
    </div>
  )
}

function ProtocolChip({ protocol }: { protocol: string }) {
  return (
    <Badge variant="outline" className="text-muted-foreground px-1.5 font-mono text-[10px] uppercase">
      {protocol}
    </Badge>
  )
}

function Destination({ host, port }: { host: string; port: number }) {
  return (
    <span dir="ltr" className="font-mono text-[11px]">
      <bdi className="break-all">{host}</bdi>
      <span className="text-muted-foreground/70">:{port}</span>
    </span>
  )
}

function StatCard({ label, value }: { label: string; value: number }) {
  return (
    <div className="bg-muted/40 rounded-lg border p-3">
      <div className="text-muted-foreground text-[11px] uppercase tracking-wide">{label}</div>
      <div className="mt-1 text-lg font-semibold tabular-nums">{value.toLocaleString()}</div>
    </div>
  )
}

function RetentionSelect({ retentionHours }: { retentionHours: number }) {
  const { t } = useTranslation()
  const queryClient = useQueryClient()

  const options = useMemo(() => {
    const list = RETENTION_CHOICES.map(choice => ({ hours: choice.hours, text: t(choice.label, { defaultValue: choice.fallback }) }))
    if (!list.some(option => option.hours === retentionHours)) {
      list.push({ hours: retentionHours, text: t('trafficLog.retention.hours', { hours: retentionHours, defaultValue: '{{hours}} hours' }) })
      list.sort((first, second) => first.hours - second.hours)
    }
    return list
  }, [retentionHours, t])

  const save = useMutation({
    mutationFn: (hours: number) => fetcher<TrafficStatus>(`${BASE}/settings`, { method: 'PUT', body: { retention_hours: hours } }),
    onSuccess: (_result, hours) => {
      toast.success(t('trafficLog.retention.saved', { hours, defaultValue: 'History is now kept for {{hours}} hours.' }))
      void queryClient.invalidateQueries({ queryKey: STATUS_KEY })
    },
    onError: error => toast.error(errorText(error, t('trafficLog.retention.failed', { defaultValue: 'Could not change how long the log is kept.' }))),
  })

  return (
    <span className="flex items-center gap-2">
      <Label htmlFor="traffic-log-retention" className="text-[11px]">
        {t('trafficLog.retention.label', { defaultValue: 'Keep for' })}
      </Label>
      <Select value={String(retentionHours)} disabled={save.isPending} onValueChange={value => save.mutate(Number(value))}>
        <SelectTrigger id="traffic-log-retention" className="h-7 w-28 text-[11px]">
          <SelectValue />
        </SelectTrigger>
        <SelectContent>
          {options.map(option => (
            <SelectItem key={option.hours} value={String(option.hours)} className="text-xs">
              {option.text}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
    </span>
  )
}

function PurgeButton() {
  const { t } = useTranslation()
  const dir = useDirDetection()
  const queryClient = useQueryClient()
  const [open, setOpen] = useState(false)
  const [scope, setScope] = useState<PurgeScopeId>('all')
  const [reclaim, setReclaim] = useState(false)

  const purge = useMutation({
    mutationFn: async ({ hours, reclaim: wantsReclaim }: PurgeRequest) => {
      const body: Record<string, number | boolean> = wantsReclaim ? { reclaim: true } : {}
      if (hours != null) body.older_than_hours = hours
      let result = await fetcher<PurgeResult>(`${BASE}/purge`, { method: 'POST', body })
      let removed = result.removed
      for (let round = 1; round < PURGE_MAX_ROUNDS && result.incomplete && result.removed > 0; round += 1) {
        result = await fetcher<PurgeResult>(`${BASE}/purge`, { method: 'POST', body })
        removed += result.removed
      }
      return { ...result, removed }
    },
    onSuccess: (result, variables) => {
      const removed = result.removed.toLocaleString()
      const freed = result.freed_bytes != null ? freedLabel(result.freed_bytes, t) : null
      if (result.incomplete) {
        const remaining = result.remaining.toLocaleString()
        toast.info(
          variables.reclaim
            ? t('trafficLog.purge.partialNotReclaimed', {
                removed,
                remaining,
                defaultValue: '{{removed}} records were removed; {{remaining}} are still stored and no disk space was returned yet — run it again to finish.',
              })
            : t('trafficLog.purge.partial', {
                removed,
                remaining,
                defaultValue: '{{removed}} records were removed; {{remaining}} are still stored — run it again to finish.',
              }),
        )
      } else if (result.reclaimed && freed != null) {
        toast.success(t('trafficLog.purge.doneFreed', { removed, freed, defaultValue: '{{removed}} records were removed and {{freed}} of disk space was returned.' }))
      } else if (result.reclaimed) {
        toast.success(t('trafficLog.purge.doneReclaimed', { removed, defaultValue: '{{removed}} records were removed and the freed disk space was returned to the filesystem.' }))
      } else if (variables.reclaim) {
        toast.success(t('trafficLog.purge.doneNotReclaimed', { removed, defaultValue: '{{removed}} records were removed, but no disk space was returned to the filesystem.' }))
      } else {
        toast.success(t('trafficLog.purge.done', { removed, defaultValue: '{{removed}} records were removed.' }))
      }
      setOpen(false)
    },
    onError: error => toast.error(errorText(error, t('trafficLog.purge.failed', { defaultValue: 'Could not clear the log.' }))),
    onSettled: () => {
      void queryClient.invalidateQueries({ queryKey: STATUS_KEY })
      void queryClient.invalidateQueries({ queryKey: HISTORY_KEY })
      void queryClient.invalidateQueries({ queryKey: SUMMARY_KEY })
    },
  })

  const selectedHours = PURGE_CHOICES.find(choice => choice.id === scope)?.hours ?? null

  return (
    <>
      <Button
        type="button"
        variant="outline"
        size="sm"
        className="border-destructive/40 text-destructive hover:bg-destructive/10 hover:text-destructive h-8 text-xs"
        disabled={purge.isPending}
        onClick={() => setOpen(true)}
      >
        {purge.isPending ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Trash2 className="h-3.5 w-3.5" />}
        {t('trafficLog.purge.button', { defaultValue: 'Clear the log now' })}
      </Button>

      <AlertDialog
        open={open}
        onOpenChange={next => {
          if (!purge.isPending) setOpen(next)
        }}
      >
        <AlertDialogContent dir={dir}>
          <AlertDialogHeader>
            <AlertDialogTitle>{t('trafficLog.purge.title', { defaultValue: 'Clear the traffic log?' })}</AlertDialogTitle>
            <AlertDialogDescription>{t('trafficLog.purge.description', { defaultValue: 'Removed records cannot be brought back. Choose what to remove.' })}</AlertDialogDescription>
          </AlertDialogHeader>
          <div className="space-y-2">
            <div className="text-muted-foreground text-[11px] font-semibold uppercase tracking-wide">{t('trafficLog.purge.scope', { defaultValue: 'What to remove' })}</div>
            <div className="flex flex-wrap gap-2">
              {PURGE_CHOICES.map(choice => (
                <Button
                  key={choice.id}
                  type="button"
                  variant={scope === choice.id ? 'default' : 'outline'}
                  size="sm"
                  className="h-8 text-xs"
                  aria-pressed={scope === choice.id}
                  disabled={purge.isPending}
                  onClick={() => setScope(choice.id)}
                >
                  {t(choice.label, { defaultValue: choice.fallback })}
                </Button>
              ))}
            </div>
            <div className="flex items-start gap-2 pt-1">
              <Checkbox id="traffic-log-reclaim" className="mt-0.5" checked={reclaim} disabled={purge.isPending} onCheckedChange={value => setReclaim(value === true)} />
              <div className="space-y-0.5">
                <Label htmlFor="traffic-log-reclaim" className="text-xs font-medium">
                  {t('trafficLog.purge.reclaim', { defaultValue: 'Also reclaim disk space (may take a moment)' })}
                </Label>
                <p className="text-muted-foreground text-[11px]">
                  {t('trafficLog.purge.reclaimHint', { defaultValue: 'Removing records on its own does not shrink the database file; reclaiming returns the freed space to the filesystem.' })}
                </p>
              </div>
            </div>
          </div>
          <AlertDialogFooter>
            <AlertDialogCancel disabled={purge.isPending}>{t('trafficLog.purge.cancel', { defaultValue: 'Cancel' })}</AlertDialogCancel>
            <AlertDialogAction
              className="bg-destructive text-destructive-foreground hover:bg-destructive/90"
              disabled={purge.isPending}
              onClick={event => {
                event.preventDefault()
                purge.mutate({ hours: selectedHours, reclaim })
              }}
            >
              {purge.isPending && <Loader2 className="h-3.5 w-3.5 animate-spin" />}
              {t('trafficLog.purge.confirm', { defaultValue: 'Clear now' })}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </>
  )
}

function StatusStrip({ status, isLoading, isSudo, onToggle, isToggling }: { status?: TrafficStatus; isLoading: boolean; isSudo: boolean; onToggle: (enabled: boolean) => void; isToggling: boolean }) {
  const { t, i18n } = useTranslation()
  const language = i18n.language

  if (isLoading && !status) {
    return <Skeleton className="h-10 w-full" />
  }
  if (!status) {
    return null
  }

  return (
    <div className="space-y-2">
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-muted-foreground text-[11px] font-semibold uppercase tracking-wide">{t('trafficLog.status.title', { defaultValue: 'Collection' })}</span>
        <TooltipProvider delayDuration={150}>
          {status.nodes.map(node => (
            <Tooltip key={node.node_id}>
              <TooltipTrigger asChild>
                <span className="bg-muted/50 flex items-center gap-1.5 rounded-md border px-2 py-1 text-[11px]">
                  <span className={cn('h-1.5 w-1.5 shrink-0 rounded-full', STATE_DOT[node.state] ?? 'bg-muted-foreground/40')} />
                  <bdi className="max-w-[140px] truncate font-medium">{node.node ?? `#${node.node_id}`}</bdi>
                  <span className="text-muted-foreground tabular-nums">
                    {node.lines.toLocaleString()} {t('trafficLog.status.lines', { defaultValue: 'lines' })} · {node.events.toLocaleString()} {t('trafficLog.status.events', { defaultValue: 'events' })}
                  </span>
                  {node.dropped > 0 && (
                    <span className="text-amber-600 tabular-nums dark:text-amber-400">
                      {node.dropped.toLocaleString()} {t('trafficLog.status.dropped', { defaultValue: 'dropped' })}
                    </span>
                  )}
                </span>
              </TooltipTrigger>
              <TooltipContent>
                <div className="space-y-0.5">
                  <div>{stateLabel(node.state, t)}</div>
                  {node.since && <div>{t('trafficLog.status.since', { time: logDayTime(node.since, language), defaultValue: 'since {{time}}' })}</div>}
                  <div>{t('trafficLog.status.records', { value: node.records.toLocaleString(), defaultValue: '{{value}} records written' })}</div>
                  {node.detail && <div className="max-w-[240px] break-words">{node.detail}</div>}
                </div>
              </TooltipContent>
            </Tooltip>
          ))}
        </TooltipProvider>
        {status.nodes.length === 0 && <span className="text-muted-foreground text-[11px]">{t('trafficLog.status.noNodes', { defaultValue: 'No node is being collected from.' })}</span>}
        {isSudo ? (
          <span className="ms-auto flex flex-wrap items-center gap-2">
            <RetentionSelect retentionHours={status.retention_hours} />
            <span className="text-muted-foreground/70 text-[11px]">{t('trafficLog.status.ceilingValue', { max: status.max_records.toLocaleString(), defaultValue: 'ceiling {{max}} records' })}</span>
          </span>
        ) : (
          <span className="text-muted-foreground/70 ms-auto text-[11px]">
            {t('trafficLog.status.retention', { hours: status.retention_hours, max: status.max_records.toLocaleString(), defaultValue: 'kept {{hours}} h · ceiling {{max}} records' })}
          </span>
        )}
        {isSudo && (
          <span className="flex items-center gap-2">
            <Label htmlFor="traffic-log-collection" className="text-[11px]">
              {t('trafficLog.status.collect', { defaultValue: 'Record traffic' })}
            </Label>
            <Switch id="traffic-log-collection" checked={status.enabled} disabled={isToggling} onCheckedChange={onToggle} />
          </span>
        )}
        {isSudo && <PurgeButton />}
      </div>

      {status.ceiling_active && (
        <Alert className="border-amber-500/40 bg-amber-500/10 py-2">
          <AlertDescription className="text-xs text-amber-700 dark:text-amber-400">
            {t('trafficLog.status.ceiling', {
              removed: status.purged_over_ceiling.toLocaleString(),
              time: logShort(status.last_purge_at, language),
              defaultValue: 'The record ceiling is in force: {{removed}} of the oldest records were removed (last purge {{time}}).',
            })}
          </AlertDescription>
        </Alert>
      )}
    </div>
  )
}

function LivePanel({ username, nodeId, refusedOnly, onControl }: { username: string; nodeId: string; refusedOnly: boolean; onControl: (control: ControlMessage) => void }) {
  const { t, i18n } = useTranslation()
  const language = i18n.language
  const dir = useDirDetection()
  const isRTL = dir === 'rtl'
  const [rows, setRows] = useState<LiveRow[]>([])
  const [held, setHeld] = useState<LiveRow[]>([])
  const [isPaused, setIsPaused] = useState(false)
  const [dropped, setDropped] = useState(0)
  const [unknownUser, setUnknownUser] = useState(false)
  const [isConnected, setIsConnected] = useState(false)
  const [hasFailed, setHasFailed] = useState(false)
  const pausedRef = useRef(false)
  const autoScrollRef = useRef(true)
  const scrollRef = useRef<HTMLDivElement>(null)
  const keyRef = useRef(0)

  useEffect(() => {
    pausedRef.current = isPaused
  }, [isPaused])

  useEffect(() => {
    let active = true
    let flushTimer: ReturnType<typeof setTimeout> | null = null
    const pending: LiveRow[] = []

    setRows([])
    setHeld([])
    setIsPaused(false)
    pausedRef.current = false
    autoScrollRef.current = true
    setDropped(0)
    setUnknownUser(false)
    setIsConnected(false)
    setHasFailed(false)

    const flush = () => {
      flushTimer = null
      if (!active || pending.length === 0) {
        pending.length = 0
        return
      }
      const batch = pending.splice(0, pending.length).reverse()
      if (pausedRef.current) {
        setHeld(previous => [...batch, ...previous].slice(0, MAX_LIVE_ROWS))
      } else {
        setRows(previous => [...batch, ...previous].slice(0, MAX_LIVE_ROWS))
      }
    }

    const query = new URLSearchParams()
    if (username) query.set('username', username)
    if (nodeId !== ALL_NODES) query.set('node_id', nodeId)
    const search = query.toString()
    const token = getAuthToken()
    const source = new EventSource(`${apiBaseUrl()}${BASE}/live${search ? `?${search}` : ''}`, {
      fetch: (input, init) =>
        fetch(input, {
          ...init,
          headers: {
            ...init?.headers,
            Authorization: `Bearer ${token}`,
          },
        }),
    })

    source.onopen = () => {
      if (!active) return
      setIsConnected(true)
      setHasFailed(false)
    }

    source.onmessage = message => {
      if (!active) return
      let payload: unknown
      try {
        payload = JSON.parse(message.data)
      } catch {
        return
      }
      if (payload == null || typeof payload !== 'object') return
      if ('control' in payload) {
        const control = payload as ControlMessage
        if (control.control === 'dropped') setDropped(previous => previous + control.count)
        if (control.control === 'unknown_user') {
          setUnknownUser(true)
          setIsConnected(false)
          source.close()
        }
        if (control.control === 'revoked') {
          setRows([])
          setHeld([])
          pending.length = 0
          if (flushTimer != null) {
            clearTimeout(flushTimer)
            flushTimer = null
          }
          setIsConnected(false)
          source.close()
        }
        onControl(control)
        return
      }
      keyRef.current += 1
      pending.push({ ...(payload as LiveEvent), key: keyRef.current })
      if (flushTimer == null) {
        flushTimer = setTimeout(flush, LIVE_FLUSH_MS)
      }
    }

    source.onerror = () => {
      if (!active) return
      setIsConnected(false)
      setHasFailed(true)
      void fetcher<TrafficStatus>(`${BASE}/status`)
        .then(() => undefined)
        .catch(error => {
          if (!active || refusedStatus(error) !== 403) return
          setRows([])
          setHeld([])
          pending.length = 0
          source.close()
          onControl({ control: 'revoked' })
        })
    }

    return () => {
      active = false
      if (flushTimer != null) clearTimeout(flushTimer)
      pending.length = 0
      source.close()
    }
  }, [username, nodeId, onControl])

  const visible = useMemo(() => (refusedOnly ? rows.filter(row => row.refused) : rows), [rows, refusedOnly])

  useEffect(() => {
    if (autoScrollRef.current && scrollRef.current) {
      scrollRef.current.scrollTop = 0
    }
  }, [visible])

  const handleScroll = () => {
    if (!scrollRef.current) return
    autoScrollRef.current = scrollRef.current.scrollTop < 12
  }

  const togglePause = () => {
    if (isPaused && held.length > 0) {
      setRows(previous => [...held, ...previous].slice(0, MAX_LIVE_ROWS))
      setHeld([])
    }
    setIsPaused(previous => !previous)
  }

  return (
    <div className="space-y-2">
      <div className="flex flex-wrap items-center gap-2">
        <Button type="button" variant="outline" size="sm" className="h-8 text-xs" onClick={togglePause}>
          {isPaused ? <Play className="h-3.5 w-3.5" /> : <Pause className="h-3.5 w-3.5" />}
          {isPaused ? t('trafficLog.resume', { defaultValue: 'Resume' }) : t('trafficLog.pause', { defaultValue: 'Pause' })}
          {isPaused && held.length > 0 && <span className="ms-1 tabular-nums">({held.length})</span>}
        </Button>
        <span className="text-muted-foreground text-[11px] tabular-nums">{t('trafficLog.rowCount', { value: visible.length, defaultValue: '{{value}} rows' })}</span>
        {dropped > 0 && (
          <span className="text-[11px] tabular-nums text-amber-600 dark:text-amber-400">{t('trafficLog.droppedCount', { value: dropped, defaultValue: '{{value}} dropped' })}</span>
        )}
        {isConnected && !isPaused && (
          <span className="flex items-center gap-1.5 text-[11px] text-emerald-600 dark:text-emerald-400">
            <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-emerald-500" />
            {t('trafficLog.streaming', { defaultValue: 'Streaming' })}
          </span>
        )}
      </div>

      {hasFailed && !unknownUser && (
        <Alert className="border-destructive/40 bg-destructive/10 py-2">
          <AlertDescription className="text-destructive text-xs">{t('trafficLog.streamFailed', { defaultValue: 'The live feed lost its connection and is retrying.' })}</AlertDescription>
        </Alert>
      )}

      <div ref={scrollRef} onScroll={handleScroll} className="max-h-[520px] min-h-[280px] overflow-auto rounded-lg border">
        <Table dir={isRTL ? 'rtl' : 'ltr'} className="text-xs" containerClassName="overflow-x-visible">
          <TableHeader className="bg-background sticky top-0 z-10">
            <TableRow>
              <TableHead className={cn('w-24 text-[11px]', isRTL && 'text-right')}>{t('trafficLog.columns.time', { defaultValue: 'Time' })}</TableHead>
              <TableHead className={cn('w-40 text-[11px]', isRTL && 'text-right')}>{t('trafficLog.columns.user', { defaultValue: 'Subscription' })}</TableHead>
              <TableHead className={cn('text-[11px]', isRTL && 'text-right')}>{t('trafficLog.columns.destination', { defaultValue: 'Destination' })}</TableHead>
              <TableHead className={cn('w-16 text-[11px]', isRTL && 'text-right')}>{t('trafficLog.columns.protocol', { defaultValue: 'Protocol' })}</TableHead>
              <TableHead className={cn('hidden w-32 text-[11px] md:table-cell', isRTL && 'text-right')}>{t('trafficLog.columns.node', { defaultValue: 'Node' })}</TableHead>
              <TableHead className={cn('hidden w-40 text-[11px] lg:table-cell', isRTL && 'text-right')}>{t('trafficLog.columns.endpoint', { defaultValue: 'Endpoint' })}</TableHead>
              <TableHead className={cn('w-40 text-[11px]', isRTL && 'text-right')}>{t('trafficLog.columns.outcome', { defaultValue: 'Outcome' })}</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {visible.map(row => (
              <TableRow key={row.key}>
                <TableCell className="text-muted-foreground py-1.5 whitespace-nowrap tabular-nums">{logTime(row.at, language)}</TableCell>
                <TableCell className="py-1.5">
                  <bdi className={cn('truncate font-medium', row.username == null && 'text-muted-foreground font-mono')}>{userLabel(row.username, row.user_id)}</bdi>
                </TableCell>
                <TableCell className="py-1.5">
                  <Destination host={row.host} port={row.port} />
                </TableCell>
                <TableCell className="py-1.5">
                  <ProtocolChip protocol={row.protocol} />
                </TableCell>
                <TableCell className="text-muted-foreground hidden py-1.5 md:table-cell">
                  <bdi className="truncate">{row.node ?? `#${row.node_id}`}</bdi>
                </TableCell>
                <TableCell className="text-muted-foreground hidden py-1.5 lg:table-cell">
                  <bdi className="truncate">{row.inbound}</bdi>
                </TableCell>
                <TableCell className="py-1.5">
                  <OutcomeChip refused={row.refused} route={row.route} />
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>

        {visible.length === 0 && (
          <div className="text-muted-foreground flex min-h-[220px] flex-col items-center justify-center gap-2 px-4 text-center text-xs">
            {unknownUser ? (
              <span>{t('trafficLog.unknownUser', { defaultValue: 'No subscription has that exact name. Pick one from the suggestions.' })}</span>
            ) : (
              <>
                <Loader2 className={cn('h-4 w-4', isConnected && 'animate-spin')} />
                <span>{t('trafficLog.waiting', { defaultValue: 'Waiting for the first connection to be reported.' })}</span>
              </>
            )}
          </div>
        )}
      </div>
    </div>
  )
}

function HistoryPanel({ username, nodeId, refusedOnly, retentionHours }: { username: string; nodeId: string; refusedOnly: boolean; retentionHours: number }) {
  const { t, i18n } = useTranslation()
  const language = i18n.language
  const dir = useDirDetection()
  const isRTL = dir === 'rtl'
  const [preset, setPreset] = useState<PresetId>('1h')
  const [customStart, setCustomStart] = useState('')
  const [customEnd, setCustomEnd] = useState('')
  const { search: destinationInput, debouncedSearch: debouncedDestination, setSearch: setDestinationInput } = useDebouncedSearch('', SUGGEST_DEBOUNCE_MS)
  const { search: inboundInput, debouncedSearch: debouncedInbound, setSearch: setInboundInput } = useDebouncedSearch('', SUGGEST_DEBOUNCE_MS)
  const [anchor, setAnchor] = useState(() => Date.now())
  const retentionMs = retentionHours * HOUR_MS

  const presets = useMemo(() => PRESETS.filter(entry => fitsRetention(entry.id, retentionMs)), [retentionMs])
  const longestPreset = useMemo<PresetId>(() => {
    const fitting = PRESETS.filter(entry => entry.id !== 'custom' && fitsRetention(entry.id, retentionMs))
    return fitting.length > 0 ? fitting[fitting.length - 1].id : 'custom'
  }, [retentionMs])

  useEffect(() => {
    if (fitsRetention(preset, retentionMs)) return
    setPreset(longestPreset)
    setAnchor(Date.now())
  }, [preset, retentionMs, longestPreset])

  const range = useMemo<RangeState>(() => {
    if (preset === 'custom') {
      if (!customStart || !customEnd) return { kind: 'incomplete' }
      const start = new Date(customStart).getTime()
      const end = new Date(customEnd).getTime()
      if (!Number.isFinite(start) || !Number.isFinite(end)) return { kind: 'incomplete' }
      if (end <= start) return { kind: 'invalid' }
      if (start < Date.now() - retentionMs) return { kind: 'tooOld' }
      return { kind: 'ready', start: new Date(start).toISOString(), end: new Date(end).toISOString() }
    }
    return { kind: 'ready', start: new Date(anchor - PRESET_MS[preset]).toISOString(), end: new Date(anchor).toISOString() }
  }, [preset, customStart, customEnd, anchor, retentionMs])

  const ready = range.kind === 'ready'
  const startIso = range.kind === 'ready' ? range.start : ''
  const endIso = range.kind === 'ready' ? range.end : ''
  const isCustom = preset === 'custom'
  const trimmedDestination = (debouncedDestination ?? '').trim()
  const trimmedInbound = (debouncedInbound ?? '').trim()

  const baseParams = useCallback(() => {
    const params = new URLSearchParams({ start: isCustom ? startIso : clampToRetention(startIso, retentionMs), end: endIso })
    if (username) params.set('username', username)
    if (nodeId !== ALL_NODES) params.set('node_id', nodeId)
    if (trimmedInbound) params.set('inbound', trimmedInbound)
    if (trimmedDestination) params.set('destination', trimmedDestination)
    if (refusedOnly) params.set('refused', 'true')
    return params
  }, [isCustom, startIso, endIso, username, nodeId, trimmedInbound, trimmedDestination, refusedOnly, retentionMs])

  const filterKey = [startIso, endIso, username, nodeId, trimmedInbound, trimmedDestination, refusedOnly, retentionHours]

  const summaryQuery = useQuery({
    queryKey: [...SUMMARY_KEY, ...filterKey],
    queryFn: () => fetcher<TrafficSummary>(`${BASE}/summary?${baseParams().toString()}`),
    enabled: ready,
    retry: false,
    refetchOnWindowFocus: false,
  })

  const historyQuery = useInfiniteQuery({
    queryKey: [...HISTORY_KEY, ...filterKey],
    queryFn: ({ pageParam }) => {
      const params = baseParams()
      params.set('limit', String(HISTORY_LIMIT))
      if (pageParam) params.set('cursor', pageParam)
      return fetcher<HistoryPage>(`${BASE}/history?${params.toString()}`)
    },
    initialPageParam: null as string | null,
    getNextPageParam: (lastPage: HistoryPage) => lastPage.next_cursor,
    enabled: ready,
    retry: false,
    refetchOnWindowFocus: false,
  })

  const items = useMemo(() => historyQuery.data?.pages.flatMap(page => page.items) ?? [], [historyQuery.data])
  const summary = summaryQuery.data
  const loadError = historyQuery.error ?? summaryQuery.error

  const choosePreset = (next: PresetId) => {
    setPreset(next)
    setAnchor(Date.now())
  }

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-2">
        {presets.map(entry => (
          <Button
            key={entry.id}
            type="button"
            variant={preset === entry.id ? 'default' : 'outline'}
            size="sm"
            className="h-8 text-xs"
            aria-pressed={preset === entry.id}
            onClick={() => choosePreset(entry.id)}
          >
            {t(entry.label, { defaultValue: entry.fallback })}
          </Button>
        ))}
        <Button type="button" variant="ghost" size="sm" className="h-8 text-xs" onClick={() => setAnchor(Date.now())} disabled={historyQuery.isFetching || summaryQuery.isFetching}>
          <RefreshCw className={cn('h-3.5 w-3.5', (historyQuery.isFetching || summaryQuery.isFetching) && 'animate-spin')} />
          {t('trafficLog.refresh', { defaultValue: 'Refresh' })}
        </Button>
        <span className="text-muted-foreground/70 text-[11px]">{t('trafficLog.rangeWindow', { window: windowLabel(retentionHours, t), defaultValue: 'Ranges within the last {{window}}' })}</span>
      </div>

      {isCustom && (
        <div className="flex flex-wrap items-end gap-2">
          <div className="w-full space-y-1 sm:w-auto">
            <Label htmlFor="traffic-log-start" className="text-[11px]">
              {t('trafficLog.range.start', { defaultValue: 'From' })}
            </Label>
            <div className="flex w-full sm:w-52">
              <Input id="traffic-log-start" type="datetime-local" value={customStart} onChange={event => setCustomStart(event.target.value)} className="h-8 text-xs" />
            </div>
          </div>
          <div className="w-full space-y-1 sm:w-auto">
            <Label htmlFor="traffic-log-end" className="text-[11px]">
              {t('trafficLog.range.end', { defaultValue: 'To' })}
            </Label>
            <div className="flex w-full sm:w-52">
              <Input id="traffic-log-end" type="datetime-local" value={customEnd} onChange={event => setCustomEnd(event.target.value)} className="h-8 text-xs" />
            </div>
          </div>
        </div>
      )}

      <div className="flex flex-wrap items-end gap-2">
        <div className="w-full space-y-1 sm:w-auto">
          <Label htmlFor="traffic-log-destination" className="text-[11px]">
            {t('trafficLog.filters.destination', { defaultValue: 'Destination contains' })}
          </Label>
          <div className="flex w-full sm:w-64">
            <Input
              id="traffic-log-destination"
              value={destinationInput}
              onChange={event => setDestinationInput(event.target.value)}
              placeholder={t('trafficLog.filters.destinationPlaceholder', { defaultValue: 'google' })}
              className="h-8 text-xs"
            />
          </div>
        </div>
        <div className="w-full space-y-1 sm:w-auto">
          <Label htmlFor="traffic-log-inbound" className="text-[11px]">
            {t('trafficLog.filters.inbound', { defaultValue: 'Endpoint (exact)' })}
          </Label>
          <div className="flex w-full sm:w-64">
            <Input
              id="traffic-log-inbound"
              value={inboundInput}
              onChange={event => setInboundInput(event.target.value)}
              placeholder={t('trafficLog.filters.inboundPlaceholder', { defaultValue: 'Shadowsocks TCP' })}
              className="h-8 text-xs"
            />
          </div>
        </div>
      </div>

      {range.kind === 'tooOld' && (
        <Alert className="border-destructive/40 bg-destructive/10 py-2">
          <AlertDescription className="text-destructive text-xs">
            {t('trafficLog.tooOld', { window: windowLabel(retentionHours, t), defaultValue: 'History is kept for {{window}}; choose a start within that window.' })}
          </AlertDescription>
        </Alert>
      )}
      {range.kind === 'invalid' && (
        <Alert className="border-destructive/40 bg-destructive/10 py-2">
          <AlertDescription className="text-destructive text-xs">{t('trafficLog.invalidRange', { defaultValue: 'The end of the range must come after its start.' })}</AlertDescription>
        </Alert>
      )}
      {range.kind === 'incomplete' && <p className="text-muted-foreground text-xs">{t('trafficLog.pickRange', { defaultValue: 'Choose a start and an end for the custom range.' })}</p>}
      {loadError != null && (
        <Alert className="border-destructive/40 bg-destructive/10 py-2">
          <AlertDescription className="text-destructive text-xs">{errorText(loadError, t('trafficLog.loadFailed', { defaultValue: 'Could not load the traffic log.' }))}</AlertDescription>
        </Alert>
      )}

      {ready && (
        <>
          <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
            {summaryQuery.isLoading && !summary ? (
              <>
                <Skeleton className="h-16 w-full" />
                <Skeleton className="h-16 w-full" />
                <Skeleton className="h-16 w-full" />
                <Skeleton className="h-16 w-full" />
              </>
            ) : (
              <>
                <StatCard label={t('trafficLog.summary.connections', { defaultValue: 'Connections' })} value={summary?.connections ?? 0} />
                <StatCard label={t('trafficLog.summary.destinations', { defaultValue: 'Destinations' })} value={summary?.destinations ?? 0} />
                <StatCard label={t('trafficLog.summary.users', { defaultValue: 'Subscriptions' })} value={summary?.users ?? 0} />
                <StatCard label={t('trafficLog.summary.refused', { defaultValue: 'Refused' })} value={summary?.refused ?? 0} />
              </>
            )}
          </div>

          <div className="grid gap-2 md:grid-cols-2">
            <div className="rounded-lg border p-3">
              <div className="text-muted-foreground mb-2 text-[11px] font-semibold uppercase tracking-wide">{t('trafficLog.summary.topUsers', { defaultValue: 'Busiest subscriptions' })}</div>
              {(summary?.top_users ?? []).length === 0 ? (
                <p className="text-muted-foreground/70 text-xs">{t('trafficLog.summary.empty', { defaultValue: 'Nothing in this range' })}</p>
              ) : (
                <ul className="space-y-1">
                  {(summary?.top_users ?? []).map(entry => (
                    <li key={`${entry.user_id}-${entry.username}`} className="flex items-center justify-between gap-2 text-xs">
                      <bdi className="truncate">{userLabel(entry.username, entry.user_id)}</bdi>
                      <span className="text-muted-foreground tabular-nums">{entry.hits.toLocaleString()}</span>
                    </li>
                  ))}
                </ul>
              )}
            </div>
            <div className="rounded-lg border p-3">
              <div className="text-muted-foreground mb-2 text-[11px] font-semibold uppercase tracking-wide">
                {t('trafficLog.summary.topDestinations', { defaultValue: 'Most requested destinations' })}
              </div>
              {(summary?.top_destinations ?? []).length === 0 ? (
                <p className="text-muted-foreground/70 text-xs">{t('trafficLog.summary.empty', { defaultValue: 'Nothing in this range' })}</p>
              ) : (
                <ul className="space-y-1">
                  {(summary?.top_destinations ?? []).map(entry => (
                    <li key={entry.host} className="flex items-center justify-between gap-2 text-xs">
                      <bdi dir="ltr" className="truncate font-mono text-[11px]">
                        {entry.host}
                      </bdi>
                      <span className="flex shrink-0 items-center gap-2">
                        {entry.refused > 0 && <span className="text-destructive tabular-nums">{t('trafficLog.summary.refusedShort', { value: entry.refused, defaultValue: '{{value}} refused' })}</span>}
                        <span className="text-muted-foreground tabular-nums">{entry.hits.toLocaleString()}</span>
                      </span>
                    </li>
                  ))}
                </ul>
              )}
            </div>
          </div>

          <div className="rounded-lg border">
            <Table dir={isRTL ? 'rtl' : 'ltr'} className="text-xs">
              <TableHeader>
                <TableRow>
                  <TableHead className={cn('w-28 text-[11px]', isRTL && 'text-right')}>{t('trafficLog.columns.lastSeen', { defaultValue: 'Last seen' })}</TableHead>
                  <TableHead className={cn('hidden w-28 text-[11px] lg:table-cell', isRTL && 'text-right')}>{t('trafficLog.columns.firstSeen', { defaultValue: 'First seen' })}</TableHead>
                  <TableHead className={cn('w-40 text-[11px]', isRTL && 'text-right')}>{t('trafficLog.columns.user', { defaultValue: 'Subscription' })}</TableHead>
                  <TableHead className={cn('text-[11px]', isRTL && 'text-right')}>{t('trafficLog.columns.destination', { defaultValue: 'Destination' })}</TableHead>
                  <TableHead className={cn('w-16 text-[11px]', isRTL && 'text-right')}>{t('trafficLog.columns.protocol', { defaultValue: 'Protocol' })}</TableHead>
                  <TableHead className={cn('hidden w-32 text-[11px] md:table-cell', isRTL && 'text-right')}>{t('trafficLog.columns.node', { defaultValue: 'Node' })}</TableHead>
                  <TableHead className={cn('hidden w-40 text-[11px] lg:table-cell', isRTL && 'text-right')}>{t('trafficLog.columns.endpoint', { defaultValue: 'Endpoint' })}</TableHead>
                  <TableHead className={cn('w-20 text-[11px]', isRTL && 'text-right')}>{t('trafficLog.columns.hits', { defaultValue: 'Connections' })}</TableHead>
                  <TableHead className={cn('w-40 text-[11px]', isRTL && 'text-right')}>{t('trafficLog.columns.outcome', { defaultValue: 'Outcome' })}</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {items.map(item => (
                  <TableRow key={item.id}>
                    <TableCell className="text-muted-foreground py-1.5 whitespace-nowrap tabular-nums">{logDayTime(item.last_seen, language)}</TableCell>
                    <TableCell className="text-muted-foreground hidden py-1.5 whitespace-nowrap tabular-nums lg:table-cell">{logDayTime(item.first_seen, language)}</TableCell>
                    <TableCell className="py-1.5">
                      <div className="flex flex-col gap-0.5">
                        <bdi className={cn('truncate font-medium', item.username == null && 'text-muted-foreground font-mono')}>{userLabel(item.username, item.user_id)}</bdi>
                        {item.user_deleted && <span className="text-muted-foreground/70 text-[10px]">{t('trafficLog.deletedUser', { defaultValue: 'deleted' })}</span>}
                      </div>
                    </TableCell>
                    <TableCell className="py-1.5">
                      <Destination host={item.host} port={item.port} />
                    </TableCell>
                    <TableCell className="py-1.5">
                      <ProtocolChip protocol={item.protocol} />
                    </TableCell>
                    <TableCell className="text-muted-foreground hidden py-1.5 md:table-cell">
                      <bdi className="truncate">{item.node ?? `#${item.node_id}`}</bdi>
                    </TableCell>
                    <TableCell className="text-muted-foreground hidden py-1.5 lg:table-cell">
                      <bdi className="truncate">{item.inbound}</bdi>
                    </TableCell>
                    <TableCell className="py-1.5 tabular-nums">{item.hits.toLocaleString()}</TableCell>
                    <TableCell className="py-1.5">
                      <OutcomeChip refused={item.refused} route={item.route} />
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>

            {historyQuery.isLoading && items.length === 0 && (
              <div className="space-y-2 p-3">
                <Skeleton className="h-8 w-full" />
                <Skeleton className="h-8 w-full" />
                <Skeleton className="h-8 w-full" />
              </div>
            )}
            {!historyQuery.isLoading && items.length === 0 && loadError == null && (
              <p className="text-muted-foreground p-6 text-center text-xs">{t('trafficLog.noRecords', { defaultValue: 'Nothing was recorded in this range.' })}</p>
            )}
            {historyQuery.hasNextPage && (
              <div className="flex justify-center border-t p-2">
                <Button type="button" variant="outline" size="sm" className="h-8 text-xs" disabled={historyQuery.isFetchingNextPage} onClick={() => void historyQuery.fetchNextPage()}>
                  {historyQuery.isFetchingNextPage && <Loader2 className="h-3.5 w-3.5 animate-spin" />}
                  {t('trafficLog.loadMore', { defaultValue: 'Load more' })}
                </Button>
              </div>
            )}
          </div>
        </>
      )}
    </div>
  )
}

export default function TrafficLogView() {
  const { t } = useTranslation()
  const { admin } = useAdmin()
  const currentAdmin = admin as unknown as AdminDetails | null
  const queryClient = useQueryClient()
  const isSudo = isOwner(currentAdmin)
  const canListNodes = hasPermission(currentAdmin, 'nodes', 'read_simple')
  const canSearchUsers = hasPermission(currentAdmin, 'users', 'read')

  const [mode, setMode] = useState<Mode>('live')
  const { search: usernameInput, debouncedSearch, setSearch: setUsernameInput } = useDebouncedSearch('', SUGGEST_DEBOUNCE_MS)
  const username = (debouncedSearch ?? '').trim()
  const [nodeId, setNodeId] = useState<string>(ALL_NODES)
  const [refusedOnly, setRefusedOnly] = useState(false)
  const [suggestOpen, setSuggestOpen] = useState(false)
  const [controlPaused, setControlPaused] = useState(false)
  const [controlUnavailable, setControlUnavailable] = useState<string | null>(null)
  const [controlRevoked, setControlRevoked] = useState(false)

  const { data: nodesResponse } = useGetNodesSimple<NodesSimpleResponse>({ all: true }, { query: { enabled: canListNodes } })
  const nodes = nodesResponse?.nodes ?? []

  const statusQuery = useQuery({
    queryKey: STATUS_KEY,
    queryFn: () => fetcher<TrafficStatus>(`${BASE}/status`),
    refetchInterval: STATUS_INTERVAL_MS,
    retry: false,
  })
  const status = statusQuery.data
  const retentionHours = status?.retention_hours ?? DEFAULT_RETENTION_HOURS

  useEffect(() => {
    if (!status) return
    if (status.enabled) setControlPaused(false)
    if (status.available) setControlUnavailable(null)
  }, [status])

  const onControl = useCallback(
    (control: ControlMessage) => {
      if (control.control === 'paused') setControlPaused(true)
      if (control.control === 'unavailable') setControlUnavailable(control.reason ?? '')
      if (control.control === 'revoked') {
        setControlRevoked(true)
        queryClient.invalidateQueries({ queryKey: ['admin'] })
      }
    },
    [queryClient],
  )

  const suggestionQuery = useQuery({
    queryKey: ['traffic-log', 'usernames', username],
    queryFn: () => fetcher<UsersResponse>(`${USERS_URL}?${new URLSearchParams({ search: username, limit: String(SUGGEST_LIMIT) }).toString()}`),
    enabled: canSearchUsers && suggestOpen && username.length > 0,
    retry: false,
    staleTime: 30000,
  })
  const options = useMemo(() => (suggestionQuery.data?.users ?? []).map(user => user.username).slice(0, SUGGEST_LIMIT), [suggestionQuery.data])
  const suggestNotice = !canSearchUsers
    ? t('trafficLog.filters.suggestUnavailable', { defaultValue: 'Suggestions need permission to read users; type the exact username.' })
    : suggestionQuery.error != null
      ? errorText(suggestionQuery.error, t('trafficLog.filters.suggestFailed', { defaultValue: 'Suggestions could not be loaded.' }))
      : null

  const toggleCollection = useMutation({
    mutationFn: (enabled: boolean) => fetcher<TrafficStatus>(`${BASE}/settings`, { method: 'PUT', body: { enabled } }),
    onSuccess: (_result, enabled) => {
      toast.success(enabled ? t('trafficLog.status.resumed', { defaultValue: 'Collection resumed' }) : t('trafficLog.status.paused', { defaultValue: 'Collection paused' }))
      void queryClient.invalidateQueries({ queryKey: STATUS_KEY })
    },
    onError: error => toast.error(errorText(error, t('trafficLog.status.toggleFailed', { defaultValue: 'Could not change collection' }))),
  })

  const isPausedCollection = controlPaused || status?.enabled === false
  const unavailableReason = controlUnavailable ?? (status && !status.available ? (status.reason ?? '') : null)
  const hasFilters = usernameInput.length > 0 || nodeId !== ALL_NODES || refusedOnly

  const clearFilters = () => {
    setUsernameInput('')
    setNodeId(ALL_NODES)
    setRefusedOnly(false)
    setSuggestOpen(false)
  }

  if (!isSudo) return null

  return (
    <Card>
      <CardHeader className="flex flex-col gap-3 border-b pb-4 sm:flex-row sm:items-center sm:justify-between">
        <div className="space-y-1">
          <CardTitle className="flex items-center gap-2 text-base">
            <Radio className="h-4 w-4" />
            {t('trafficLog.title', { defaultValue: 'Live traffic log' })}
          </CardTitle>
          <CardDescription className="text-xs">
            {t('trafficLog.description', { hours: retentionHours, defaultValue: 'Destinations reported by every monitored node, live and for the last {{hours}} hours.' })}
          </CardDescription>
        </div>
        <div className="bg-muted inline-flex shrink-0 items-center gap-1 rounded-lg p-1">
          <Button type="button" variant={mode === 'live' ? 'default' : 'ghost'} size="sm" className="h-8 text-xs" aria-pressed={mode === 'live'} onClick={() => setMode('live')}>
            <Activity className="h-3.5 w-3.5" />
            {t('trafficLog.live', { defaultValue: 'Live' })}
          </Button>
          <Button type="button" variant={mode === 'history' ? 'default' : 'ghost'} size="sm" className="h-8 text-xs" aria-pressed={mode === 'history'} onClick={() => setMode('history')}>
            <History className="h-3.5 w-3.5" />
            {t('trafficLog.history', { defaultValue: 'History' })}
          </Button>
        </div>
      </CardHeader>

      <CardContent className="space-y-3 p-3 sm:p-4">
        <div className="flex flex-wrap items-end gap-2">
          <div className="w-full space-y-1 sm:w-auto">
            <Label htmlFor="traffic-log-username" className="text-[11px]">
              {t('trafficLog.filters.username', { defaultValue: 'Subscription' })}
            </Label>
            <Popover open={suggestOpen && (options.length > 0 || suggestNotice != null)} onOpenChange={setSuggestOpen}>
              <PopoverAnchor asChild>
                <div className="flex w-full sm:w-64">
                  <Input
                    id="traffic-log-username"
                    value={usernameInput}
                    autoComplete="off"
                    placeholder={t('trafficLog.filters.usernamePlaceholder', { defaultValue: 'Exact username' })}
                    className="h-8 text-xs"
                    onChange={event => {
                      setUsernameInput(event.target.value)
                      setSuggestOpen(event.target.value.length > 0)
                    }}
                    onFocus={() => setSuggestOpen(usernameInput.length > 0)}
                    onKeyDown={event => {
                      if (event.key === 'Escape') setSuggestOpen(false)
                    }}
                  />
                </div>
              </PopoverAnchor>
              <PopoverContent align="start" className="w-64 p-1" onOpenAutoFocus={event => event.preventDefault()}>
                {suggestNotice != null && <p className="text-muted-foreground px-2 py-1.5 text-[11px]">{suggestNotice}</p>}
                <ul>
                  {options.map(option => (
                    <li key={option}>
                      <button
                        type="button"
                        className="hover:bg-muted flex w-full items-center rounded px-2 py-1.5 text-start text-xs"
                        onClick={() => {
                          setUsernameInput(option)
                          setSuggestOpen(false)
                        }}
                      >
                        <bdi className="truncate">{option}</bdi>
                      </button>
                    </li>
                  ))}
                </ul>
              </PopoverContent>
            </Popover>
          </div>

          <div className="w-full space-y-1 sm:w-auto">
            <Label htmlFor="traffic-log-node" className="text-[11px]">
              {t('trafficLog.filters.node', { defaultValue: 'Node' })}
            </Label>
            <Select value={nodeId} onValueChange={setNodeId}>
              <SelectTrigger id="traffic-log-node" className="h-8 w-full text-xs sm:w-48">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value={ALL_NODES} className="text-xs">
                  {t('trafficLog.filters.allNodes', { defaultValue: 'All nodes' })}
                </SelectItem>
                {nodes.map(node => (
                  <SelectItem key={node.id} value={String(node.id)} className="text-xs">
                    {node.name}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>

          <div className="flex h-8 items-center gap-2">
            <Switch id="traffic-log-refused" checked={refusedOnly} onCheckedChange={setRefusedOnly} />
            <Label htmlFor="traffic-log-refused" className="text-[11px]">
              {t('trafficLog.filters.refusedOnly', { defaultValue: 'Refused only' })}
            </Label>
          </div>

          {hasFilters && (
            <Button type="button" variant="ghost" size="sm" className="h-8 text-xs" onClick={clearFilters}>
              <X className="h-3.5 w-3.5" />
              {t('trafficLog.filters.clear', { defaultValue: 'Clear' })}
            </Button>
          )}
        </div>

        <StatusStrip status={status} isLoading={statusQuery.isLoading} isSudo={isSudo} isToggling={toggleCollection.isPending} onToggle={enabled => toggleCollection.mutate(enabled)} />

        {unavailableReason != null && (
          <Alert className="border-destructive/40 bg-destructive/10 py-2">
            <AlertDescription className="text-destructive text-xs">
              {t('trafficLog.unavailable', {
                reason: unavailableReason || t('trafficLog.unknownReason', { defaultValue: 'the collector cannot run in this deployment' }),
                defaultValue: 'Live collection is unavailable: {{reason}}',
              })}
            </AlertDescription>
          </Alert>
        )}

        {controlRevoked && (
          <Alert className="border-destructive/40 bg-destructive/10 py-2">
            <AlertDescription className="text-destructive text-xs">
              {t('trafficLog.revokedBanner', { defaultValue: 'Your access to the traffic log was withdrawn while the feed was open. The live feed was closed and its rows were discarded. Reload the page.' })}
            </AlertDescription>
          </Alert>
        )}

        {isPausedCollection && (
          <Alert className="border-amber-500/40 bg-amber-500/10 py-2">
            <AlertDescription className="text-xs text-amber-700 dark:text-amber-400">
              {t('trafficLog.pausedBanner', { defaultValue: 'Collection is paused: nothing is being recorded and the live feed is stopped. The per-node log viewer is unaffected.' })}
            </AlertDescription>
          </Alert>
        )}

        {statusQuery.error != null && (
          <Alert className="border-destructive/40 bg-destructive/10 py-2">
            <AlertDescription className="text-destructive text-xs">{errorText(statusQuery.error, t('trafficLog.statusFailed', { defaultValue: 'Could not read the collection status.' }))}</AlertDescription>
          </Alert>
        )}

        {mode === 'live' ? (
          <LivePanel username={username} nodeId={nodeId} refusedOnly={refusedOnly} onControl={onControl} />
        ) : (
          <HistoryPanel username={username} nodeId={nodeId} refusedOnly={refusedOnly} retentionHours={retentionHours} />
        )}
      </CardContent>
    </Card>
  )
}
