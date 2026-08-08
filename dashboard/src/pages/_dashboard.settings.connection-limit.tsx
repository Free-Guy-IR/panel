import { DecimalInput } from '@/components/common/decimal-input'
import { Textarea } from '@/components/ui/textarea'
import { SubscriptionFormActions } from '@/features/subscriptions/components/subscription-form-actions'
import { Form, FormControl, FormDescription, FormField, FormItem, FormLabel, FormMessage } from '@/components/ui/form'
import { Skeleton } from '@/components/ui/skeleton'
import { Switch } from '@/components/ui/switch'
import { Checkbox } from '@/components/ui/checkbox'
import { useDefaultCdnRanges, useGetAdminsSimple, useGetGroupsSimple } from '@/service/api'
import { zodResolver } from '@hookform/resolvers/zod'
import { useMemo } from 'react'
import { useForm } from 'react-hook-form'
import { useTranslation } from 'react-i18next'
import { toast } from 'sonner'
import { z } from 'zod'
import { useSettingsContext } from './_dashboard.settings'

const connectionLimitSchema = z
  .object({
    enabled: z.boolean().default(false),
    monitor_only: z.boolean().default(true),
    device_limit: z.number().min(1).default(2),
    warn_at_devices: z.number().min(1).default(2),
    check_interval_seconds: z.number().min(30).default(120),
    online_window_seconds: z.number().min(60).default(180),
    concurrency_window_seconds: z.number().min(10).default(90),
    persistence_cycles: z.number().min(1).max(20).default(3),
    infrastructure_min_users: z.number().min(2).default(4),
    cdn_ranges: z.string().default(''),
    apply_to_group_ids: z.array(z.number()).default([]),
    apply_to_admin_ids: z.array(z.number()).default([]),
    resolve_isp: z.boolean().default(false),
  })
  .superRefine((data, ctx) => {
    if (data.warn_at_devices > data.device_limit) {
      ctx.addIssue({
        code: z.ZodIssueCode.custom,
        message: 'settings.connectionLimit.validation.warnAboveLimit',
        path: ['warn_at_devices'],
      })
    }
  })

type ConnectionLimitFormInput = z.input<typeof connectionLimitSchema>

const defaultValues: ConnectionLimitFormInput = {
  enabled: false,
  monitor_only: true,
  device_limit: 2,
  warn_at_devices: 2,
  check_interval_seconds: 120,
  online_window_seconds: 180,
  concurrency_window_seconds: 90,
  persistence_cycles: 3,
  infrastructure_min_users: 4,
  cdn_ranges: '',
  apply_to_group_ids: [],
  apply_to_admin_ids: [],
  resolve_isp: false,
}

const toPositive = (value: unknown, fallback: number): number => {
  const parsed = typeof value === 'number' ? value : typeof value === 'string' && value.trim() !== '' ? Number(value) : NaN
  if (!Number.isFinite(parsed) || parsed <= 0) return fallback
  return Math.floor(parsed)
}

