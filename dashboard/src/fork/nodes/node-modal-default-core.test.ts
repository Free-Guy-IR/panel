import { describe, expect, it } from 'bun:test'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'

const dashboardRoot = join(import.meta.dir, '..', '..', '..')
const modalSource = readFileSync(join(dashboardRoot, 'src', 'features', 'nodes', 'dialogs', 'node-modal.tsx'), 'utf8')

const extractEditDefaults = (source: string) => {
  const found: string[] = []
  const pattern = /core_config_id:\s*((?:node|nodeData)\.core_config_id\s*\?\?[^,\n]+)/g
  for (const match of source.matchAll(pattern)) found.push(match[1].trim())
  return found
}

const declaredDefaultCore = (): number | undefined => {
  const match = modalSource.match(/const DEFAULT_CORE_CONFIG_ID\s*=\s*(\d+)/)
  return match ? Number(match[1]) : undefined
}

const resolve = (expression: string, node: { core_config_id: number | null }, cores: { id: number }[]) =>
  new Function('node', 'nodeData', 'cores', 'DEFAULT_CORE_CONFIG_ID', `return ${expression}`)(
    node,
    node,
    cores,
    declaredDefaultCore(),
  ) as number | undefined

describe('node edit dialog default core', () => {
  const expressions = extractEditDefaults(modalSource)

  it('reads every edit-path core default from the real modal source', () => {
    expect(expressions.length).toBe(3)
  })

  it('shows core 1 for a node stored without a core, not the first core in the list', () => {
    const reordered = [{ id: 7 }, { id: 1 }, { id: 4 }]
    for (const expression of expressions) {
      expect(resolve(expression, { core_config_id: null }, reordered)).toBe(1)
    }
  })

  it('keeps a node that has a core on that core', () => {
    const reordered = [{ id: 7 }, { id: 1 }, { id: 4 }]
    for (const expression of expressions) {
      expect(resolve(expression, { core_config_id: 4 }, reordered)).toBe(4)
    }
  })

  it('still shows core 1 when the cores list has not loaded', () => {
    for (const expression of expressions) {
      expect(resolve(expression, { core_config_id: null }, [])).toBe(1)
    }
  })
})
