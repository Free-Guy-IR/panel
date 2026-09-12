import { expect, test } from 'bun:test'
import { isForkHostProtocol, resolveHostMode } from '../../dashboard/src/fork/hosts/resolve-mode.ts'

test('resolveHostMode maps fork protocols and falls back to xray', () => {
  expect(resolveHostMode('wireguard')).toBe('wireguard')
  expect(resolveHostMode('openvpn')).toBe('openvpn')
  expect(resolveHostMode('mtproto')).toBe('mtproto')
  expect(resolveHostMode('l2tp')).toBe('l2tp')
  expect(resolveHostMode('vless')).toBe('xray')
  expect(resolveHostMode('hysteria2')).toBe('xray')
  expect(resolveHostMode(undefined)).toBe('xray')
  expect(resolveHostMode(null)).toBe('xray')
  expect(resolveHostMode('')).toBe('xray')
})

test('isForkHostProtocol', () => {
  expect(isForkHostProtocol('wireguard')).toBe(true)
  expect(isForkHostProtocol('openvpn')).toBe(true)
  expect(isForkHostProtocol('mtproto')).toBe(true)
  expect(isForkHostProtocol('l2tp')).toBe(true)
  expect(isForkHostProtocol('vmess')).toBe(false)
  expect(isForkHostProtocol(undefined)).toBe(false)
})
