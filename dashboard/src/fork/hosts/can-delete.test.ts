import { describe, expect, it } from 'bun:test'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'

import { PERMISSION_GROUPS } from '@/features/admin-roles/forms/admin-role-form'
import { hasPermission } from '@/utils/rbac'
import type { AdminDetails } from '@/service/api'

const SRC = join(import.meta.dir, '..', '..')
const DELETE_SCREENS = ['features/hosts/components/host-actions-menu.tsx', 'features/hosts/components/hosts-list.tsx']

const adminWith = (hosts: Record<string, boolean>) =>
  ({ username: 'editor', role: { is_owner: false, permissions: { hosts } } }) as unknown as AdminDetails

describe('host deletion permission', () => {
  it('is offered in the role editor next to host editing', () => {
    const actions = PERMISSION_GROUPS.flatMap(group => group.actions).filter(item => item.resource === 'hosts')
    expect(actions.map(item => item.action)).toContain('delete')
  })

  it('is separate from host editing', () => {
    expect(hasPermission(adminWith({ read: true, update: true }), 'hosts', 'delete')).toBe(false)
    expect(hasPermission(adminWith({ read: true, update: true, delete: true }), 'hosts', 'delete')).toBe(true)
  })

  it('gates every delete control on the hosts screens', () => {
    for (const relativePath of DELETE_SCREENS) {
      const source = readFileSync(join(SRC, relativePath), 'utf8')
      expect(source).toContain('useCanDeleteHosts()')
      expect(source).not.toMatch(/if \(!canUpdate \|\| !(host\.id|selectedHostIds\.length)\) return\n\n    try \{\n      (await removeHost|const response = await bulkDeleteHostsMutation)/)
    }
  })

  it('lets an admin holding only hosts.delete select hosts and reach the delete controls', () => {
    const list = readFileSync(join(SRC, 'features/hosts/components/hosts-list.tsx'), 'utf8')
    expect(list).not.toContain('{canUpdate && <BulkActionsBar')
    expect(list).not.toMatch(/enableSelection=\{canUpdate\}/)
    expect(list).toContain('enableSelection={canUpdate || canDelete}')
    expect(list).not.toMatch(/injectSelectionProps=\{canUpdate\}/)
    const columns = readFileSync(join(SRC, 'features/hosts/components/use-hosts-list-columns.tsx'), 'utf8')
    expect(columns).toContain('canUpdate || canCreate || canDelete')
  })
})
