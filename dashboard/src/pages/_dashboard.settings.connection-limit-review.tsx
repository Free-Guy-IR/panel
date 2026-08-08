import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { Skeleton } from '@/components/ui/skeleton'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { cn } from '@/lib/utils'
import { renderReason, type ConnectionReason } from '@/features/users/components/connection-reasons'
import OverrideDialog from '@/features/users/components/connection-override-dialog'
import { useListConnectionStates } from '@/service/api'
import dayjs from 'dayjs'
import { RefreshCw, Users } from 'lucide-react'
import { useState } from 'react'
import { useTranslation } from 'react-i18next'

const PAGE_SIZE = 25

const verdictTone = (verdict: string | undefined) => {
  switch (verdict) {
    case 'over_limit':
      return 'border-destructive/40 bg-destructive/10 text-destructive'
    case 'at_limit':
      return 'border-amber-500/40 bg-amber-500/10 text-amber-600 dark:text-amber-400'
    default:
      return 'border-border bg-muted/50 text-muted-foreground'
  }
}

const verdictLabel = (verdict: string | undefined) => {
  switch (verdict) {
    case 'over_limit':
      return 'settings.connectionLimit.review.overLimit'
    case 'at_limit':
      return 'settings.connectionLimit.review.atLimit'
    default:
      return 'settings.connectionLimit.review.withinLimit'
  }
}

export default function ConnectionLimitReview() {
  const { t } = useTranslation()
  const [verdict, setVerdict] = useState<string>('over_limit')
  const [page, setPage] = useState(0)
  const [editing, setEditing] = useState<{ id: number; username?: string | null } | null>(null)

  const { data, isLoading, isFetching, refetch } = useListConnectionStates(
    {
      verdict: verdict === 'all' ? undefined : verdict,
      limit: PAGE_SIZE,
      offset: page * PAGE_SIZE,
    },
    { query: { refetchOnWindowFocus: false, placeholderData: previous => previous } },
  )

  const states = data?.states ?? []
  const total = data?.total ?? 0
  const lastPage = Math.max(0, Math.ceil(total / PAGE_SIZE) - 1)

  const onVerdictChange = (value: string) => {
    setVerdict(value)
    setPage(0)
  }

  return (
    <div className="w-full p-4 sm:py-6 lg:py-8">
      <Card>
        <CardHeader className="flex flex-col gap-3 border-b sm:flex-row sm:items-center sm:justify-between">
          <div className="space-y-1.5">
            <CardTitle className="flex items-center gap-2 text-base sm:text-lg">
              <Users className="h-4 w-4" />
              {t('settings.connectionLimit.review.title', { defaultValue: 'Devices per subscription' })}
            </CardTitle>
            <CardDescription className="text-xs sm:text-sm">
              {t('settings.connectionLimit.review.description', {
                defaultValue: 'What the last check saw. Nothing here has been acted on.',
              })}
            </CardDescription>
          </div>

          <div className="flex items-center gap-2">
            <Select value={verdict} onValueChange={onVerdictChange}>
              <SelectTrigger className="h-8 w-[170px] text-xs">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="over_limit">{t('settings.connectionLimit.review.overLimit', { defaultValue: 'Over the limit' })}</SelectItem>
                <SelectItem value="at_limit">{t('settings.connectionLimit.review.atLimit', { defaultValue: 'At the limit' })}</SelectItem>
                <SelectItem value="within_limit">{t('settings.connectionLimit.review.withinLimit', { defaultValue: 'Within the limit' })}</SelectItem>
                <SelectItem value="all">{t('settings.connectionLimit.review.all', { defaultValue: 'All users' })}</SelectItem>
              </SelectContent>
            </Select>
            <Button variant="outline" size="sm" className="h-8" onClick={() => void refetch()} disabled={isFetching}>
              <RefreshCw className={cn('h-3.5 w-3.5', isFetching && 'animate-spin')} />
            </Button>
          </div>
        </CardHeader>

        <CardContent className="p-0">
          {!data?.enabled && !isLoading && (
            <p className="text-muted-foreground p-4 text-xs sm:text-sm">
              {t('settings.connectionLimit.review.disabled', {
                defaultValue: 'Detection is off. Turn it on in the settings above to start collecting.',
              })}
            </p>
          )}

          {isLoading ? (
            <div className="space-y-2 p-4">
              <Skeleton className="h-10 w-full" />
              <Skeleton className="h-10 w-full" />
              <Skeleton className="h-10 w-full" />
            </div>
          ) : states.length === 0 ? (
            <p className="text-muted-foreground p-6 text-center text-xs sm:text-sm">
              {t('settings.connectionLimit.review.empty', { defaultValue: 'Nothing recorded for this filter yet.' })}
            </p>
          ) : (
            <div className="overflow-x-auto">
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead className="text-xs">{t('username')}</TableHead>
                    <TableHead className="text-xs">{t('settings.connectionLimit.review.devices', { defaultValue: 'Devices' })}</TableHead>
                    <TableHead className="hidden text-xs md:table-cell">
                      {t('settings.connectionLimit.review.evidence', { defaultValue: 'What was seen' })}
                    </TableHead>
                    <TableHead className="hidden text-xs sm:table-cell">
                      {t('settings.connectionLimit.review.streak', { defaultValue: 'Cycles' })}
                    </TableHead>
                    <TableHead className="hidden text-xs lg:table-cell">
                      {t('settings.connectionLimit.review.checked', { defaultValue: 'Checked' })}
                    </TableHead>
                    <TableHead className="text-xs" />
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {states.map(state => (
                    <TableRow key={state.user_id}>
                      <TableCell className="align-top">
                        <div className="flex flex-col gap-0.5">
                          <span className="text-sm font-medium">{state.username}</span>
                          <span className="text-muted-foreground/70 font-mono text-[10px]">#{state.user_id}</span>
                        </div>
                      </TableCell>

                      <TableCell className="align-top">
                        <Badge variant="outline" className={cn('gap-1 font-medium', verdictTone(state.verdict))}>
                          <Users className="h-3 w-3" />
                          {state.devices}
                        </Badge>
                        <div className="text-muted-foreground mt-1 text-[10px]">{t(verdictLabel(state.verdict))}</div>
                      </TableCell>

                      <TableCell className="hidden max-w-md align-top md:table-cell">
                        <ul className="space-y-0.5">
                          {((state.reasons ?? []) as ConnectionReason[]).slice(1).map((reason, index) => (
                            <li key={index} className="text-muted-foreground text-xs leading-relaxed" dir="auto">
                              {renderReason(reason, t)}
                            </li>
                          ))}
                        </ul>
                      </TableCell>

                      <TableCell className="hidden align-top text-xs sm:table-cell">{state.streak}</TableCell>

                      <TableCell className="text-muted-foreground hidden align-top text-xs lg:table-cell" dir="ltr">
                        {state.checked_at ? dayjs(state.checked_at).format('HH:mm') : '—'}
                      </TableCell>

                      <TableCell className="align-top">
                        <Button
                          variant="ghost"
                          size="sm"
                          className="h-7 text-xs"
                          onClick={() => setEditing({ id: state.user_id, username: state.username })}
                        >
                          {t('settings.connectionLimit.review.setAllowance', { defaultValue: 'Allowance' })}
                        </Button>
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

      <OverrideDialog
        userId={editing?.id ?? null}
        username={editing?.username}
        defaultLimit={data?.device_limit ?? 2}
        open={editing !== null}
        onOpenChange={open => !open && setEditing(null)}
      />
    </div>
  )
}