export default function ConnectionLimitSettings() {
  const { t } = useTranslation()
  const { settings, isLoading, error, updateSettings, isSaving } = useSettingsContext()
  const { data: shippedRanges } = useDefaultCdnRanges()
  const { data: groupsData } = useGetGroupsSimple({ limit: 500 })
  const { data: adminsData } = useGetAdminsSimple({ limit: 500 })
  const groups = groupsData?.groups ?? []
  const admins = adminsData?.admins ?? []

  const formValues = useMemo<ConnectionLimitFormInput>(() => {
    const limit = settings?.connection_limit
    if (!limit) return defaultValues
    return {
      enabled: limit.enabled ?? false,
      monitor_only: limit.monitor_only ?? true,
      device_limit: toPositive(limit.device_limit, 2),
      warn_at_devices: toPositive(limit.warn_at_devices, 2),
      check_interval_seconds: toPositive(limit.check_interval_seconds, 120),
      online_window_seconds: toPositive(limit.online_window_seconds, 180),
      concurrency_window_seconds: toPositive(limit.concurrency_window_seconds, 90),
      persistence_cycles: toPositive(limit.persistence_cycles, 3),
      infrastructure_min_users: toPositive(limit.infrastructure_min_users, 4),
      cdn_ranges: (limit.cdn_ranges ?? []).join('\n'),
      apply_to_group_ids: limit.apply_to_group_ids ?? [],
      apply_to_admin_ids: limit.apply_to_admin_ids ?? [],
      resolve_isp: limit.resolve_isp ?? false,
    }
  }, [settings?.connection_limit])

  const form = useForm<ConnectionLimitFormInput>({
    resolver: zodResolver(connectionLimitSchema),
    values: formValues,
  })

  const onSubmit = async (data: ConnectionLimitFormInput) => {
    try {
      await updateSettings({
        connection_limit: {
          enabled: data.enabled,
          monitor_only: data.monitor_only,
          device_limit: toPositive(data.device_limit, 2),
          warn_at_devices: toPositive(data.warn_at_devices, 2),
          check_interval_seconds: toPositive(data.check_interval_seconds, 120),
          online_window_seconds: toPositive(data.online_window_seconds, 180),
          concurrency_window_seconds: toPositive(data.concurrency_window_seconds, 90),
          persistence_cycles: toPositive(data.persistence_cycles, 3),
          infrastructure_min_users: toPositive(data.infrastructure_min_users, 4),
          cdn_ranges: (data.cdn_ranges || '')
            .split('\n')
            .map(line => line.trim())
            .filter(Boolean),
          apply_to_group_ids: data.apply_to_group_ids ?? [],
          apply_to_admin_ids: data.apply_to_admin_ids ?? [],
          resolve_isp: data.resolve_isp ?? false,
        },
      })
    } catch {
      // Surfaced by the settings context.
    }
  }

  const handleCancel = () => {
    form.reset(formValues)
    toast.success(t('settings.connectionLimit.cancelSuccess', { defaultValue: 'Changes cancelled' }))
  }

  const useShippedRanges = () => {
    form.setValue('cdn_ranges', (shippedRanges ?? []).join('\n'), { shouldDirty: true })
  }

  const enabled = form.watch('enabled')

  if (isLoading) {
    return (
      <div className="w-full p-4 sm:py-6 lg:py-8">
        <div className="space-y-6">
          <Skeleton className="h-16 w-full" />
          <div className="grid gap-4 md:grid-cols-3">
            <Skeleton className="h-24" />
            <Skeleton className="h-24" />
            <Skeleton className="h-24" />
          </div>
        </div>
      </div>
    )
  }

  if (error) {
    return (
      <div className="flex min-h-[400px] items-center justify-center p-4 sm:py-6 lg:py-8">
        <p className="text-destructive text-sm">
          {t('settings.connectionLimit.loadError', { defaultValue: 'Failed to load connection limit settings' })}
        </p>
      </div>
    )
  }

  return (
    <div className="w-full">
      <Form {...form}>
        <form onSubmit={form.handleSubmit(onSubmit)} className="space-y-6 p-4 sm:py-6 lg:py-8">
          <section className="space-y-4">
            <div className="space-y-1.5">
              <h3 className="text-base font-semibold sm:text-lg">
                {t('settings.connectionLimit.detection.title', { defaultValue: 'Device detection' })}
              </h3>
              <p className="text-muted-foreground max-w-3xl text-xs leading-relaxed sm:text-sm">
                {t('settings.connectionLimit.detection.description', {
                  defaultValue:
                    'Counts how many devices are using each subscription at once, from the addresses the nodes report and the hardware IDs clients send.',
                })}
              </p>
            </div>

            <div className="grid gap-3 lg:grid-cols-2">
              <FormField
                control={form.control}
                name="enabled"
                render={({ field }) => (
                  <FormItem className="bg-card hover:bg-accent/50 flex flex-row items-center justify-between gap-4 space-y-0 rounded-md border p-3 transition-colors sm:p-4">
                    <div className="min-w-0 flex-1 space-y-1">
                      <FormLabel className="cursor-pointer text-sm font-medium">
                        {t('settings.connectionLimit.enabled.title', { defaultValue: 'Enable detection' })}
                      </FormLabel>
                      <FormDescription className="text-xs leading-relaxed sm:text-sm">
                        {t('settings.connectionLimit.enabled.description', {
                          defaultValue: 'Check online users on a schedule and record how many devices each has.',
                        })}
                      </FormDescription>
                    </div>
                    <FormControl>
                      <Switch checked={field.value} onCheckedChange={field.onChange} className="shrink-0" />
                    </FormControl>
                  </FormItem>
                )}
              />

              <FormField
                control={form.control}
                name="monitor_only"
                render={({ field }) => (
                  <FormItem className="bg-card hover:bg-accent/50 flex flex-row items-center justify-between gap-4 space-y-0 rounded-md border p-3 transition-colors sm:p-4">
                    <div className="min-w-0 flex-1 space-y-1">
                      <FormLabel className="cursor-pointer text-sm font-medium">
                        {t('settings.connectionLimit.monitorOnly.title', { defaultValue: 'Monitor only' })}
                      </FormLabel>
                      <FormDescription className="text-xs leading-relaxed sm:text-sm">
                        {t('settings.connectionLimit.monitorOnly.description', {
                          defaultValue: 'Record what is seen without restricting anyone. Leave on until the numbers look right.',
                        })}
                      </FormDescription>
                    </div>
                    <FormControl>
                      <Switch checked={field.value} onCheckedChange={field.onChange} disabled={!enabled} className="shrink-0" />
                    </FormControl>
                  </FormItem>
                )}
              />
            </div>
          </section>

          <section className="space-y-4">
            <div className="space-y-1.5">
              <h3 className="text-base font-semibold sm:text-lg">
                {t('settings.connectionLimit.limits.title', { defaultValue: 'Device allowance' })}
              </h3>
            </div>

            <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
              <FormField
                control={form.control}
                name="device_limit"
                render={({ field }) => (
                  <FormItem className="space-y-2">
                    <FormLabel className="text-sm font-medium">
                      {t('settings.connectionLimit.deviceLimit.title', { defaultValue: 'Device limit' })}
                    </FormLabel>
                    <FormControl>
                      <DecimalInput placeholder="2" value={field.value} emptyValue={2} normalizeDisplayValueOnBlur={Math.floor} onValueChange={v => field.onChange(v ?? 2)} />
                    </FormControl>
                    <FormDescription className="text-xs leading-relaxed sm:text-sm">
                      {t('settings.connectionLimit.deviceLimit.description', { defaultValue: 'Above this many devices counts as over the limit.' })}
                    </FormDescription>
                    <FormMessage />
                  </FormItem>
                )}
              />

              <FormField
                control={form.control}
                name="warn_at_devices"
                render={({ field }) => (
                  <FormItem className="space-y-2">
                    <FormLabel className="text-sm font-medium">
                      {t('settings.connectionLimit.warnAt.title', { defaultValue: 'Warn at' })}
                    </FormLabel>
                    <FormControl>
                      <DecimalInput placeholder="2" value={field.value} emptyValue={2} normalizeDisplayValueOnBlur={Math.floor} onValueChange={v => field.onChange(v ?? 2)} />
                    </FormControl>
                    <FormDescription className="text-xs leading-relaxed sm:text-sm">
                      {t('settings.connectionLimit.warnAt.description', { defaultValue: 'Highlight from this many devices, before the limit is passed.' })}
                    </FormDescription>
                    <FormMessage />
                  </FormItem>
                )}
              />

              <FormField
                control={form.control}
                name="check_interval_seconds"
                render={({ field }) => (
                  <FormItem className="space-y-2">
                    <FormLabel className="text-sm font-medium">
                      {t('settings.connectionLimit.interval.title', { defaultValue: 'Check every (seconds)' })}
                    </FormLabel>
                    <FormControl>
                      <DecimalInput placeholder="120" value={field.value} emptyValue={120} normalizeDisplayValueOnBlur={Math.floor} onValueChange={v => field.onChange(v ?? 120)} />
                    </FormControl>
                    <FormMessage />
                  </FormItem>
                )}
              />

              <FormField
                control={form.control}
                name="concurrency_window_seconds"
                render={({ field }) => (
                  <FormItem className="space-y-2">
                    <FormLabel className="text-sm font-medium">
                      {t('settings.connectionLimit.concurrency.title', { defaultValue: 'Concurrency window (seconds)' })}
                    </FormLabel>
                    <FormControl>
                      <DecimalInput placeholder="90" value={field.value} emptyValue={90} normalizeDisplayValueOnBlur={Math.floor} onValueChange={v => field.onChange(v ?? 90)} />
                    </FormControl>
                    <FormDescription className="text-xs leading-relaxed sm:text-sm">
                      {t('settings.connectionLimit.concurrency.description', {
                        defaultValue: 'Addresses seen further apart than this are treated as the same person after an address change.',
                      })}
                    </FormDescription>
                    <FormMessage />
                  </FormItem>
                )}
              />

              <FormField
                control={form.control}
                name="persistence_cycles"
                render={({ field }) => (
                  <FormItem className="space-y-2">
                    <FormLabel className="text-sm font-medium">
                      {t('settings.connectionLimit.persistence.title', { defaultValue: 'Checks a pattern must hold for' })}
                    </FormLabel>
                    <FormControl>
                      <DecimalInput placeholder="3" value={field.value} emptyValue={3} normalizeDisplayValueOnBlur={Math.floor} onValueChange={v => field.onChange(v ?? 3)} />
                    </FormControl>
                    <FormDescription className="text-xs leading-relaxed sm:text-sm">
                      {t('settings.connectionLimit.persistence.description', {
                        defaultValue:
                          'Being on several nodes is only reported once it has held this many checks in a row. An app that tries every server reaches a high count for one check and drops back.',
                      })}
                    </FormDescription>
                    <FormMessage />
                  </FormItem>
                )}
              />
            </div>
          </section>

          <section className="space-y-4">
            <div className="space-y-1.5">
              <h3 className="text-base font-semibold sm:text-lg">
                {t('settings.connectionLimit.accuracy.title', { defaultValue: 'Avoiding false positives' })}
              </h3>
              <p className="text-muted-foreground max-w-3xl text-xs leading-relaxed sm:text-sm">
                {t('settings.connectionLimit.accuracy.description', {
                  defaultValue:
                    'A CDN shows one device as many addresses, and a tunnel shows many devices as one. These settings keep either from being mistaken for a device count.',
                })}
              </p>
            </div>

            <FormField
              control={form.control}
              name="infrastructure_min_users"
              render={({ field }) => (
                <FormItem className="max-w-xs space-y-2">
                  <FormLabel className="text-sm font-medium">
                    {t('settings.connectionLimit.infra.title', { defaultValue: 'Treat as shared infrastructure at' })}
                  </FormLabel>
                  <FormControl>
                    <DecimalInput placeholder="4" value={field.value} emptyValue={4} normalizeDisplayValueOnBlur={Math.floor} onValueChange={v => field.onChange(v ?? 4)} />
                  </FormControl>
                  <FormDescription className="text-xs leading-relaxed sm:text-sm">
                    {t('settings.connectionLimit.infra.description', {
                      defaultValue: 'An address seen under this many different users is a tunnel or NAT, not a device, and is ignored.',
                    })}
                  </FormDescription>
                  <FormMessage />
                </FormItem>
              )}
            />

            <FormField
              control={form.control}
              name="cdn_ranges"
              render={({ field }) => (
                <FormItem className="space-y-2">
                  <div className="flex items-center justify-between gap-2">
                    <FormLabel className="text-sm font-medium">
                      {t('settings.connectionLimit.cdn.title', { defaultValue: 'CDN address ranges' })}
                    </FormLabel>
                    <button
                      type="button"
                      onClick={useShippedRanges}
                      className="text-primary hover:text-primary/80 text-xs font-medium transition-colors"
                    >
                      {t('settings.connectionLimit.cdn.useDefaults', { defaultValue: 'Use built-in list' })}
                    </button>
                  </div>
                  <FormControl>
                    <Textarea dir="ltr" rows={6} placeholder="162.158.0.0/15&#10;151.101.0.0/16" className="font-mono text-xs" {...field} />
                  </FormControl>
                  <FormDescription className="text-xs leading-relaxed sm:text-sm">
                    {t('settings.connectionLimit.cdn.description', {
                      defaultValue:
                        'One range per line. All of a user’s addresses inside these ranges count as one device, since a CDN hides how many are behind it. The built-in list covers Cloudflare and Fastly.',
                    })}
                  </FormDescription>
                  <FormMessage />
                </FormItem>
              )}
            />
          </section>

          <section className="space-y-4">
            <div className="space-y-1.5">
              <h3 className="text-base font-semibold sm:text-lg">
                {t('settings.connectionLimit.scope.title', { defaultValue: 'Who is checked' })}
              </h3>
              <p className="text-muted-foreground max-w-3xl text-xs leading-relaxed sm:text-sm">
                {t('settings.connectionLimit.scope.description', {
                  defaultValue: 'Leave both empty to check every user. Choosing any narrows checking to those users only.',
                })}
              </p>
            </div>

            <div className="grid gap-4 lg:grid-cols-2">
              <FormField
                control={form.control}
                name="apply_to_group_ids"
                render={({ field }) => (
                  <FormItem className="space-y-2">
                    <div className="flex items-center justify-between gap-2">
                      <FormLabel className="text-sm font-medium">
                        {t('settings.connectionLimit.scope.groups', { defaultValue: 'Groups' })}
                      </FormLabel>
                      <span className="text-muted-foreground text-xs">
                        {(field.value ?? []).length === 0
                          ? t('settings.connectionLimit.scope.allGroups', { defaultValue: 'all groups' })
                          : `${(field.value ?? []).length}/${groups.length}`}
                      </span>
                    </div>
                    <div className="max-h-52 space-y-1 overflow-y-auto rounded-md border p-2">
                      {groups.length === 0 ? (
                        <p className="text-muted-foreground px-1 py-2 text-xs">
                          {t('settings.connectionLimit.scope.noGroups', { defaultValue: 'No groups yet' })}
                        </p>
                      ) : (
                        groups.map(group => {
                          const selected = (field.value ?? []).includes(group.id)
                          return (
                            <label
                              key={group.id}
                              className="hover:bg-muted/40 flex cursor-pointer items-center gap-x-2 rounded-sm px-2 py-1.5 transition-colors"
                            >
                              <Checkbox
                                checked={selected}
                                onCheckedChange={checked => {
                                  const current = new Set(field.value ?? [])
                                  if (checked === true) current.add(group.id)
                                  else current.delete(group.id)
                                  field.onChange(Array.from(current))
                                }}
                                className="h-4 w-4"
                              />
                              <span className="truncate text-xs">{group.name}</span>
                            </label>
                          )
                        })
                      )}
                    </div>
                    <FormMessage />
                  </FormItem>
                )}
              />

              <FormField
                control={form.control}
                name="apply_to_admin_ids"
                render={({ field }) => (
                  <FormItem className="space-y-2">
                    <div className="flex items-center justify-between gap-2">
                      <FormLabel className="text-sm font-medium">
                        {t('settings.connectionLimit.scope.admins', { defaultValue: 'Admins' })}
                      </FormLabel>
                      <span className="text-muted-foreground text-xs">
                        {(field.value ?? []).length === 0
                          ? t('settings.connectionLimit.scope.allAdmins', { defaultValue: 'all admins' })
                          : `${(field.value ?? []).length}/${admins.length}`}
                      </span>
                    </div>
                    <div className="max-h-52 space-y-1 overflow-y-auto rounded-md border p-2">
                      {admins.map(admin => {
                        const selected = (field.value ?? []).includes(admin.id)
                        return (
                          <label
                            key={admin.id}
                            className="hover:bg-muted/40 flex cursor-pointer items-center gap-x-2 rounded-sm px-2 py-1.5 transition-colors"
                          >
                            <Checkbox
                              checked={selected}
                              onCheckedChange={checked => {
                                const current = new Set(field.value ?? [])
                                if (checked === true) current.add(admin.id)
                                else current.delete(admin.id)
                                field.onChange(Array.from(current))
                              }}
                              className="h-4 w-4"
                            />
                            <span className="truncate text-xs">{admin.username}</span>
                          </label>
                        )
                      })}
                    </div>
                    <FormMessage />
                  </FormItem>
                )}
              />
            </div>

            <FormField
              control={form.control}
              name="resolve_isp"
              render={({ field }) => (
                <FormItem className="bg-card hover:bg-accent/50 flex flex-row items-center justify-between gap-4 space-y-0 rounded-md border p-3 transition-colors sm:p-4">
                  <div className="min-w-0 flex-1 space-y-1">
                    <FormLabel className="cursor-pointer text-sm font-medium">
                      {t('settings.connectionLimit.isp.title', { defaultValue: 'Look up the provider behind an address' })}
                    </FormLabel>
                    <FormDescription className="text-xs leading-relaxed sm:text-sm">
                      {t('settings.connectionLimit.isp.description', {
                        defaultValue:
                          'Shows the ISP name when reviewing a user. Only when a case is opened, never during checking — but it does send that address to a third-party service.',
                      })}
                    </FormDescription>
                  </div>
                  <FormControl>
                    <Switch checked={field.value} onCheckedChange={field.onChange} className="shrink-0" />
                  </FormControl>
                </FormItem>
              )}
            />
          </section>

          <SubscriptionFormActions onCancel={handleCancel} isSaving={isSaving} />
        </form>
      </Form>
    </div>
  )
}
