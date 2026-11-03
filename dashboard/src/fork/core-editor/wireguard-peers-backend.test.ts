import { describe, expect, it } from 'bun:test'
import type { Profile } from '@pasarguard/xray-config-kit'

import { sanitizeProfileInbounds } from '@/features/core-editor/kit/sanitize-inbound'
import { draftToPersistedConfig, getWireGuardPersistConfig, wireGuardConfigToDraft } from '@/features/core-editor/kit/wireguard-adapter'

const PRIVATE_KEY = 'yAnz5TF+lXXJte14tji3zlMNq+hd2rYUIgJBgB3fBmk='
const PEER_PUBLIC_KEY = 'HIgo9xNzJMWLKASShiTqIybxZ0U3wGLiUeJ1PKf8ykw='

const legacyPeers = [{ public_key: PEER_PUBLIC_KEY, allowed_ips: ['10.8.0.2/32'] }]

const rawWireGuardCore = (extra: Record<string, unknown> = {}) => ({
  interface_name: 'wg0',
  private_key: PRIVATE_KEY,
  listen_port: 51820,
  address: ['10.8.0.1/24'],
  ...extra,
})

const xrayWireguardInbound = {
  tag: 'wg-in',
  protocol: 'wireguard',
  port: 51820,
  secretKey: PRIVATE_KEY,
  address: ['10.0.0.1/24'],
  peers: [{ publicKey: PEER_PUBLIC_KEY, allowedIPs: ['10.0.0.2/32'] }],
}

const xrayVlessInbound = {
  tag: 'vless-in',
  protocol: 'vless',
  port: 443,
  clients: [],
  transport: { type: 'tcp' },
  security: { type: 'none' },
}

const profileWith = (inbounds: unknown[]) => ({ inbounds, outbounds: [], routing: { rules: [] } }) as unknown as Profile

describe('Xray WireGuard inbound peers are left to the backend', () => {
  it('keeps the peers an inbound carries while the fork node does not supply Xray WireGuard user peers', () => {
    const sanitized = sanitizeProfileInbounds(profileWith([xrayWireguardInbound]))
    const inbound = sanitized.inbounds[0] as unknown as Record<string, unknown>
    expect(inbound.peers).toEqual(xrayWireguardInbound.peers)
    expect(inbound.secretKey).toBe(PRIVATE_KEY)
    expect(inbound.address).toEqual(['10.0.0.1/24'])
  })

  it('gives a WireGuard inbound without peers an empty peer list', () => {
    const withoutPeers: Record<string, unknown> = { ...xrayWireguardInbound }
    delete withoutPeers.peers
    const sanitized = sanitizeProfileInbounds(profileWith([withoutPeers]))
    expect((sanitized.inbounds[0] as unknown as Record<string, unknown>).peers).toEqual([])
  })

  it('leaves the other inbounds of the profile untouched', () => {
    const sanitized = sanitizeProfileInbounds(profileWith([xrayVlessInbound, xrayWireguardInbound]))
    expect(sanitized.inbounds[0]).toEqual(xrayVlessInbound as never)
    expect((sanitized.inbounds[1] as unknown as Record<string, unknown>).peers).toEqual(xrayWireguardInbound.peers)
  })

  it('does not mutate the profile it was given', () => {
    const inbound = structuredClone(xrayWireguardInbound)
    sanitizeProfileInbounds(profileWith([inbound]))
    expect(inbound.peers).toEqual(xrayWireguardInbound.peers)
  })
})

describe('WireGuard core config never persists peers', () => {
  it('drops peers a legacy config carried when it is loaded and saved again', () => {
    const loaded = wireGuardConfigToDraft(rawWireGuardCore({ peers: legacyPeers }))
    expect(loaded.ok).toBe(true)
    if (!loaded.ok) return
    expect('peers' in loaded.draft.extra).toBe(false)
    expect('peers' in draftToPersistedConfig(loaded.draft)).toBe(false)
  })

  it('drops peers placed in the draft extra fields', () => {
    const loaded = wireGuardConfigToDraft(rawWireGuardCore())
    expect(loaded.ok).toBe(true)
    if (!loaded.ok) return
    const draft = { ...loaded.draft, extra: { ...loaded.draft.extra, peers: legacyPeers } } as typeof loaded.draft
    const persisted = draftToPersistedConfig(draft)
    expect('peers' in persisted).toBe(false)
    expect(persisted.interface_name).toBe('wg0')
    expect(persisted.address).toEqual(['10.8.0.1/24'])
    const saved = getWireGuardPersistConfig(draft)
    expect(saved.ok).toBe(true)
    if (saved.ok) expect('peers' in saved.config).toBe(false)
  })

  it('keeps other extra fields of the config', () => {
    const loaded = wireGuardConfigToDraft(rawWireGuardCore({ peers: legacyPeers, mtu: 1420 }))
    expect(loaded.ok).toBe(true)
    if (!loaded.ok) return
    expect(draftToPersistedConfig(loaded.draft).mtu).toBe(1420)
  })
})
