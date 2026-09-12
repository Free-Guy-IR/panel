import { memo, type ReactNode } from 'react'
import { useTranslation } from 'react-i18next'
import { StringArrayPopoverInput } from '@/components/common/string-array-popover-input'
import { Button } from '@/components/ui/button'
import { FormItem, FormLabel, FormMessage } from '@/components/ui/form'
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/popover'
import useDirDetection from '@/hooks/use-dir-detection'
import { useIsMobile } from '@/hooks/use-mobile'
import { Info } from 'lucide-react'

type HostArrayInputProps = {
  field: any
  placeholder: string
  label: string
  infoContent?: ReactNode
  customVariablesTrigger?: ReactNode
}

export const HostArrayInput = memo<HostArrayInputProps>(({ field, placeholder, label, infoContent, customVariablesTrigger }) => {
  const { t } = useTranslation()
  const dir = useDirDetection()
  const isMobile = useIsMobile()

  return (
    <FormItem className="min-w-0">
      <div className="flex items-center gap-2">
        <FormLabel>{label}</FormLabel>
        {infoContent && (
          <Popover>
            <PopoverTrigger asChild>
              <Button type="button" variant="ghost" size="icon" className="h-4 w-4 p-0 hover:bg-transparent">
                <Info className="text-muted-foreground h-4 w-4" />
              </Button>
            </PopoverTrigger>
            <PopoverContent className="w-[min(90vw,20rem)] p-3 sm:w-80" side={isMobile ? 'bottom' : 'top'} align={dir === 'rtl' ? 'end' : 'start'} sideOffset={5}>
              <div className="space-y-1.5">
                <h4 className="mb-2 text-[11px] font-medium">{t('hostsDialog.variables.title')}</h4>
                <div className="max-h-[60vh] space-y-1 overflow-y-auto pr-1">{infoContent}</div>
              </div>
            </PopoverContent>
          </Popover>
        )}
        {customVariablesTrigger}
      </div>
      <StringArrayPopoverInput
        value={Array.isArray(field.value) ? field.value : []}
        onChange={(next: string[]) => field.onChange(next)}
        placeholder={placeholder}
        addPlaceholder={t('arrayInput.addPlaceholder')}
        addButtonLabel={t('arrayInput.addButton')}
        itemsLabel={t('arrayInput.items')}
        emptyMessage={t('arrayInput.noItems')}
        duplicateErrorMessage={t('arrayInput.duplicateError')}
        clickToEditTitle={t('arrayInput.clickToEdit')}
        editItemTitle={t('arrayInput.editItem')}
        removeItemTitle={t('arrayInput.removeItem')}
        saveEditTitle={t('arrayInput.saveEdit')}
        cancelEditTitle={t('arrayInput.cancelEdit')}
      />
      <FormMessage />
    </FormItem>
  )
})

HostArrayInput.displayName = 'HostArrayInput'
