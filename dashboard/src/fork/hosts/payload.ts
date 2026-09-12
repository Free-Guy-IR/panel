import { stripXrayHostFields } from './xray-fields'

export type WireGuardOverridePayload = {
  allowed_ips?: string[]
  mtu?: number
  reserved?: string
  keepalive_seconds?: number
  dns?: string[]
}

export type OpenVPNOverridePayload = {
  dns_servers?: string[]
}

export type ProtocolHostPayload = {
  host?: unknown
  sni?: unknown
  path?: unknown
  http_headers?: unknown
  security?: unknown
  alpn?: unknown
  fingerprint?: unknown
  cipher_suites?: unknown
  allowinsecure?: unknown
  random_user_agent?: unknown
  use_sni_as_host?: unknown
  vless_route?: unknown
  ech_config_list?: unknown
  ech_query_strategy?: unknown
  pinned_peer_cert_sha256?: unknown
  verify_peer_cert_by_name?: unknown
  fragment_settings?: unknown
  noise_settings?: unknown
  mux_settings?: unknown
  transport_settings?: unknown
  wireguard_overrides?: WireGuardOverridePayload
  openvpn_overrides?: OpenVPNOverridePayload
}

export function normalizeWireguardOverrides(wg?: WireGuardOverridePayload): WireGuardOverridePayload | undefined {
  if (!wg) {
    return undefined
  }
  const next: WireGuardOverridePayload = {}
  if (wg.allowed_ips?.length) next.allowed_ips = wg.allowed_ips
  if (wg.mtu != null && !Number.isNaN(Number(wg.mtu))) next.mtu = Number(wg.mtu)
  if (wg.reserved?.trim()) next.reserved = wg.reserved.trim()
  if (wg.keepalive_seconds != null && !Number.isNaN(Number(wg.keepalive_seconds))) {
    next.keepalive_seconds = Number(wg.keepalive_seconds)
  }
  if (wg.dns?.length) next.dns = wg.dns
  return Object.keys(next).length > 0 ? next : undefined
}

export function normalizeOpenvpnOverrides(ov?: OpenVPNOverridePayload): OpenVPNOverridePayload | undefined {
  if (!ov) {
    return undefined
  }
  const next: OpenVPNOverridePayload = {}
  if (ov.dns_servers?.length) next.dns_servers = ov.dns_servers
  return Object.keys(next).length > 0 ? next : undefined
}

export function cleanWireguardPayload(payload: ProtocolHostPayload): void {
  stripXrayHostFields(payload)
  if (payload.wireguard_overrides) {
    payload.wireguard_overrides = normalizeWireguardOverrides(payload.wireguard_overrides)
  }
}

export function cleanOpenvpnPayload(payload: ProtocolHostPayload): void {
  stripXrayHostFields(payload)
  if (payload.openvpn_overrides) {
    payload.openvpn_overrides = normalizeOpenvpnOverrides(payload.openvpn_overrides)
  }
}

export function cleanNoExtraPayload(payload: ProtocolHostPayload): void {
  stripXrayHostFields(payload)
}

export function clearWireguardPayload(payload: ProtocolHostPayload): void {
  payload.wireguard_overrides = undefined
}

export function clearOpenvpnPayload(payload: ProtocolHostPayload): void {
  payload.openvpn_overrides = undefined
}

export function applyHostProtocolPayload<T extends ProtocolHostPayload>(payload: T, protocol?: string | null): T {
  if (protocol === 'wireguard') {
    cleanWireguardPayload(payload)
    clearOpenvpnPayload(payload)
    return payload
  }
  if (protocol === 'openvpn') {
    cleanOpenvpnPayload(payload)
    clearWireguardPayload(payload)
    return payload
  }
  if (protocol === 'mtproto' || protocol === 'l2tp') {
    cleanNoExtraPayload(payload)
    clearWireguardPayload(payload)
    clearOpenvpnPayload(payload)
    return payload
  }
  clearWireguardPayload(payload)
  clearOpenvpnPayload(payload)
  return payload
}
