import type { ReactNode } from 'react'
import type { UseFormReturn } from 'react-hook-form'
import type { HostFormValues } from '@/features/hosts/forms/host-form'
import { getHostProtocolEntryByMode } from './registry'
import type { HostProtocolMode } from './resolve-mode'

type HostProtocolExtraSectionProps = {
  mode: HostProtocolMode
  form: UseFormReturn<HostFormValues>
  wireguardOpenSection?: string
  onWireguardAccordionChange: (value: string) => void
  openvpnOpenSection?: string
  onOpenvpnAccordionChange: (value: string) => void
  renderCamouflageSection?: () => ReactNode
}

export function HostProtocolExtraSection({
  mode,
  form,
  wireguardOpenSection,
  onWireguardAccordionChange,
  openvpnOpenSection,
  onOpenvpnAccordionChange,
  renderCamouflageSection,
}: HostProtocolExtraSectionProps) {
  const entry = getHostProtocolEntryByMode(mode)
  if (!entry) {
    return null
  }
  const Extra = entry.ExtraSection
  const openSection = mode === 'wireguard' ? wireguardOpenSection : mode === 'openvpn' ? openvpnOpenSection : undefined
  const onOpenSectionChange = mode === 'wireguard' ? onWireguardAccordionChange : mode === 'openvpn' ? onOpenvpnAccordionChange : undefined
  return <Extra form={form} openSection={openSection} onOpenSectionChange={onOpenSectionChange} renderCamouflageSection={renderCamouflageSection} />
}
