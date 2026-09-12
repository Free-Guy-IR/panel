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
  ['pinned_peer_cert_sha256', undefined],
  ['verify_peer_cert_by_name', []],
  ['fragment_settings', undefined],
  ['noise_settings', undefined],
  ['mux_settings', undefined],
  ['transport_settings', undefined],
] as const

export function stripXrayHostFields(target: any) {
  target.host = []
  target.sni = []
  target.path = ''
  target.http_headers = {}
  target.security = 'inbound_default'
  target.alpn = []
  target.fingerprint = ''
  target.cipher_suites = undefined
  target.allowinsecure = false
  target.random_user_agent = false
  target.use_sni_as_host = false
  target.vless_route = ''
  target.ech_config_list = undefined
  target.ech_query_strategy = undefined
  target.pinned_peer_cert_sha256 = undefined
  target.verify_peer_cert_by_name = []
  target.fragment_settings = undefined
  target.noise_settings = undefined
  target.mux_settings = undefined
  target.transport_settings = undefined
  return target
}
