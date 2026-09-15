import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Checkbox } from '@/components/ui/checkbox'
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from '@/components/ui/collapsible'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { Separator } from '@/components/ui/separator'
import { Skeleton } from '@/components/ui/skeleton'
import { Switch } from '@/components/ui/switch'
import useDirDetection from '@/hooks/use-dir-detection'
import { cn } from '@/lib/utils'
import { fetcher } from '@/service/http'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  Baby,
  ChevronDown,
  Clapperboard,
  Gamepad2,
  Loader2,
  Megaphone,
  Plus,
  Search,
  ShieldAlert,
  ShieldBan,
  ShieldCheck,
  Trash2,
  Users,
  X,
} from 'lucide-react'
import { useEffect, useMemo, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { toast } from 'sonner'

type CatalogService = { key: string; geosite: string; domains: number }
type CatalogGroup = { key: string; geosite: string | null; domains: number; services: CatalogService[] }
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
  inbounds: TargetInbound[]
}
type Assignment = {
  id: number
  profile_id: number
  node_id: number
  inbound_tag: string
  is_enabled: boolean
  enforced: boolean
  last_checked_at: string | null
  last_error: string | null
}

const BASE = '/api/content-filter'

const GROUP_ICON: Record<string, typeof ShieldBan> = {
  adult: Baby,
  social: Users,
  games: Gamepad2,
  streaming: Clapperboard,
  ads: Megaphone,
}

const GROUP_TONE: Record<string, string> = {
  adult: 'text-rose-600 dark:text-rose-400',
  social: 'text-sky-600 dark:text-sky-400',
  games: 'text-violet-600 dark:text-violet-400',
  streaming: 'text-amber-600 dark:text-amber-400',
  ads: 'text-emerald-600 dark:text-emerald-400',
}

