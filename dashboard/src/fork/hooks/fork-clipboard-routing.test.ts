import { describe, expect, it } from 'bun:test'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'

const srcRoot = join(import.meta.dir, '..', '..')

const forkCopyActions = ['features/core-editor/components/l2tp/l2tp-form.tsx']

describe('fork copy actions', () => {
  for (const file of forkCopyActions) {
    it(`${file} copies through useClipboard so plain-HTTP panels can copy`, () => {
      const source = readFileSync(join(srcRoot, file), 'utf8')
      expect(source).not.toContain('navigator.clipboard')
      expect(source).toMatch(/import \{ useClipboard \} from '@\/hooks\/use-clipboard'/)
    })
  }
})
