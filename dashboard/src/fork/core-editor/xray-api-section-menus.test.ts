import { describe, expect, it } from 'bun:test'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'

import { XRAY_CORE_SECTION_NAV } from '../../features/core-editor/kit/core-section-nav'

const srcRoot = join(import.meta.dir, '..', '..')

const commandMenus = ['components/layout/command-palette.tsx', 'features/core-editor/components/shared/core-command-menu.tsx']

const xraySectionIds = (file: string): string[] => {
  const source = readFileSync(join(srcRoot, file), 'utf8')
  const block = source.match(/const xraySections[^=]*= \[([\s\S]*?)\n\]/)
  if (!block) return []
  return [...block[1].matchAll(/\{ id: '([^']+)'/g)].map(match => match[1])
}

describe('xray command menus list every xray section of the editor navigation', () => {
  it('the navigation lists the api section', () => {
    expect(XRAY_CORE_SECTION_NAV.map(item => item.id)).toContain('api')
  })

  for (const file of commandMenus) {
    it(`${file} lists every navigation section`, () => {
      const listed = xraySectionIds(file)
      const missing = XRAY_CORE_SECTION_NAV.map(item => item.id).filter(id => !listed.includes(id))
      expect(missing).toEqual([])
    })
  }
})

describe('core config modal API services heading', () => {
  const modalSource = readFileSync(join(srcRoot, 'features/nodes/dialogs/core-config-modal.tsx'), 'utf8')
  const headingLine = modalSource.split('\n').find(line => line.includes("t('coreConfigModal.apiServices',")) ?? ''

  it('is rendered', () => {
    expect(headingLine.trim().length > 0).toBe(true)
  })

  it('is a plain Label, not a FormLabel that needs an enclosing FormField', () => {
    expect(headingLine.includes('<FormLabel')).toBe(false)
    expect(headingLine.trim().startsWith('<Label')).toBe(true)
  })
})
