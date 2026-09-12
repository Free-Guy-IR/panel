import { Accordion, AccordionContent, AccordionItem, AccordionTrigger } from '@/components/ui/accordion'
import { FormControl, FormField, FormItem, FormLabel, FormMessage } from '@/components/ui/form'
import { Input } from '@/components/ui/input'
import { Network } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { HostArrayInput } from '../host-array-input'
import type { ExtraSectionProps } from '../types'

export function WireGuardExtraSection({ form, openSection, onOpenSectionChange, renderCamouflageSection }: ExtraSectionProps) {
  const { t } = useTranslation()

  return (
    <Accordion type="single" collapsible value={openSection} onValueChange={onOpenSectionChange} className="!mt-0 mb-6 flex w-full flex-col gap-y-6">
      <AccordionItem className="rounded-sm border px-4 [&_[data-state=closed]]:no-underline [&_[data-state=open]]:no-underline" value="wg_subscription_network">
        <AccordionTrigger>
          <div className="flex items-center gap-2">
            <Network className="h-4 w-4" />
            <span>{t('hostsDialog.wireguard.sectionNetworkOverrides')}</span>
          </div>
        </AccordionTrigger>
        <AccordionContent className="px-2 pb-4">
          <div className="space-y-3">
            <FormField
              control={form.control}
              name="wireguard_overrides.allowed_ips"
              render={({ field }) => (
                <HostArrayInput
                  field={{ ...field, value: field.value ?? [] }}
                  placeholder="0.0.0.0/0"
                  label={t('hostsDialog.wireguard.allowedIps')}
                  infoContent={<p className="text-muted-foreground text-[11px]">{t('hostsDialog.wireguard.allowedIpsHint')}</p>}
                />
              )}
            />
            <div className="grid gap-3 sm:grid-cols-2">
              <FormField
                control={form.control}
                name="wireguard_overrides.mtu"
                render={({ field }) => (
                  <FormItem>
                    <FormLabel>{t('hostsDialog.wireguard.mtu')}</FormLabel>
                    <FormControl>
                      <Input
                        type="number"
                        placeholder="1280"
                        min={576}
                        max={9000}
                        {...field}
                        value={field.value ?? ''}
                        onChange={e => {
                          const v = e.target.value
                          field.onChange(v === '' ? undefined : Number.parseInt(v, 10))
                        }}
                      />
                    </FormControl>
                    <FormMessage />
                  </FormItem>
                )}
              />
              <FormField
                control={form.control}
                name="wireguard_overrides.keepalive_seconds"
                render={({ field }) => (
                  <FormItem>
                    <FormLabel>{t('hostsDialog.wireguard.keepalive')}</FormLabel>
                    <FormControl>
                      <Input
                        type="number"
                        placeholder={t('hostsDialog.wireguard.keepalivePlaceholder')}
                        min={0}
                        max={86400}
                        {...field}
                        value={field.value ?? ''}
                        onChange={e => {
                          const v = e.target.value
                          field.onChange(v === '' ? undefined : Number.parseInt(v, 10))
                        }}
                      />
                    </FormControl>
                    <FormMessage />
                  </FormItem>
                )}
              />
            </div>
            <FormField
              control={form.control}
              name="wireguard_overrides.reserved"
              render={({ field }) => (
                <FormItem>
                  <FormLabel>{t('hostsDialog.wireguard.reserved')}</FormLabel>
                  <FormControl>
                    <Input className="font-mono text-xs" dir="ltr" placeholder="0,0,0" {...field} />
                  </FormControl>
                  <FormMessage />
                </FormItem>
              )}
            />
            <FormField
              control={form.control}
              name="wireguard_overrides.dns"
              render={({ field }) => (
                <HostArrayInput
                  field={{ ...field, value: field.value ?? [] }}
                  placeholder="1.1.1.1"
                  label={t('hostsDialog.wireguard.dns')}
                  infoContent={<p className="text-muted-foreground text-[11px]">{t('hostsDialog.wireguard.dnsHint')}</p>}
                />
              )}
            />
          </div>
        </AccordionContent>
      </AccordionItem>
      {renderCamouflageSection?.()}
    </Accordion>
  )
}
