import type { ReactNode } from 'react'
import type { UseFormReturn } from 'react-hook-form'
import type { HostFormValues } from '@/features/hosts/forms/host-form'
import type { ProtocolHostPayload } from './payload'
import type { ForkHostProtocol, HostProtocolMode } from './resolve-mode'

export type { ForkHostProtocol, HostProtocolMode }

export type ExtraSectionProps = {
  form: UseFormReturn<HostFormValues>
  openSection?: string
  onOpenSectionChange?: (value: string) => void
  renderCamouflageSection?: () => ReactNode
}

export type HostProtocolEntry = {
  id: ForkHostProtocol
  mode: HostProtocolMode
  ExtraSection: (props: ExtraSectionProps) => ReactNode
  stripXrayFields: boolean
  initForm?: (form: UseFormReturn<HostFormValues>) => void
  clearForeignForm?: (form: UseFormReturn<HostFormValues>) => void
  cleanPayload: (payload: ProtocolHostPayload) => void
  clearForeignPayload?: (payload: ProtocolHostPayload) => void
}
