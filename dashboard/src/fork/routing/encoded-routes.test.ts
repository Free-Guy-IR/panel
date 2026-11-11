import { describe, expect, it } from 'bun:test'

import { settingsUrlVisibleTo } from '@/fork/settings/credential-tabs'
import type { AdminDetails } from '@/service/api'
import { canAccessRoute } from '@/utils/rbac'

const manager = {
  username: 'manager',
  role: { is_owner: false, permissions: { settings: { read: true, read_general: true, update: true } } },
} as unknown as AdminDetails

const owner = { username: 'owner', role: { is_owner: true, permissions: {} } } as unknown as AdminDetails

const reader = {
  username: 'reader',
  role: { is_owner: false, permissions: { users: { read: true }, hosts: { read: true } } },
} as unknown as AdminDetails

describe('the route guard reads a path the way the router matches it', () => {
  it('keeps non-owners out of the credential settings pages when a letter is percent-encoded', () => {
    for (const path of ['/settings/%74elegram', '/settings/%54elegram', '/settings/n%6Ftifications', '/settings/%77ebhook/', '/%73ettings/webhook']) {
      expect(canAccessRoute(manager, path)).toBe(false)
      expect(settingsUrlVisibleTo(path, false)).toBe(false)
      expect(canAccessRoute(owner, path)).toBe(true)
    }
  })

  it('keeps every permission-gated page closed when a letter is percent-encoded', () => {
    for (const path of ['/%61dmin-roles', '/%61dmins', '/%61pi-keys', '/%6Eodes', '/nodes/%6Cogs', '/%67roups', '/%73tatistics', '/%74emplates/user']) {
      expect(canAccessRoute(reader, path)).toBe(false)
    }
    expect(canAccessRoute(manager, '/%75sers')).toBe(false)
  })

  it('still opens the pages an admin may see when the path is percent-encoded', () => {
    expect(canAccessRoute(reader, '/%75sers')).toBe(true)
    expect(canAccessRoute(reader, '/%68osts')).toBe(true)
    expect(canAccessRoute(manager, '/settings/%73ubscriptions')).toBe(true)
  })

  it('never throws on a malformed escape, and judges it undecoded as the router does', () => {
    for (const path of ['/settings/%E0%A4%A', '/%61dmin-roles%', '/%zzadmins']) {
      expect(() => canAccessRoute(manager, path)).not.toThrow()
      expect(() => settingsUrlVisibleTo(path, false)).not.toThrow()
    }
    expect(canAccessRoute(manager, '/settings/telegram%')).toBe(true)
    expect(canAccessRoute(manager, '/settings/%74elegram/%E0%A4%A')).toBe(true)
    expect(canAccessRoute(reader, '/admin-roles/%E0%A4%A')).toBe(false)
  })
})
