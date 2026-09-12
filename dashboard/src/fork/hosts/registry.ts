import type { UseFormReturn } from 'react-hook-form'
import type { HostFormValues } from '@/features/hosts/forms/host-form'
import { cleanNoExtraPayload, cleanOpenvpnPayload, cleanWireguardPayload, clearOpenvpnPayload, clearWireguardPayload } from './payload'
import { resolveHostMode, type HostProtocolMode } from './resolve-mode'
import { L2TPExtraSection, MTProtoExtraSection } from './sections/no-extra-settings'
import { OpenVPNExtraSection } from './sections/openvpn-extra'
import { WireGuardExtraSection } from './sections/wireguard-extra'
import type { HostProtocolEntry } from './types'

export const EMPTY_WIREGUARD_OVERRIDES: NonNullable<HostFormValues['wireguard_overrides']> = {
  allowed_ips: [],
  reserved: '',
  mtu: undefined,
  keepalive_seconds: undefined,
  dns: [],
}

export const EMPTY_OPENVPN_OVERRIDES: NonNullable<HostFormValues['openvpn_overrides']> = {
  dns_servers: [],
}

function initWireguardForm(form: UseFormReturn<HostFormValues>) {
  if (form.getValues('wireguard_overrides') == null) {
    form.setValue('wireguard_overrides', { ...EMPTY_WIREGUARD_OVERRIDES }, { shouldDirty: false })
  }
}

function initOpenvpnForm(form: UseFormReturn<HostFormValues>) {
  if (form.getValues('openvpn_overrides') == null) {
    form.setValue('openvpn_overrides', { ...EMPTY_OPENVPN_OVERRIDES }, { shouldDirty: false })
  }
}

function clearWireguardForm(form: UseFormReturn<HostFormValues>) {
  form.setValue('wireguard_overrides', undefined, { shouldDirty: false })
}

function clearOpenvpnForm(form: UseFormReturn<HostFormValues>) {
  form.setValue('openvpn_overrides', undefined, { shouldDirty: false })
}

export const HOST_PROTOCOL_REGISTRY: HostProtocolEntry[] = [
  {
    id: 'wireguard',
    mode: 'wireguard',
    ExtraSection: WireGuardExtraSection,
    stripXrayFields: true,
    initForm: initWireguardForm,
    clearForeignForm: clearWireguardForm,
    cleanPayload: cleanWireguardPayload,
    clearForeignPayload: clearWireguardPayload,
  },
  {
    id: 'openvpn',
    mode: 'openvpn',
    ExtraSection: OpenVPNExtraSection,
    stripXrayFields: true,
    initForm: initOpenvpnForm,
    clearForeignForm: clearOpenvpnForm,
    cleanPayload: cleanOpenvpnPayload,
    clearForeignPayload: clearOpenvpnPayload,
  },
  {
    id: 'mtproto',
    mode: 'mtproto',
    ExtraSection: MTProtoExtraSection,
    stripXrayFields: true,
    cleanPayload: cleanNoExtraPayload,
  },
  {
    id: 'l2tp',
    mode: 'l2tp',
    ExtraSection: L2TPExtraSection,
    stripXrayFields: true,
    cleanPayload: cleanNoExtraPayload,
  },
]

export function getHostProtocolEntry(protocol?: string | null) {
  return HOST_PROTOCOL_REGISTRY.find(entry => entry.id === protocol)
}

export function getHostProtocolEntryByMode(mode: HostProtocolMode) {
  return HOST_PROTOCOL_REGISTRY.find(entry => entry.mode === mode)
}

export function hasHostProtocolExtraSection(mode: HostProtocolMode): boolean {
  return getHostProtocolEntryByMode(mode) != null
}

export { resolveHostMode }
