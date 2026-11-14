import { describe, expect, it } from 'bun:test'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'

import { createDefaultInboundForProtocol } from '@pasarguard/xray-config-kit'
import type { Inbound } from '@pasarguard/xray-config-kit'

import { createNewXrayProfile, validateProfileForPersist } from '@/features/core-editor/kit/xray-adapter'

const SECRET_KEY = 'yAnz5TF+lXXJte14tji3zlMNq+hd2rYUIgJBgB3fBmk='

const sectionSource = readFileSync(join(import.meta.dir, '..', '..', 'features', 'core-editor', 'components', 'xray', 'xray-inbounds-section.tsx'), 'utf8')

const declarationOf = (name: string) => sectionSource.match(new RegExp(`^function ${name}\\(ib: Inbound\\): Inbound \\{\\n[\\s\\S]*?\\n\\}\\n`, 'm'))?.[0]

const loadWireguardCreationDefaults = async () => {
  const declaration = declarationOf('applyWireguardEditorCreationDefaults')
  if (!declaration) throw new Error('applyWireguardEditorCreationDefaults is no longer declared in xray-inbounds-section.tsx')
  const js = new Bun.Transpiler({ loader: 'ts' }).transformSync(`${declaration}\nexport { applyWireguardEditorCreationDefaults }`)
  const loaded = await import(`data:text/javascript;base64,${Buffer.from(js).toString('base64')}`)
  return loaded.applyWireguardEditorCreationDefaults as (inbound: Inbound) => Inbound
}

const newWireguardInbound = async () => {
  const applyWireguardEditorCreationDefaults = await loadWireguardCreationDefaults()
  const created = createDefaultInboundForProtocol({ protocol: 'wireguard', tag: 'wg-in', listen: '127.0.0.1', port: 51888, clientDefaults: 'empty' })
  return { created, defaulted: applyWireguardEditorCreationDefaults(created) }
}

describe('a new Xray WireGuard inbound starts without an interface address', () => {
  it('the editor runs the WireGuard creation default whenever an inbound is created or its protocol changes', () => {
    const chain = declarationOf('applyInboundEditorCreationDefaults')
    expect(chain).toBeDefined()
    expect(chain).toContain('applyWireguardEditorCreationDefaults(')
  })

  it('drops the address the kit fills in instead of putting a subnet there', async () => {
    const { created, defaulted } = await newWireguardInbound()
    expect((created as unknown as Record<string, unknown>).address).toBeDefined()
    expect((defaulted as unknown as Record<string, unknown>).address).toBeUndefined()
  })

  it('saves the inbound with no address, which Xray 26.3.27 starts and the panel leaves unmanaged', async () => {
    const { defaulted } = await newWireguardInbound()
    const profile = createNewXrayProfile()
    const persisted = validateProfileForPersist({ ...profile, inbounds: [{ ...defaulted, secretKey: SECRET_KEY } as Inbound] })
    expect(persisted.ok).toBe(true)
    if (!persisted.ok) return
    const inbound = (persisted.config.inbounds as Record<string, unknown>[]).find(entry => entry.tag === 'wg-in')
    expect(inbound?.protocol).toBe('wireguard')
    expect(Object.keys((inbound?.settings ?? {}) as Record<string, unknown>)).not.toContain('address')
  })
})
