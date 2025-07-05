import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import { Calendar } from '@/components/ui/calendar'
import { Calendar as PersianCalendar } from '@/components/ui/persian-calendar'
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/popover'
import { AlertDialog, AlertDialogAction, AlertDialogCancel, AlertDialogContent, AlertDialogDescription, AlertDialogFooter, AlertDialogHeader, AlertDialogTitle, AlertDialogTrigger } from '@/components/ui/alert-dialog'
import { Alert, AlertDescription } from '@/components/ui/alert'
import { CalendarIcon, Database, RotateCcw, AlertTriangle, Trash2 } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { useState } from 'react'
import { cn } from '@/lib/utils'
import { format } from 'date-fns'
import { toast } from 'sonner'
import { $fetch } from 'ofetch'
import useDirDetection from '@/hooks/use-dir-detection'

export default function CleanupSettings() {
  const { t, i18n } = useTranslation()
  const dir = useDirDetection()
  const isPersianLocale = i18n.language === 'fa'
  const [expiredAfter, setExpiredAfter] = useState<Date | undefined>()
  const [expiredBefore, setExpiredBefore] = useState<Date | undefined>()
  const [isDeleting, setIsDeleting] = useState(false)
  const [isResetting, setIsResetting] = useState(false)

  const handleDeleteExpired = async () => {
    if (!expiredAfter && !expiredBefore) {
      toast.error(t('settings.cleanup.expiredUsers.noDateSelected'))
      return
    }

    setIsDeleting(true)
    try {
      const params = new URLSearchParams()
      if (expiredAfter) params.append('expired_after', expiredAfter.toISOString())
      if (expiredBefore) params.append('expired_before', expiredBefore.toISOString())

      const response = await $fetch(`/api/users/expired?${params.toString()}`, {
        method: 'DELETE'
      })

      const count = response?.deleted_count || 0
      toast.success(t('settings.cleanup.expiredUsers.deleteSuccess', { count }))
    } catch (error: any) {
      toast.error(t('settings.cleanup.expiredUsers.deleteFailed'))
    } finally {
      setIsDeleting(false)
    }
  }

  const formatDate = (date: Date) => {
    if (isPersianLocale) {
      return new Intl.DateTimeFormat('fa-IR', {
        year: 'numeric',
        month: '2-digit',
        day: '2-digit',
      }).format(date)
    }
    return format(date, 'PPP')
  }

  const handleResetUsage = async () => {
    setIsResetting(true)
    try {
      await $fetch('/api/users/reset', {
        method: 'POST'
      })
      toast.success(t('settings.cleanup.resetUsage.resetSuccess'))
    } catch (error: any) {
      toast.error(t('settings.cleanup.resetUsage.resetFailed'))
    } finally {
      setIsResetting(false)
    }
  }

  return (
    <div className="p-6 space-y-6">
      {/* Delete Expired Users Section */}
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <Database className="h-5 w-5" />
            {t('settings.cleanup.expiredUsers.title')}
          </CardTitle>
          <CardDescription>
            {t('settings.cleanup.expiredUsers.description')}
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-4">
          <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
            <div className="space-y-2">
              <label className="text-sm font-medium">
                {t('settings.cleanup.expiredUsers.expiredAfter')}
              </label>
              <Popover>
                <PopoverTrigger asChild>
                  <Button
                    variant="outline"
                    className={cn(
                      'w-full justify-start text-left font-normal',
                      !expiredAfter && 'text-muted-foreground'
                    )}
                  >
                    <CalendarIcon className="mr-2 h-4 w-4" />
                    {expiredAfter ? formatDate(expiredAfter) : t('settings.cleanup.expiredUsers.expiredAfterPlaceholder')}
                  </Button>
                </PopoverTrigger>
                <PopoverContent className="w-auto p-0">
                  {isPersianLocale ? (
                    <PersianCalendar
                      mode="single"
                      selected={expiredAfter}
                      onSelect={setExpiredAfter}
                      disabled={(date) => date > new Date() || date < new Date("1900-01-01")}
                      captionLayout="dropdown"
                      formatters={{
                        formatMonthDropdown: date => date.toLocaleString('fa-IR', { month: 'short' }),
                      }}
                    />
                  ) : (
                    <Calendar
                      mode="single"
                      selected={expiredAfter}
                      onSelect={setExpiredAfter}
                      disabled={(date) => date > new Date() || date < new Date("1900-01-01")}
                      captionLayout="dropdown"
                      formatters={{
                        formatMonthDropdown: date => date.toLocaleString('default', { month: 'short' }),
                      }}
                    />
                  )}
                </PopoverContent>
              </Popover>
            </div>

            <div className="space-y-2">
              <label className="text-sm font-medium">
                {t('settings.cleanup.expiredUsers.expiredBefore')}
              </label>
              <Popover>
                <PopoverTrigger asChild>
                  <Button
                    variant="outline"
                    className={cn(
                      'w-full justify-start text-left font-normal',
                      !expiredBefore && 'text-muted-foreground'
                    )}
                  >
                    <CalendarIcon className="mr-2 h-4 w-4" />
                    {expiredBefore ? formatDate(expiredBefore) : t('settings.cleanup.expiredUsers.expiredBeforePlaceholder')}
                  </Button>
                </PopoverTrigger>
                <PopoverContent className="w-auto p-0">
                  {isPersianLocale ? (
                    <PersianCalendar
                      mode="single"
                      selected={expiredBefore}
                      onSelect={setExpiredBefore}
                      disabled={(date) => date > new Date() || date < new Date("1900-01-01")}
                      captionLayout="dropdown"
                      formatters={{
                        formatMonthDropdown: date => date.toLocaleString('fa-IR', { month: 'short' }),
                      }}
                    />
                  ) : (
                    <Calendar
                      mode="single"
                      selected={expiredBefore}
                      onSelect={setExpiredBefore}
                      disabled={(date) => date > new Date() || date < new Date("1900-01-01")}
                      captionLayout="dropdown"
                      formatters={{
                        formatMonthDropdown: date => date.toLocaleString('default', { month: 'short' }),
                      }}
                    />
                  )}
                </PopoverContent>
              </Popover>
            </div>
          </div>

          <div className="text-sm text-muted-foreground">
            {t('settings.cleanup.expiredUsers.selectDateRange')}
          </div>

          <AlertDialog>
            <AlertDialogTrigger asChild>
              <Button 
                variant="destructive" 
                disabled={(!expiredAfter && !expiredBefore) || isDeleting}
                className="w-full"
              >
                <Trash2 className="mr-2 h-4 w-4" />
                {isDeleting ? t('settings.cleanup.expiredUsers.deleting') : t('settings.cleanup.expiredUsers.deleteExpired')}
              </Button>
            </AlertDialogTrigger>
            <AlertDialogContent>
              <AlertDialogHeader>
                <AlertDialogTitle className="flex items-center gap-2">
                  <AlertTriangle className="h-5 w-5 text-destructive" />
                  {t('settings.cleanup.expiredUsers.confirmDelete')}
                </AlertDialogTitle>
                <AlertDialogDescription>
                  {t('settings.cleanup.expiredUsers.confirmDeleteMessage')}
                </AlertDialogDescription>
              </AlertDialogHeader>
              <AlertDialogFooter>
                <AlertDialogCancel>{t('cancel')}</AlertDialogCancel>
                <AlertDialogAction
                  onClick={handleDeleteExpired}
                  className="bg-destructive text-destructive-foreground hover:bg-destructive/90"
                >
                  {t('settings.cleanup.expiredUsers.deleteExpired')}
                </AlertDialogAction>
              </AlertDialogFooter>
            </AlertDialogContent>
          </AlertDialog>
        </CardContent>
      </Card>

      {/* Reset Usage Section */}
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <RotateCcw className="h-5 w-5" />
            {t('settings.cleanup.resetUsage.title')}
          </CardTitle>
          <CardDescription>
            {t('settings.cleanup.resetUsage.description')}
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-4">
          <Alert>
            <AlertTriangle className="h-4 w-4" />
            <AlertDescription>
              {t('settings.cleanup.resetUsage.warning')}
            </AlertDescription>
          </Alert>

          <AlertDialog>
            <AlertDialogTrigger asChild>
              <Button variant="destructive" disabled={isResetting} className="w-full">
                <RotateCcw className="mr-2 h-4 w-4" />
                {isResetting ? t('settings.cleanup.resetUsage.resetting') : t('settings.cleanup.resetUsage.resetAll')}
              </Button>
            </AlertDialogTrigger>
            <AlertDialogContent dir={dir}>
              <AlertDialogHeader>
                <AlertDialogTitle className="flex items-center gap-2">
                  {t('settings.cleanup.resetUsage.confirmReset')}
                </AlertDialogTitle>
                <AlertDialogDescription className={cn(dir === 'rtl' ? 'text-right' : 'text-left')}>
                  {t('settings.cleanup.resetUsage.confirmResetMessage')}
                </AlertDialogDescription>
              </AlertDialogHeader>
              <AlertDialogFooter className={cn("gap-2",dir === 'rtl' ? 'flex-row-reverse' : '')}>
                <AlertDialogCancel>{t('cancel')}</AlertDialogCancel>
                <AlertDialogAction
                  onClick={handleResetUsage}
                  disabled={isResetting}
                  className="bg-destructive text-destructive-foreground hover:bg-destructive/90 !m-0"
                >
                  {t('settings.cleanup.resetUsage.resetAll')}
                </AlertDialogAction>
              </AlertDialogFooter>
            </AlertDialogContent>
          </AlertDialog>
        </CardContent>
      </Card>
    </div>
  )
} 