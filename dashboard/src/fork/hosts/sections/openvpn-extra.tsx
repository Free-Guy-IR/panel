import { Accordion, AccordionContent, AccordionItem, AccordionTrigger } from '@/components/ui/accordion'
import { FormControl, FormField, FormItem, FormLabel, FormMessage } from '@/components/ui/form'
import { Input } from '@/components/ui/input'
import { Network } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { HostArrayInput } from '../host-array-input'
import type { ExtraSectionProps } from '../types'

export function OpenVPNExtraSection({ form, openSection, onOpenSectionChange }: ExtraSectionProps) {
  const { t } = useTranslation()

  return (
    <Accordion type="single" collapsible value={openSection} onValueChange={onOpenSectionChange} className="!mt-0 mb-6 flex w-full flex-col gap-y-6">
      <AccordionItem className="rounded-sm border px-4 [&_[data-state=closed]]:no-underline [&_[data-state=open]]:no-underline" value="openvpn_subscription_network">
        <AccordionTrigger>
          <div className="flex items-center gap-2">
            <Network className="h-4 w-4" />
            <span>{t('hostsDialog.openvpn.sectionNetworkOverrides', { defaultValue: 'Network Settings' })}</span>
          </div>
        </AccordionTrigger>
        <AccordionContent className="px-2 pb-4">
          <div className="space-y-3">
            <FormField
              control={form.control}
              name="openvpn_overrides.dns_servers"
              render={({ field }) => (
                <HostArrayInput
                  field={{ ...field, value: field.value ?? [] }}
                  placeholder="1.1.1.1"
                  label={t('hostsDialog.openvpn.dns', { defaultValue: 'DNS Servers' })}
                  infoContent={
                    <p className="text-muted-foreground text-[11px]">
                      {t('hostsDialog.openvpn.dnsHint', {
                        defaultValue: "Overrides this host's DNS for OpenVPN clients. Leave empty to use the core's default DNS.",
                      })}
                    </p>
                  }
                />
              )}
            />
            <FormField
              control={form.control}
              name="priority"
              render={({ field }) => (
                <FormItem>
                  <FormLabel>{t('hostsDialog.openvpn.priority', { defaultValue: 'Connection priority' })}</FormLabel>
                  <FormControl>
                    <Input
                      type="number"
                      min={0}
                      {...field}
                      value={field.value ?? 0}
                      onChange={e => {
                        const v = e.target.value
                        field.onChange(v === '' ? 0 : Number.parseInt(v, 10))
                      }}
                    />
                  </FormControl>
                  <p className="text-muted-foreground text-[11px]">
                    {t('hostsDialog.openvpn.priorityHint', {
                      defaultValue: 'Lower numbers are tried first when the client fails over between OpenVPN hosts in the downloaded file.',
                    })}
                  </p>
                  <FormMessage />
                </FormItem>
              )}
            />
          </div>
        </AccordionContent>
      </AccordionItem>
    </Accordion>
  )
}
