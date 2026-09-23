import { describe, expect, it } from 'bun:test'

import { importRawToProfile, profileToPersistedConfig } from '@/features/core-editor/kit/xray-adapter'

function roundTrip(address: string) {
  const raw = {
    log: { loglevel: 'warning' },
    inbounds: [
      {
        tag: 'relay-in',
        protocol: 'dokodemo-door',
        listen: '0.0.0.0',
        port: 20001,
        settings: { address, port: 8443, network: 'tcp' },
      },
    ],
    outbounds: [{ protocol: 'freedom', tag: 'DIRECT' }],
  }
  const { profile } = importRawToProfile(raw)
  const saved = profileToPersistedConfig(profile) as { inbounds: Array<Record<string, any>> }
  const inbound = saved.inbounds.find(i => i.tag === 'relay-in')!
  return inbound.settings?.address ?? inbound.address
}

describe('core editor keeps tunnel targets on load and save', () => {
  it('keeps a public target', () => {
    expect(roundTrip('10.20.30.40')).toBe('10.20.30.40')
  })
  for (const loopback of ['127.0.0.1', 'localhost', '::1']) {
    it(`keeps the loopback target ${loopback}`, () => {
      expect(roundTrip(loopback)).toBe(loopback)
    })
  }
})
