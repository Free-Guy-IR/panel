import { Dialog, DialogContent, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Form, FormControl, FormField, FormItem, FormLabel, FormMessage } from '@/components/ui/form'
import { Input } from '@/components/ui/input'
import { Switch } from '@/components/ui/switch'
import { useTranslation } from 'react-i18next'
import { UseFormReturn } from 'react-hook-form'
import { Button } from '@/components/ui/button'

interface AdminFormValues {
  username: string
  password: string
  is_sudo: boolean
}

interface AdminModalProps {
  isDialogOpen: boolean
  onOpenChange: (open: boolean) => void
  onSubmit: (data: AdminFormValues) => Promise<void>
  editingAdmin?: boolean
  form: UseFormReturn<AdminFormValues>
}

export default function AdminModal({
  isDialogOpen,
  onOpenChange,
  onSubmit,
  editingAdmin,
  form
}: AdminModalProps) {
  const { t } = useTranslation()

  return (
    <Dialog open={isDialogOpen} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>{editingAdmin ? t('admins.editAdmin') : t('admins.createAdmin')}</DialogTitle>
        </DialogHeader>
        <Form {...form}>
          <form onSubmit={form.handleSubmit(onSubmit)} className="space-y-4">
            <FormField
              control={form.control}
              name="username"
              render={({ field }) => (
                <FormItem>
                  <FormLabel>{t('admins.username')}</FormLabel>
                  <FormControl>
                    <Input placeholder={t('admins.enterUsername')} {...field} />
                  </FormControl>
                  <FormMessage />
                </FormItem>
              )}
            />
            <FormField
              control={form.control}
              name="password"
              render={({ field }) => (
                <FormItem>
                  <FormLabel>{t('admins.password')}</FormLabel>
                  <FormControl>
                    <Input type="password" placeholder={t('admins.enterPassword')} {...field} />
                  </FormControl>
                  <FormMessage />
                </FormItem>
              )}
            />
            <FormField
              control={form.control}
              name="is_sudo"
              render={({ field }) => (
                <FormItem className="flex flex-row items-center justify-between rounded-lg border p-4">
                  <div className="space-y-0.5">
                    <FormLabel className="text-base">{t('admins.sudo')}</FormLabel>
                  </div>
                  <FormControl>
                    <Switch
                      checked={field.value}
                      onCheckedChange={field.onChange}
                    />
                  </FormControl>
                </FormItem>
              )}
            />
            <div className="flex justify-end gap-2">
              <Button type="button" variant="outline" onClick={() => onOpenChange(false)}>
                {t('cancel')}
              </Button>
              <Button type="submit">
                {editingAdmin ? t('edit') : t('create')}
              </Button>
            </div>
          </form>
        </Form>
      </DialogContent>
    </Dialog>
  )
} 