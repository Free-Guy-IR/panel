import { expect, test } from 'bun:test'
import { applyHostProtocolPayload } from './payload'
import { XRAY_HOST_FIELD_CLEARS, stripXrayHostFields } from './xray-fields'

const ECH_CLIENT_FIELDS = ['mihomo_ech_config', 'mihomo_ech_query_server_name', 'sing_box_ech_config', 'sing_box_ech_query_server_name']

const camouflage = () => ({
  fragment_settings: { xray: { packets: 'tlshello', length: '100-200', interval: '10-20' } },
  noise_settings: { xray: [{ type: 'rand', packet: '10-20', delay: '10-16', apply_to: 'ip' }] },
  wireguard_overrides: { mtu: 1280 },
})

test('a WireGuard host keeps the camouflage settings the modal lets the admin edit', () => {
  const payload: any = applyHostProtocolPayload(camouflage() as any, 'wireguard')
  expect(payload.fragment_settings).toEqual(camouflage().fragment_settings)
  expect(payload.noise_settings).toEqual(camouflage().noise_settings)
  expect(payload.wireguard_overrides).toEqual({ mtu: 1280 })
})

test('an Xray host keeps its camouflage settings', () => {
  const payload: any = applyHostProtocolPayload(camouflage() as any, 'vless')
  expect(payload.fragment_settings).toEqual(camouflage().fragment_settings)
  expect(payload.noise_settings).toEqual(camouflage().noise_settings)
})

test('a WireGuard save still drops the per-client ECH fields', () => {
  const payload: any = applyHostProtocolPayload(
    { mihomo_ech_config: 'a', mihomo_ech_query_server_name: 'b', sing_box_ech_config: 'c', sing_box_ech_query_server_name: 'd' } as any,
    'wireguard',
  )
  for (const field of ECH_CLIENT_FIELDS) expect(payload[field]).toBeUndefined()
})

test('switching protocol resets the camouflage and per-client ECH fields', () => {
  const names = XRAY_HOST_FIELD_CLEARS.map(([name]) => name as string)
  for (const field of ['fragment_settings', 'noise_settings', ...ECH_CLIENT_FIELDS]) expect(names).toContain(field)
})

test('stripping Xray fields gives every payload its own empty containers', () => {
  const first: any = stripXrayHostFields({})
  const second: any = stripXrayHostFields({})
  first.host.push('mutated')
  first.http_headers.mutated = true
  expect(second.host).toEqual([])
  expect(second.http_headers).toEqual({})
})
