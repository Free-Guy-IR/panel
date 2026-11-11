import { describe, expect, it } from 'bun:test'

import type { AdminDetails } from '@/service/api'
import { hasPermission } from '@/utils/rbac'

const adminWith = (permissions: Record<string, Record<string, unknown>>) =>
  ({ username: 'scoped', role: { is_owner: false, permissions } }) as unknown as AdminDetails

describe('scope own in the dashboard', () => {
  it('is refused on resources that have no owner, as the server refuses it', () => {
    expect(hasPermission(adminWith({ admins: { read: { scope: 1 } } }), 'admins', 'read')).toBe(false)
    expect(hasPermission(adminWith({ nodes: { read: { scope: 1 } } }), 'nodes', 'read')).toBe(false)
    expect(hasPermission(adminWith({ hosts: { delete: { scope: 1 } } }), 'hosts', 'delete')).toBe(false)
  })

  it('keeps its meaning on users, api keys and hwids', () => {
    expect(hasPermission(adminWith({ users: { read: { scope: 1 } } }), 'users', 'read')).toBe(true)
    expect(hasPermission(adminWith({ api_keys: { read: { scope: 1 } } }), 'api_keys', 'read')).toBe(true)
    expect(hasPermission(adminWith({ hwids: { read: { scope: 1 } } }), 'hwids', 'read')).toBe(true)
  })

  it('leaves scope all and plain grants alone', () => {
    expect(hasPermission(adminWith({ admins: { read: { scope: 2 } } }), 'admins', 'read')).toBe(true)
    expect(hasPermission(adminWith({ admins: { read: true } }), 'admins', 'read')).toBe(true)
  })
})
