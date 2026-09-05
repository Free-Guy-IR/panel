import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { Skeleton } from '@/components/ui/skeleton'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { renderReason, type ConnectionReason } from '@/features/users/components/connection-reasons'
import useDirDetection from '@/hooks/use-dir-detection'
import { cn } from '@/lib/utils'
import { useListViolations, useReleaseUser } from '@/service/api'
import { useQueryClient } from '@tanstack/react-query'
import type { TFunction } from 'i18next'
import dayjs from 'dayjs'
import { RefreshCw, ShieldAlert } from 'lucide-react'
import { useState } from 'react'
import { useTranslation } from 'react-i18next'
import { toast } from 'sonner'

const PAGE_SIZE = 25

/** Zero is a warning that changed nothing; below zero stays until a person lifts it. */
const describeStep = (minutes: number, t: TFunction): string => {
  if (minutes === 0) return t('settings.connectionLimit.violations.warned', { defaultValue: 'Warned' })
  if (minutes < 0) return t('settings.connectionLimit.violations.untilLifted', { defaultValue: 'Until lifted' })
  return t('settings.connectionLimit.violations.disabledFor', { count: minutes, defaultValue: 'Disabled {{count}} min' })
}

const stepTone = (minutes: number) => {
  if (minutes === 0) return 'border-border bg-muted/50 text-muted-foreground'
  if (minutes < 0) return 'border-destructive/40 bg-destructive/10 text-destructive'
  return 'border-amber-500/40 bg-amber-500/10 text-amber-600 dark:text-amber-400'
}

