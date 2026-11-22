import { test } from 'node:test'
import assert from 'node:assert/strict'

import { importRawToProfile, profileToPersistedConfig } from '@/features/core-editor/kit/xray-adapter'

type Entry = { tag?: string; streamSettings?: { finalmask?: { tcp?: { settings?: Record<string, unknown> }[]; udp?: unknown[] } } }

const LIST_ONLY = { packets: 'tlshello', lengths: ['100-200', '200-300'], delays: ['10-20', '20-30'] }
const STABLE = { ...LIST_ONLY, length: '100-200', delay: '10-20' }
const VLESS_USER = { id: '1818cf9e-4ebd-4925-8877-21fe8fd40416', encryption: 'none' }

const rawWithMask = (finalmask: Record<string, unknown>) => ({
  inbounds: [
    {
      tag: 'VLESS TCP',
      listen: '0.0.0.0',
      port: 2053,
      protocol: 'vless',
      settings: { clients: [], decryption: 'none' },
      streamSettings: { network: 'tcp', security: 'none', finalmask: structuredClone(finalmask) },
    },
  ],
  outbounds: [
    { protocol: 'freedom', tag: 'DIRECT' },
    { protocol: 'freedom', tag: 'FRAGMENTED', streamSettings: { finalmask: structuredClone(finalmask) } },
    {
      protocol: 'vless',
      tag: 'UPSTREAM',
      settings: { vnext: [{ address: '198.51.100.1', port: 443, users: [VLESS_USER] }] },
      streamSettings: { network: 'tcp', finalmask: structuredClone(finalmask) },
    },
  ],
})

const persisted = (finalmask: Record<string, unknown>) => {
  const { profile, issues } = importRawToProfile(rawWithMask(finalmask))
  assert.deepEqual(issues, [])
  return profileToPersistedConfig(profile)
}

const masks = (config: Record<string, unknown>) =>
  [
    ...(config.inbounds as Entry[]).filter(entry => entry.tag === 'VLESS TCP'),
    ...(config.outbounds as Entry[]).filter(entry => entry.tag === 'FRAGMENTED' || entry.tag === 'UPSTREAM'),
  ].map(entry => entry.streamSettings?.finalmask)

test('a fragment with only lengths and delays is saved with the length and delay stable Xray reads', () => {
  const saved = masks(persisted({ tcp: [{ type: 'fragment', settings: LIST_ONLY }] }))

  assert.equal(saved.length, 3)
  for (const finalmask of saved) assert.deepEqual(finalmask, { tcp: [{ type: 'fragment', settings: STABLE }] })
})

test('a fragment length and delay the admin set are saved unchanged next to the lists', () => {
  const settings = { ...LIST_ONLY, length: '50-60', delay: '5' }
  const saved = masks(persisted({ tcp: [{ type: 'fragment', settings }] }))

  assert.equal(saved.length, 3)
  for (const finalmask of saved) assert.deepEqual(finalmask, { tcp: [{ type: 'fragment', settings }] })
})

test('only fragment layers gain the scalars', () => {
  const noise = { type: 'noise', settings: { noise: [{ type: 'str', packet: 'abc', delay: '5-10' }] } }
  const header = { type: 'header-custom', settings: { clients: [[{ type: 'array', packet: [1], delay: '5-10' }]] } }
  const saved = masks(persisted({ tcp: [header, { type: 'fragment', settings: LIST_ONLY }], udp: [noise] }))

  assert.equal(saved.length, 3)
  for (const finalmask of saved) {
    assert.deepEqual(finalmask, { tcp: [header, { type: 'fragment', settings: STABLE }], udp: [noise] })
  }
})
