import { useEffect } from 'react'
import type { UseFormReturn } from 'react-hook-form'
import type { HostFormValues } from '@/features/hosts/forms/host-form'
import { getHostProtocolEntry, HOST_PROTOCOL_REGISTRY } from './registry'
import { XRAY_HOST_FIELD_CLEARS } from './xray-fields'

function clearXrayHostFormFields(form: UseFormReturn<HostFormValues>) {
  for (const [name, value] of XRAY_HOST_FIELD_CLEARS) {
    form.setValue(name, value as never, { shouldDirty: true })
  }
}

export function useHostProtocolFormEffects({
  form,
  protocol,
  editingHost,
  selectedInboundTag,
}: {
  form: UseFormReturn<HostFormValues>
  protocol?: string
  editingHost?: boolean
  selectedInboundTag?: string
}) {
  const entry = getHostProtocolEntry(protocol)

  useEffect(() => {
    if (!entry?.stripXrayFields || editingHost) {
      return
    }
    clearXrayHostFormFields(form)
  }, [form, protocol, selectedInboundTag, editingHost, entry])

  useEffect(() => {
    for (const item of HOST_PROTOCOL_REGISTRY) {
      if (item.id === protocol) {
        if (!editingHost) {
          item.initForm?.(form)
        }
      } else {
        item.clearForeignForm?.(form)
      }
    }
  }, [form, protocol, selectedInboundTag, editingHost])
}
