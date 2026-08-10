import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Skeleton } from '@/components/ui/skeleton'
import { useResolveUserAddresses } from '@/service/api'
import { useTranslation } from 'react-i18next'

interface Props {
  userId: number | null
  username?: string | null
  open: boolean
  onOpenChange: (open: boolean) => void
}

/**
 * The addresses a user was last seen on, each with the provider behind it.
 *
 * The lookup goes to a third-party service, so it runs only here - when an
 * admin opens one user's case - never in the checking loop over thousands of
 * users. When the lookup is switched off the addresses still show, just
 * without a provider name, and the dialog says why.
 */
export default function ConnectionAddressesDialog({ userId, username, open, onOpenChange }: Props) {
  const { t } = useTranslation()

  const { data, isLoading } = useResolveUserAddresses(userId as number, {
    query: { enabled: open && userId != null },
  })

  const addresses = data?.addresses ?? []

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-lg">
        <DialogHeader>
          <DialogTitle>{t('settings.connectionLimit.addresses.title', { defaultValue: 'Addresses and providers' })}</DialogTitle>
          <DialogDescription dir="auto">
            {username} —{' '}
            {data && data.enabled === false
              ? t('settings.connectionLimit.addresses.lookupOff', {
                  defaultValue: 'Provider lookup is off, so only the addresses are shown. Turn it on in the settings above.',
                })
              : t('settings.connectionLimit.addresses.description', {
                  defaultValue: 'The networks this user was last seen on, and the provider behind each.',
                })}
          </DialogDescription>
        </DialogHeader>

        <div className="max-h-[60vh] space-y-2 overflow-y-auto py-1">
          {isLoading ? (
            <>
              <Skeleton className="h-10 w-full" />
              <Skeleton className="h-10 w-full" />
              <Skeleton className="h-10 w-full" />
            </>
          ) : addresses.length === 0 ? (
            <p className="text-muted-foreground py-4 text-center text-xs sm:text-sm">
              {t('settings.connectionLimit.addresses.empty', { defaultValue: 'No addresses recorded for this user.' })}
            </p>
          ) : (
            addresses.map((item, index) => (
              <div key={index} className="bg-card flex items-center justify-between gap-3 rounded-md border p-2.5">
                <span className="text-foreground font-mono text-xs" dir="ltr">
                  {item.address}
                </span>
                <div className="min-w-0 text-right">
                  <div className="truncate text-xs font-medium" dir="auto">
                    {item.provider || t('settings.connectionLimit.addresses.unknown', { defaultValue: 'unknown' })}
                  </div>
                  {item.country && <div className="text-muted-foreground text-[10px]" dir="auto">{item.country}</div>}
                </div>
              </div>
            ))
          )}
        </div>
      </DialogContent>
    </Dialog>
  )
}
