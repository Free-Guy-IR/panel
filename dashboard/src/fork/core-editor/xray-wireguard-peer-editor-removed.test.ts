import { describe, expect, it } from 'bun:test'
import { readFileSync } from 'node:fs'

const readXraySource = (name: string) => readFileSync(new URL(`../../features/core-editor/components/xray/${name}`, import.meta.url), 'utf8')
const section = readXraySource('xray-inbounds-section.tsx')
const wireguardFields = readXraySource('wireguard-inbound-fields.tsx')

describe('Xray inbound editor without the manual WireGuard peer form', () => {
  it('no longer offers to add, edit or generate inbound peers', () => {
    for (const marker of ['wireguardPeersForUi', 'coreEditor.inbound.wireguard.addPeer', 'setWireguardPeerPrivateKeys', 'wireguardKitAllowedMigrateKeyRef']) {
      expect({ marker, present: section.includes(marker) || wireguardFields.includes(marker) }).toEqual({ marker, present: false })
    }
  })

  it('still edits the interface addresses of a WireGuard inbound', () => {
    expect(section).toContain('<WireguardInboundFields inbound={inbound}')
    expect(wireguardFields).toContain('coreEditor.inbound.wireguard.address')
    expect(wireguardFields).toContain('patchInbound({ address: cleaned.length > 0 ? cleaned : undefined }')
  })
})
