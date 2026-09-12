import { expect, test } from 'bun:test'
import { applyHostProtocolPayload, normalizeOpenvpnOverrides, normalizeWireguardOverrides } from '../../dashboard/src/fork/hosts/payload.ts'

test('normalizeWireguardOverrides drops empty values', () => {
  expect(normalizeWireguardOverrides(undefined)).toBeUndefined()
  expect(normalizeWireguardOverrides({ allowed_ips: [], reserved: '  ', dns: [] })).toBeUndefined()
  expect(normalizeWireguardOverrides({ allowed_ips: ['0.0.0.0/0'], reserved: '0,0,0', mtu: 1280, keepalive_seconds: 25, dns: ['1.1.1.1'] })).toEqual({
    allowed_ips: ['0.0.0.0/0'],
    reserved: '0,0,0',
    mtu: 1280,
    keepalive_seconds: 25,
    dns: ['1.1.1.1'],
  })
})

test('normalizeOpenvpnOverrides drops empty dns', () => {
  expect(normalizeOpenvpnOverrides(undefined)).toBeUndefined()
  expect(normalizeOpenvpnOverrides({ dns_servers: [] })).toBeUndefined()
  expect(normalizeOpenvpnOverrides({ dns_servers: ['8.8.8.8'] })).toEqual({ dns_servers: ['8.8.8.8'] })
})

test('applyHostProtocolPayload wireguard strips xray fields and foreign overrides', () => {
  const payload = applyHostProtocolPayload(
    {
      host: ['a'],
      security: 'tls',
      wireguard_overrides: { dns: ['1.1.1.1'], allowed_ips: [] },
      openvpn_overrides: { dns_servers: ['8.8.8.8'] },
    },
    'wireguard',
  )
  expect(payload.host).toEqual([])
  expect(payload.security).toBe('inbound_default')
  expect(payload.wireguard_overrides).toEqual({ dns: ['1.1.1.1'] })
  expect(payload.openvpn_overrides).toBeUndefined()
})

test('applyHostProtocolPayload openvpn keeps only openvpn overrides', () => {
  const payload = applyHostProtocolPayload(
    {
      path: '/x',
      wireguard_overrides: { dns: ['1.1.1.1'] },
      openvpn_overrides: { dns_servers: ['9.9.9.9'] },
    },
    'openvpn',
  )
  expect(payload.path).toBe('')
  expect(payload.wireguard_overrides).toBeUndefined()
  expect(payload.openvpn_overrides).toEqual({ dns_servers: ['9.9.9.9'] })
})

test('applyHostProtocolPayload mtproto and l2tp strip extras', () => {
  for (const protocol of ['mtproto', 'l2tp'] as const) {
    const payload = applyHostProtocolPayload(
      {
        sni: ['x'],
        wireguard_overrides: { dns: ['1.1.1.1'] },
        openvpn_overrides: { dns_servers: ['8.8.8.8'] },
      },
      protocol,
    )
    expect(payload.sni).toEqual([])
    expect(payload.wireguard_overrides).toBeUndefined()
    expect(payload.openvpn_overrides).toBeUndefined()
  }
})

test('applyHostProtocolPayload xray only drops fork overrides', () => {
  const payload = applyHostProtocolPayload(
    {
      host: ['keep.example'],
      security: 'tls',
      wireguard_overrides: { dns: ['1.1.1.1'] },
      openvpn_overrides: { dns_servers: ['8.8.8.8'] },
    },
    'vless',
  )
  expect(payload.host).toEqual(['keep.example'])
  expect(payload.security).toBe('tls')
  expect(payload.wireguard_overrides).toBeUndefined()
  expect(payload.openvpn_overrides).toBeUndefined()
})