export default function ConnectionLimitViolations() {
  const { t } = useTranslation()
  const dir = useDirDetection()
  const isRTL = dir === 'rtl'
  const queryClient = useQueryClient()
  const [scope, setScope] = useState('all')
  const [page, setPage] = useState(0)

  const { data, isLoading, isFetching, refetch } = useListViolations(
    { active_only: scope === 'active', limit: PAGE_SIZE, offset: page * PAGE_SIZE },
    { query: { refetchOnWindowFocus: false, placeholderData: previous => previous } },
  )

  const { mutateAsync: releaseUser, isPending: isReleasing } = useReleaseUser()

  const violations = data?.violations ?? []
  const total = data?.total ?? 0
  const lastPage = Math.max(0, Math.ceil(total / PAGE_SIZE) - 1)

  const onRelease = async (userId: number) => {
    try {
      await releaseUser({ userId, params: { forget: false } })
      toast.success(t('settings.connectionLimit.violations.released', { defaultValue: 'Released' }))
      queryClient.invalidateQueries({ queryKey: ['/api/connection-limit/violations'] })
    } catch {
      toast.error(t('settings.connectionLimit.violations.releaseFailed', { defaultValue: 'Could not release them' }))
    }
  }

  return (
    <div className="w-full p-4 sm:py-6 lg:py-8">
      <Card>
        <CardHeader className="flex flex-col gap-3 border-b sm:flex-row sm:items-center sm:justify-between">
          <div className="space-y-1.5">
            <CardTitle className="flex items-center gap-2 text-base sm:text-lg">
              <ShieldAlert className="h-4 w-4" />
              {t('settings.connectionLimit.violations.title', { defaultValue: 'Violations' })}
            </CardTitle>
            <CardDescription className="text-xs sm:text-sm">
              {data?.enforcement_enabled
                ? t('settings.connectionLimit.violations.on', {
                    hours: data?.window_hours ?? 72,
                    defaultValue:
                      'Counted over the last {{hours}} hours. A user who behaves for that long starts again at the first step.',
                  })
                : t('settings.connectionLimit.violations.off', {
                    defaultValue: 'Acting on users is off, so nothing new will be added here.',
                  })}
            </CardDescription>
          </div>

          <div className="flex items-center gap-2">
            <Select
              value={scope}
              onValueChange={value => {
                setScope(value)
                setPage(0)
              }}
            >
              <SelectTrigger className="h-8 w-[170px] text-xs">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="all">{t('settings.connectionLimit.violations.all', { defaultValue: 'All of them' })}</SelectItem>
                <SelectItem value="active">{t('settings.connectionLimit.violations.inForce', { defaultValue: 'Still in force' })}</SelectItem>
              </SelectContent>
            </Select>
            <Button variant="outline" size="sm" className="h-8" onClick={() => void refetch()} disabled={isFetching}>
              <RefreshCw className={cn('h-3.5 w-3.5', isFetching && 'animate-spin')} />
            </Button>
          </div>
        </CardHeader>

        <CardContent className="p-0">
          {isLoading ? (
            <div className="space-y-2 p-4">
              <Skeleton className="h-10 w-full" />
              <Skeleton className="h-10 w-full" />
              <Skeleton className="h-10 w-full" />
            </div>
          ) : violations.length === 0 ? (
            <p className="text-muted-foreground p-6 text-center text-xs sm:text-sm">
              {t('settings.connectionLimit.violations.empty', { defaultValue: 'Nobody has been acted on.' })}
            </p>
          ) : (
            <div className="overflow-x-auto">
              <Table dir={isRTL ? 'rtl' : 'ltr'}>
                <TableHeader>
                  <TableRow>
                    <TableHead className={cn('w-44 text-xs', isRTL && 'text-right')}>{t('username')}</TableHead>
                    <TableHead className={cn('w-36 text-xs', isRTL && 'text-right')}>
                      {t('settings.connectionLimit.violations.action', { defaultValue: 'What was done' })}
                    </TableHead>
                    <TableHead className={cn('w-24 text-xs', isRTL && 'text-right')}>
                      {t('settings.connectionLimit.review.devices', { defaultValue: 'Devices' })}
                    </TableHead>
                    <TableHead className={cn('hidden text-xs md:table-cell', isRTL && 'text-right')}>
                      {t('settings.connectionLimit.violations.why', { defaultValue: 'Why it was acted on' })}
                    </TableHead>
                    <TableHead className={cn('hidden w-32 text-xs lg:table-cell', isRTL && 'text-right')}>
                      {t('settings.connectionLimit.violations.when', { defaultValue: 'When' })}
                    </TableHead>
                    <TableHead className="w-24 text-xs" />
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {violations.map(violation => (
                    <TableRow key={violation.id}>
                      <TableCell className="align-top">
                        <div className="flex flex-col gap-0.5">
                          <bdi className="truncate text-sm font-medium">{violation.username}</bdi>
                          <bdi className="text-muted-foreground/70 font-mono text-[10px]">#{violation.user_id}</bdi>
                        </div>
                      </TableCell>

                      <TableCell className="align-top">
                        <Badge variant="outline" className={cn('font-medium', stepTone(violation.disable_minutes))}>
                          {describeStep(violation.disable_minutes, t)}
                        </Badge>
                        <div className="text-muted-foreground mt-1 text-[10px]">
                          {violation.active
                            ? violation.restore_at
                              ? t('settings.connectionLimit.violations.liftsAt', {
                                  time: dayjs(violation.restore_at).format('HH:mm'),
                                  defaultValue: 'lifts at {{time}}',
                                })
                              : t('settings.connectionLimit.violations.inForce', { defaultValue: 'Still in force' })
                            : t('settings.connectionLimit.violations.over', { defaultValue: 'over' })}
                        </div>
                      </TableCell>

                      <TableCell className="align-top text-xs tabular-nums">
                        <bdi>
                          {violation.devices} / {violation.limit_applied}
                        </bdi>
                      </TableCell>

                      <TableCell className="text-muted-foreground hidden max-w-md align-top text-xs md:table-cell">
                        <div className="flex flex-col gap-1">
                          {((violation.reasons ?? []) as ConnectionReason[]).length > 0 && (
                            <ul className="text-foreground/80 space-y-0.5">
                              {((violation.reasons ?? []) as ConnectionReason[]).map((reason, index) => (
                                <li key={index}>
                                  <bdi>{renderReason(reason, t)}</bdi>
                                </li>
                              ))}
                            </ul>
                          )}
                          <bdi className="text-muted-foreground/70">
                            {(violation.observed_addresses ?? []).slice(0, 4).join(', ') || '—'}
                          </bdi>
                        </div>
                      </TableCell>

                      <TableCell className="text-muted-foreground hidden align-top text-xs whitespace-nowrap tabular-nums lg:table-cell">
                        <bdi>{dayjs(violation.created_at).format('MM-DD HH:mm')}</bdi>
                      </TableCell>

                      <TableCell className="align-top">
                        {violation.active && (
                          <Button
                            variant="ghost"
                            size="sm"
                            className="h-7 text-xs"
                            disabled={isReleasing}
                            onClick={() => void onRelease(violation.user_id)}
                          >
                            {t('settings.connectionLimit.violations.release', { defaultValue: 'Release' })}
                          </Button>
                        )}
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </div>
          )}

          {total > PAGE_SIZE && (
            <div className="flex items-center justify-between gap-2 border-t p-3">
              <span className="text-muted-foreground text-xs">
                {t('settings.connectionLimit.review.count', {
                  from: page * PAGE_SIZE + 1,
                  to: Math.min((page + 1) * PAGE_SIZE, total),
                  total,
                  defaultValue: '{{from}}–{{to}} of {{total}}',
                })}
              </span>
              <div className="flex gap-1">
                <Button variant="outline" size="sm" className="h-7 text-xs" disabled={page === 0} onClick={() => setPage(p => p - 1)}>
                  {t('previous', { defaultValue: 'Previous' })}
                </Button>
                <Button variant="outline" size="sm" className="h-7 text-xs" disabled={page >= lastPage} onClick={() => setPage(p => p + 1)}>
                  {t('next', { defaultValue: 'Next' })}
                </Button>
              </div>
            </div>
          )}
        </CardContent>
      </Card>
    </div>
  )
}
