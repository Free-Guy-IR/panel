import { expect, test } from 'bun:test'
import { stripXrayHostFields } from '../../dashboard/src/fork/hosts/xray-fields.ts'

test('stripXrayHostFields clears transport and security fields', () => {
  const payload = stripXrayHostFields({
    remark: 'keep',
    host: ['a.example'],
    sni: ['sni.example'],
    path: '/x',
    http_headers: { X: '1' },
    security: 'tls',
    alpn: ['h2'],
    fingerprint: 'chrome',
    cipher_suites: 'TLS_AES_128_GCM_SHA256',
    allowinsecure: true,
    random_user_agent: true,
    use_sni_as_host: true,
    vless_route: '1',
    ech_config_list: 'ech',
    ech_query_strategy: 'full',
    pinned_peer_cert_sha256: 'ab',
    verify_peer_cert_by_name: ['peer'],
    fragment_settings: { xray: { packets: 'tlshello' } },
    noise_settings: { xray: [] },
    mux_settings: {},
    transport_settings: {},
    wireguard_overrides: { dns: ['1.1.1.1'] },
  })

  expect(payload.remark).toBe('keep')
  expect(payload.host).toEqual([])
  expect(payload.sni).toEqual([])
  expect(payload.path).toBe('')
  expect(payload.http_headers).toEqual({})
  expect(payload.security).toBe('inbound_default')
  expect(payload.alpn).toEqual([])
  expect(payload.fingerprint).toBe('')
  expect(payload.cipher_suites).toBeUndefined()
  expect(payload.allowinsecure).toBe(false)
  expect(payload.random_user_agent).toBe(false)
  expect(payload.use_sni_as_host).toBe(false)
  expect(payload.vless_route).toBe('')
  expect(payload.ech_config_list).toBeUndefined()
  expect(payload.ech_query_strategy).toBeUndefined()
  expect(payload.pinned_peer_cert_sha256).toBeUndefined()
  expect(payload.verify_peer_cert_by_name).toEqual([])
  expect(payload.fragment_settings).toBeUndefined()
  expect(payload.noise_settings).toBeUndefined()
  expect(payload.mux_settings).toBeUndefined()
  expect(payload.transport_settings).toBeUndefined()
  expect(payload.wireguard_overrides).toEqual({ dns: ['1.1.1.1'] })
})
