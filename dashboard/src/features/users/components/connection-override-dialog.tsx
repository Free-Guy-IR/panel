import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Switch } from '@/components/ui/switch'
import { useClearOverride, useSetOverride } from '@/service/api'
import { useQueryClient } from '@tanstack/react-query'
import { useEffect, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { toast } from 'sonner'

interface Props {
  userId: number | null
  username?: string | null
  defaultLimit: number
  open: boolean
  onOpenChange: (open: boolean) => void
}

/**
 * Set one user's device allowance, or exempt them from checking.
 *
 * An empty limit means "use the default" rather than zero - zero would read as
 * "no devices allowed", which is not something anyone wants to set by leaving
 * a box blank.
 */
export default function OverrideDialog({ userId, username, defaultLimit, open, onOpenChange }: Props) {
  const { t } = useTranslation()
  const queryClient = useQueryClient()
  const [limit, setLimit] = useState<string>('')
  const [exempt, setExempt] = useState(false)
  const [note, setNote] = useState('')

  useEffect(() => {
    if (open) {
      setLimit('')
      setExempt(false)
      setNote('')
    }
  }, [open, userId])

  const { mutateAsync: save, isPending: isSaving } = useSetOverride()
  const { mutateAsync: clear, isPending: isClearing } = useClearOverride()

  const invalidate = () => {
    queryClient.invalidateQueries({ queryKey: ['/api/connection-limit/overrides'] })
    queryClient.invalidateQueries({ queryKey: ['/api/connection-limit/states'] })
  }

  const onSave = async () => {
    if (userId == null) return
    const parsed = limit.trim() === '' ? null : Number(limit)
    if (parsed !== null && (!Number.isFinite(parsed) || parsed < 1)) {
      toast.error(t('settings.connectionLimit.override.invalid', { defaultValue: 'Enter a number of 1 or more, or leave it empty' }))
      return
    }
    try {
      await save({ userId, data: { ip_limit: parsed, exempt, note: note.trim() || null } })
      toast.success(t('settings.connectionLimit.override.saved', { defaultValue: 'Saved' }))
      invalidate()
      onOpenChange(false)
    } catch {
      toast.error(t('settings.connectionLimit.override.failed', { defaultValue: 'Could not save' }))
    }
  }

  const onClear = async () => {
    if (userId == null) return
    try {
      await clear({ userId })
      toast.success(t('settings.connectionLimit.override.cleared', { defaultValue: 'Back to the default' }))
      invalidate()
      onOpenChange(false)
    } catch {
      toast.error(t('settings.connectionLimit.override.failed', { defaultValue: 'Could not save' }))
    }
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle>{t('settings.connectionLimit.override.title', { defaultValue: 'Device allowance' })}</DialogTitle>
          <DialogDescription>
            {username} — {t('settings.connectionLimit.override.description', {
              limit: defaultLimit,
              defaultValue: 'Leave empty to use the default of {{limit}}.',
            })}
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-4 py-2">
          <div className="space-y-2">
            <Label className="text-sm">{t('settings.connectionLimit.override.limit', { defaultValue: 'Devices allowed' })}</Label>
            <Input
              inputMode="numeric"
              placeholder={String(defaultLimit)}
              value={limit}
              disabled={exempt}
              onChange={event => setLimit(event.target.value.replace(/[^0-9]/g, ''))}
            />
          </div>

          <div className="bg-card flex items-center justify-between gap-4 rounded-md border p-3">
            <div className="min-w-0 flex-1 space-y-1">
              <Label className="cursor-pointer text-sm font-medium">
                {t('settings.connectionLimit.override.exempt', { defaultValue: 'Never check this user' })}
              </Label>
              <p className="text-muted-foreground text-xs leading-relaxed">
                {t('settings.connectionLimit.override.exemptHelp', {
                  defaultValue: 'They are skipped entirely and no count is recorded.',
                })}
              </p>
            </div>
            <Switch checked={exempt} onCheckedChange={setExempt} className="shrink-0" />
          </div>

          <div className="space-y-2">
            <Label className="text-sm">{t('settings.connectionLimit.override.note', { defaultValue: 'Note' })}</Label>
            <Input placeholder={t('settings.connectionLimit.override.notePlaceholder', { defaultValue: 'Why, for the next person looking' })} value={note} onChange={event => setNote(event.target.value)} />
          </div>
        </div>

        <DialogFooter className="gap-2 sm:gap-2">
          <Button variant="ghost" onClick={onClear} disabled={isClearing || isSaving} className="sm:mr-auto">
            {t('settings.connectionLimit.override.useDefault', { defaultValue: 'Use the default' })}
          </Button>
          <Button variant="outline" onClick={() => onOpenChange(false)} disabled={isSaving || isClearing}>
            {t('cancel', { defaultValue: 'Cancel' })}
          </Button>
          <Button onClick={onSave} disabled={isSaving || isClearing}>
            {t('save', { defaultValue: 'Save' })}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