function useCatalog() {
  return useQuery({
    queryKey: ['content-filter', 'catalog'],
    queryFn: () => fetcher<CatalogGroup[]>(`${BASE}/catalog`),
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

function errorText(e: unknown, fallback: string): string {
  const detail = (e as { data?: { detail?: unknown } })?.data?.detail
  if (typeof detail === 'string') return detail
  if (Array.isArray(detail) && detail.length) {
    const first = detail[0] as { msg?: string }
    if (first?.msg) return first.msg
  }
  return (e as Error)?.message || fallback
}

function DomainCount({ n }: { n: number }) {
  const { t } = useTranslation()
  return (
    <span className="text-xs tabular-nums text-muted-foreground">
      {t('contentFilter.domainCount', { count: n, defaultValue: '{{count}} sites' })}
    </span>
  )
}

function CategoryGroupCard({
  group,
  selected,
  onToggleGroup,
  onToggleService,
}: {
  group: CatalogGroup
  selected: Set<string>
  onToggleGroup: (key: string, on: boolean) => void
  onToggleService: (key: string, on: boolean) => void
}) {
  const { t } = useTranslation()
  const [open, setOpen] = useState(false)
  const Icon = GROUP_ICON[group.key] ?? ShieldBan
  const whole = selected.has(group.key)
  const picked = group.services.filter(s => selected.has(s.key)).length
  const partial = !whole && picked > 0

  return (
    <Card
      className={cn(
        'flex h-full flex-col overflow-hidden transition-colors',
        (whole || partial) && 'border-primary/40 bg-primary/[0.03]',
      )}
    >
      <CardHeader className="flex flex-row items-start justify-between gap-3 space-y-0 pb-3">
        <div className="flex min-w-0 items-start gap-3">
          <Icon className={cn('mt-0.5 size-5 shrink-0', GROUP_TONE[group.key])} />
          <div className="min-w-0">
            <CardTitle className="text-base">
              {t(`contentFilter.groups.${group.key}`, { defaultValue: group.key })}
            </CardTitle>
            <CardDescription className="mt-1 flex flex-wrap items-center gap-x-2 gap-y-1">
              <DomainCount n={group.domains} />
              {partial ? (
                <Badge variant="secondary" className="text-[11px]">
                  {t('contentFilter.partial', {
                    picked,
                    total: group.services.length,
                    defaultValue: '{{picked}} of {{total}}',
                  })}
                </Badge>
              ) : null}
            </CardDescription>
          </div>
        </div>
        <Switch
          checked={whole}
          onCheckedChange={on => onToggleGroup(group.key, on)}
          aria-label={t(`contentFilter.groups.${group.key}`, { defaultValue: group.key })}
        />
      </CardHeader>
      <CardContent className="mt-auto pb-3 pt-0">
        <Collapsible open={open} onOpenChange={setOpen}>
          <CollapsibleTrigger asChild>
            <Button variant="ghost" size="sm" className="h-7 gap-1 px-2 text-xs text-muted-foreground">
              <ChevronDown className={cn('size-3.5 transition-transform', open && 'rotate-180')} />
              {t('contentFilter.pickIndividually', {
                count: group.services.length,
                defaultValue: 'Pick individually ({{count}})',
              })}
            </Button>
          </CollapsibleTrigger>
          <CollapsibleContent className="pt-2">
            <div className="grid grid-cols-1 gap-x-4 gap-y-1 sm:grid-cols-2">
              {group.services.map(service => (
                <label
                  key={service.key}
                  className={cn(
                    'flex cursor-pointer items-center justify-between gap-2 rounded-md px-2 py-1.5 hover:bg-muted/60',
                    whole && 'cursor-not-allowed opacity-50',
                  )}
                >
                  <span className="flex items-center gap-2">
                    <Checkbox
                      checked={whole || selected.has(service.key)}
                      disabled={whole}
                      onCheckedChange={on => onToggleService(service.key, on === true)}
                    />
                    <span className="text-sm">
                      {t(`contentFilter.services.${service.key}`, { defaultValue: service.key })}
                    </span>
                  </span>
                  <DomainCount n={service.domains} />
                </label>
              ))}
            </div>
            {whole ? (
              <p className="px-2 pt-2 text-xs text-muted-foreground">
                {t('contentFilter.wholeGroupNote', {
                  defaultValue: 'The whole group is blocked, so every service in it is already covered.',
                })}
              </p>
            ) : null}
          </CollapsibleContent>
        </Collapsible>
      </CardContent>
    </Card>
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

  const catalog = useCatalog()
  const profiles = useProfiles()
  const targets = useTargets()
  const assignments = useAssignments()

  const [activeId, setActiveId] = useState<number | null>(null)
  const [draft, setDraft] = useState<Profile | null>(null)
  const [assignOpen, setAssignOpen] = useState(false)
  const [assignNode, setAssignNode] = useState<string>('')
  const [assignInbound, setAssignInbound] = useState<string>('')
  const [probeDomain, setProbeDomain] = useState('')
  const [probeResult, setProbeResult] = useState<{ domain: string; blocked: boolean; outbound: string } | null>(null)

  useEffect(() => {
    if (activeId === null && profiles.data?.length) setActiveId(profiles.data[0].id)
  }, [profiles.data, activeId])

  useEffect(() => {
    const found = profiles.data?.find(p => p.id === activeId) ?? null
    setDraft(found ? { ...found, categories: [...found.categories], allow_list: [...found.allow_list], block_list: [...found.block_list] } : null)
  }, [activeId, profiles.data])

  const selected = useMemo(() => new Set(draft?.categories ?? []), [draft])

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
    mutationFn: (profile: Profile) =>
      fetcher<Profile>(`${BASE}/profiles/${profile.id}`, {
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
    onError: e => toast.error(errorText(e, t('contentFilter.saveFailed', { defaultValue: 'Could not save' }))),
  })

  const removeProfile = useMutation({
    mutationFn: (id: number) => fetcher<void>(`${BASE}/profiles/${id}`, { method: 'DELETE' }),
    onSuccess: () => {
      toast.success(t('contentFilter.profileDeleted', { defaultValue: 'Profile removed' }))
      setActiveId(null)
      invalidate()
    },
    onError: e => toast.error(errorText(e, t('contentFilter.deleteFailed', { defaultValue: 'Could not remove' }))),
  })

  const addAssignment = useMutation({
    mutationFn: (body: { profile_id: number; node_id: number; inbound_tag: string }) =>
      fetcher<Assignment>(`${BASE}/assignments`, { method: 'POST', body: { ...body, is_enabled: true } }),
    onSuccess: () => {
      toast.success(t('contentFilter.applied', { defaultValue: 'Applied and confirmed on the node' }))
      setAssignOpen(false)
      setAssignNode('')
      setAssignInbound('')
      invalidate()
    },
    onError: e => toast.error(errorText(e, t('contentFilter.applyFailed', { defaultValue: 'Could not apply' }))),
  })

  const removeAssignment = useMutation({
    mutationFn: (id: number) => fetcher<void>(`${BASE}/assignments/${id}`, { method: 'DELETE' }),
    onSuccess: () => {
      toast.success(t('contentFilter.lifted', { defaultValue: 'Filter lifted from that endpoint' }))
      invalidate()
    },
    onError: e => toast.error(errorText(e, t('contentFilter.liftFailed', { defaultValue: 'Could not lift' }))),
  })

  const runProbe = useMutation({
    mutationFn: (payload: { node_id: number; inbound_tag: string; domain: string }) =>
      fetcher<{ domain: string; outbound: string; blocked: boolean }>(`${BASE}/test`, { method: 'POST', body: payload }),
    onSuccess: r => setProbeResult(r),
    onError: e => toast.error(errorText(e, t('contentFilter.probeFailed', { defaultValue: 'Could not check' }))),
  })

  const toggleGroup = (key: string, on: boolean) => {
    if (!draft) return
    const group = catalog.data?.find(g => g.key === key)
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

  const toggleService = (key: string, on: boolean) => {
    if (!draft) return
    const next = new Set(draft.categories)
    if (on) next.add(key)
    else next.delete(key)
    setDraft({ ...draft, categories: [...next] })
  }

  const profileAssignments = (assignments.data ?? []).filter(a => a.profile_id === activeId)
  const nodeById = new Map((targets.data ?? []).map(n => [n.id, n]))
  const chosenNode = assignNode ? nodeById.get(Number(assignNode)) : undefined

  const loading = catalog.isLoading || profiles.isLoading

  return (
    <div dir={dir} className="space-y-6 pb-10">
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
            const name = window.prompt(t('contentFilter.namePrompt', { defaultValue: 'Name this profile' }) as string, '')
            if (name && name.trim()) createProfile.mutate(name.trim())
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
                const name = window.prompt(t('contentFilter.namePrompt', { defaultValue: 'Name this profile' }) as string, '')
                if (name && name.trim()) createProfile.mutate(name.trim())
              }}
            >
              <Plus className="size-4" />
              {t('contentFilter.createFirst', { defaultValue: 'Create the first profile' })}
            </Button>
          </CardContent>
        </Card>
      ) : (
        <div className="grid items-start gap-5 lg:grid-cols-[232px_minmax(0,1fr)]">
          <div className="space-y-2 lg:sticky lg:top-4">
            {profiles.data.map(profile => {
              const count = (assignments.data ?? []).filter(a => a.profile_id === profile.id).length
              return (
                <button
                  key={profile.id}
                  onClick={() => setActiveId(profile.id)}
                  className={cn(
                    'w-full rounded-lg border px-3 py-2.5 text-start transition-colors',
                    profile.id === activeId ? 'border-primary bg-primary/5' : 'hover:bg-muted/60',
                  )}
                >
                  <div className="truncate text-sm font-medium">{profile.name}</div>
                  <div className="mt-0.5 flex items-center gap-2 text-xs text-muted-foreground">
                    <span>
                      {t('contentFilter.categoryCount', {
                        count: profile.categories.length,
                        defaultValue: '{{count}} categories',
                      })}
                    </span>
                    {count ? (
                      <Badge variant="secondary" className="h-4 px-1.5 text-[10px]">
                        {t('contentFilter.endpointCount', { count, defaultValue: '{{count}} endpoints' })}
                      </Badge>
                    ) : null}
                  </div>
                </button>
              )
            })}
          </div>

          {draft ? (
            <div className="space-y-5">
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
                  onClick={() => {
                    if (window.confirm(t('contentFilter.confirmDelete', { defaultValue: 'Remove this profile and lift it everywhere it applies?' }) as string)) {
                      removeProfile.mutate(draft.id)
                    }
                  }}
                >
                  <Trash2 className="size-4" />
                </Button>
              </div>

              <div className="grid auto-rows-fr gap-3 sm:grid-cols-2">
                {(catalog.data ?? []).map(group => (
                  <CategoryGroupCard
                    key={group.key}
                    group={group}
                    selected={selected}
                    onToggleGroup={toggleGroup}
                    onToggleService={toggleService}
                  />
                ))}
              </div>

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
                <Button onClick={() => saveProfile.mutate(draft)} disabled={saveProfile.isPending} className="gap-1.5">
                  {saveProfile.isPending ? <Loader2 className="size-4 animate-spin" /> : <ShieldCheck className="size-4" />}
                  {t('contentFilter.save', { defaultValue: 'Save and apply' })}
                </Button>
              </div>

              <Separator />

              <Card>
                <CardHeader className="flex flex-row items-center justify-between space-y-0 pb-3">
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
                    {t('contentFilter.addEndpoint', { defaultValue: 'Apply to an endpoint' })}
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
                    <div className="space-y-2">
                      {profileAssignments.map(a => {
                        const node = nodeById.get(a.node_id)
                        return (
                          <div key={a.id} className="flex flex-wrap items-center justify-between gap-3 rounded-lg border px-3 py-2.5">
                            <div className="min-w-0">
                              <div className="truncate text-sm font-medium">
                                {node?.name ?? `#${a.node_id}`}
                                <span className="text-muted-foreground"> · </span>
                                {a.inbound_tag || t('contentFilter.wholeNode', { defaultValue: 'whole node' })}
                              </div>
                              {a.last_error ? (
                                <p className="mt-0.5 line-clamp-2 text-xs text-destructive">{a.last_error}</p>
                              ) : null}
                            </div>
                            <div className="flex items-center gap-2">
                              {a.enforced ? (
                                <Badge className="gap-1 border-emerald-500/40 bg-emerald-500/10 text-emerald-700 hover:bg-emerald-500/10 dark:text-emerald-400">
                                  <ShieldCheck className="size-3" />
                                  {t('contentFilter.enforced', { defaultValue: 'In force' })}
                                </Badge>
                              ) : (
                                <Badge variant="outline" className="gap-1 border-destructive/40 bg-destructive/10 text-destructive">
                                  <ShieldAlert className="size-3" />
                                  {t('contentFilter.notEnforced', { defaultValue: 'Not confirmed' })}
                                </Badge>
                              )}
                              <Button
                                size="icon"
                                variant="ghost"
                                className="size-8"
                                onClick={() => removeAssignment.mutate(a.id)}
                                aria-label={t('contentFilter.remove', { defaultValue: 'Remove' })}
                              >
                                <Trash2 className="size-4" />
                              </Button>
                            </div>
                          </div>
                        )
                      })}
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
                          if (e.key === 'Enter' && probeDomain.trim()) {
                            const first = profileAssignments[0]
                            runProbe.mutate({ node_id: first.node_id, inbound_tag: first.inbound_tag, domain: probeDomain.trim() })
                          }
                        }}
                      />
                      <Button
                        variant="secondary"
                        className="h-9"
                        disabled={!probeDomain.trim() || runProbe.isPending}
                        onClick={() => {
                          const first = profileAssignments[0]
                          runProbe.mutate({ node_id: first.node_id, inbound_tag: first.inbound_tag, domain: probeDomain.trim() })
                        }}
                      >
                        {runProbe.isPending ? <Loader2 className="size-4 animate-spin" /> : t('contentFilter.check', { defaultValue: 'Check' })}
                      </Button>
                    </div>
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

      <Dialog open={assignOpen} onOpenChange={setAssignOpen}>
        <DialogContent dir={dir}>
          <DialogHeader>
            <DialogTitle>{t('contentFilter.addEndpoint', { defaultValue: 'Apply to an endpoint' })}</DialogTitle>
            <DialogDescription>
              {t('contentFilter.addEndpointBody', {
                defaultValue: 'Endpoints that cannot enforce destination rules are listed with the reason and cannot be chosen.',
              })}
            </DialogDescription>
          </DialogHeader>
          <div className="space-y-4">
            <div>
              <Label className="text-xs text-muted-foreground">{t('contentFilter.node', { defaultValue: 'Node' })}</Label>
              <Select
                value={assignNode}
                onValueChange={v => {
                  setAssignNode(v)
                  setAssignInbound('')
                }}
              >
                <SelectTrigger className="mt-1">
                  <SelectValue placeholder={t('contentFilter.pickNode', { defaultValue: 'Choose a node' })} />
                </SelectTrigger>
                <SelectContent>
                  {(targets.data ?? []).map(node => (
                    <SelectItem key={node.id} value={String(node.id)}>
                      {node.name}
                      {node.routing_service ? '' : ` — ${t('contentFilter.noRouting', { defaultValue: 'cannot filter' })}`}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
            <div>
              <Label className="text-xs text-muted-foreground">{t('contentFilter.endpoint', { defaultValue: 'Endpoint' })}</Label>
              <Select value={assignInbound} onValueChange={setAssignInbound} disabled={!chosenNode}>
                <SelectTrigger className="mt-1">
                  <SelectValue placeholder={t('contentFilter.pickEndpoint', { defaultValue: 'Choose an endpoint' })} />
                </SelectTrigger>
                <SelectContent>
                  {(chosenNode?.inbounds ?? []).map(inbound => (
                    <SelectItem key={inbound.tag} value={inbound.tag} disabled={!inbound.filterable}>
                      {inbound.tag} · {inbound.protocol}
                      {inbound.filterable ? '' : ` — ${inbound.reason}`}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
          </div>
          <DialogFooter>
            <Button variant="ghost" onClick={() => setAssignOpen(false)}>
              {t('contentFilter.cancel', { defaultValue: 'Cancel' })}
            </Button>
            <Button
              disabled={!assignNode || !assignInbound || addAssignment.isPending || activeId === null}
              onClick={() =>
                addAssignment.mutate({ profile_id: activeId as number, node_id: Number(assignNode), inbound_tag: assignInbound })
              }
            >
              {addAssignment.isPending ? <Loader2 className="size-4 animate-spin" /> : null}
              {t('contentFilter.apply', { defaultValue: 'Apply' })}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  )
}
