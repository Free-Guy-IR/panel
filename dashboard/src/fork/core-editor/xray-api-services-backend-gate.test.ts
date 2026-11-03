import { describe, expect, it } from 'bun:test'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'

import { coreBackendTypes } from '../../features/nodes/forms/core-config-form'

const modalLines = readFileSync(join(import.meta.dir, '..', '..', 'features', 'nodes', 'dialogs', 'core-config-modal.tsx'), 'utf8').split('\n')

const guardOf = (needle: string) => {
  const at = modalLines.findIndex(line => line.includes(needle))
  if (at < 0) return ''
  for (let index = at - 1; index >= 0; index--) {
    const line = modalLines[index].trim()
    if (line.startsWith('if (')) return line
  }
  return ''
}

describe('xray api-services validation on save is limited to xray cores', () => {
  it('the fork offers core types besides xray and wireguard', () => {
    expect(coreBackendTypes.filter(type => type !== 'xray' && type !== 'wg').length > 0).toBe(true)
  })

  it('the save-time unknown api-services check runs for xray only, like the derived check that drives the save button', () => {
    expect(guardOf('const unknownServices = findUnknownApiServices(configObj)')).toBe("if (backendType === 'xray') {")
  })
})
