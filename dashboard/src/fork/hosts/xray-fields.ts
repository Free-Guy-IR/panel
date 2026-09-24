export const XRAY_HOST_FIELD_CLEARS = [
  ['host', []],
  ['sni', []],
  ['path', ''],
  ['http_headers', {}],
  ['security', 'inbound_default'],
  ['alpn', []],
  ['fingerprint', ''],
  ['cipher_suites', undefined],
  ['allowinsecure', false],
  ['random_user_agent', false],
  ['use_sni_as_host', false],
  ['vless_route', ''],
  ['ech_config_list', undefined],
  ['ech_query_strategy', undefined],
  ['mihomo_ech_config', undefined],
  ['mihomo_ech_query_server_name', undefined],
  ['sing_box_ech_config', undefined],
  ['sing_box_ech_query_server_name', undefined],
  ['pinned_peer_cert_sha256', undefined],
  ['verify_peer_cert_by_name', []],
  ['fragment_settings', undefined],
  ['noise_settings', undefined],
  ['mux_settings', undefined],
  ['transport_settings', undefined],
] as const

const CAMOUFLAGE_FIELDS: ReadonlySet<string> = new Set(['fragment_settings', 'noise_settings'])

const freshClearValue = (value: unknown) => {
  if (Array.isArray(value)) return []
  if (value !== null && typeof value === 'object') return {}
  return value
}

export function stripXrayHostFields(target: any, { keepCamouflage = false }: { keepCamouflage?: boolean } = {}) {
  for (const [name, value] of XRAY_HOST_FIELD_CLEARS) {
    if (keepCamouflage && CAMOUFLAGE_FIELDS.has(name)) continue
    target[name] = freshClearValue(value)
  }
  return target
}
