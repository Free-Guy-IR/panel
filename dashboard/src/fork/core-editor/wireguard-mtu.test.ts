import { describe, expect, it } from 'bun:test'

import { draftToPersistedConfig, validateWireGuardDraftForSave, wireGuardConfigToDraft } from '@/features/core-editor/kit/wireguard-adapter'

const PRIVATE_KEY = 'yAnz5TF+lXXJte14tji3zlMNq+hd2rYUIgJBgB3fBmk='

const raw = (extra: Record<string, unknown> = {}) => ({
  interface_name: 'wg0',
  private_key: PRIVATE_KEY,
  listen_port: 51820,
  address: ['10.8.0.1/24'],
  ...extra,
})

const draftOf = (extra: Record<string, unknown>) => {
  const loaded = wireGuardConfigToDraft(raw())
  if (!loaded.ok) throw new Error(loaded.message)
  return { ...loaded.draft, extra: { ...loaded.draft.extra, ...extra } } as typeof loaded.draft
}

describe('WireGuard server interface MTU in the core editor', () => {
  it('keeps a valid MTU through load and save', () => {
    const loaded = wireGuardConfigToDraft(raw({ mtu: 1420 }))
    expect(loaded.ok).toBe(true)
    if (!loaded.ok) return
    expect(loaded.draft.extra.mtu).toBe(1420)
    expect(draftToPersistedConfig(loaded.draft).mtu).toBe(1420)
  })

  it('writes no MTU key when none was set', () => {
    const loaded = wireGuardConfigToDraft(raw())
    expect(loaded.ok).toBe(true)
    if (!loaded.ok) return
    expect('mtu' in draftToPersistedConfig(loaded.draft)).toBe(false)
  })

  for (const mtu of [575, 9001, 1420.5, '1420']) {
    it(`refuses to load or save the MTU ${JSON.stringify(mtu)} the node would reject`, () => {
      expect(wireGuardConfigToDraft(raw({ mtu })).ok).toBe(false)
      const saved = validateWireGuardDraftForSave(draftOf({ mtu }))
      expect(saved.ok).toBe(false)
    })
  }

  for (const mtu of [576, 9000]) {
    it(`accepts the boundary MTU ${mtu}`, () => {
      expect(wireGuardConfigToDraft(raw({ mtu })).ok).toBe(true)
      expect(validateWireGuardDraftForSave(draftOf({ mtu })).ok).toBe(true)
    })
  }
})
