import { describe, expect, it } from 'bun:test'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'

import * as apiClient from '@/service/api'

const dashboardRoot = join(import.meta.dir, '..', '..', '..')

const PASSKEY_UI_FILES = ['src/components/layout/nav-user.tsx', 'src/features/admins/dialogs/admin-modal.tsx']

const namedApiImports = (source: string) => {
  const names: string[] = []
  for (const match of source.matchAll(/import\s+(type\s+)?\{([^}]*)\}\s*from\s*'@\/service\/api'/g)) {
    if (match[1]) continue
    for (const entry of match[2].split(',')) {
      const text = entry.trim()
      if (!text || /^type\s/.test(text)) continue
      const name = text.split(/\s+as\s+/)[0].trim()
      if (name) names.push(name)
    }
  }
  return names
}

describe('the passkey admin UI imports names the generated client exports', () => {
  for (const file of PASSKEY_UI_FILES) {
    it(`every @/service/api import in ${file} resolves`, () => {
      const source = readFileSync(join(dashboardRoot, file), 'utf8')
      const names = namedApiImports(source)

      expect(names.length).toBeGreaterThan(0)
      const missing = names.filter(name => !(name in apiClient))
      expect(missing).toEqual([])
    })
  }

  it('the four passkey operations are callable functions', () => {
    for (const name of ['listAdminPasskeys', 'adminPasskeyRegisterOptions', 'adminPasskeyRegisterVerify', 'deleteAdminPasskey']) {
      expect(typeof (apiClient as Record<string, unknown>)[name]).toBe('function')
    }
  })
})
