import { describe, expect, it } from 'bun:test'
import { importRawToProfile, profileToPersistedConfig, validateProfileForPersist } from '@/features/core-editor/kit/xray-adapter'

const PRIVATE_KEY = 'yAnz5TF+lXXJte14tji3zlMNq+hd2rYUIgJBgB3fBmk='
const PEER_PUBLIC_KEY = 'HIgo9xNzJMWLKASShiTqIybxZ0U3wGLiUeJ1PKf8ykw='
const peers = [{ publicKey: PEER_PUBLIC_KEY, allowedIPs: ['10.0.0.2/32'] }]

const rawWith = (settings: Record<string, unknown>) => ({
  log: { loglevel: 'warning' },
  inbounds: [{ tag: 'wg-in', protocol: 'wireguard', port: 51820, settings: { secretKey: PRIVATE_KEY, address: ['10.0.0.1/24'], ...settings } }],
  outbounds: [{ protocol: 'freedom', tag: 'DIRECT' }],
})

type Persisted = { inbounds: { settings?: { peers?: unknown[] } }[] }

describe('Xray WireGuard inbound peers survive a structured editor save', () => {
  it('keeps the peers a stored config carries through load and save', () => {
    const { profile } = importRawToProfile(rawWith({ peers }))
    expect(validateProfileForPersist(profile).ok).toBe(true)
    const out = profileToPersistedConfig(profile) as Persisted
    expect(out.inbounds[0].settings?.peers).toEqual(peers)
  })

  it('still gives an inbound without peers the empty list the kit requires', () => {
    const { profile } = importRawToProfile(rawWith({}))
    const out = profileToPersistedConfig(profile) as Persisted
    expect(out.inbounds[0].settings?.peers ?? []).toEqual([])
  })
})
