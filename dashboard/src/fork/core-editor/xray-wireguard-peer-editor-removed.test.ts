import { describe, expect, it } from 'bun:test'
import { readFileSync } from 'node:fs'

const section = readFileSync(new URL('../../features/core-editor/components/xray/xray-inbounds-section.tsx', import.meta.url), 'utf8')

describe('Xray inbound editor without the manual WireGuard peer form', () => {
  it('no longer offers to add, edit or generate inbound peers', () => {
    for (const marker of ['wireguardPeersForUi', 'coreEditor.inbound.wireguard.addPeer', 'setWireguardPeerPrivateKeys', 'wireguardKitAllowedMigrateKeyRef']) {
      expect({ marker, present: section.includes(marker) }).toEqual({ marker, present: false })
    }
  })

  it('still edits the interface addresses of a WireGuard inbound', () => {
    expect(section).toContain('coreEditor.inbound.wireguard.address')
    expect(section).toContain('wireguardAddressesForUi(inbound)')
  })
})
