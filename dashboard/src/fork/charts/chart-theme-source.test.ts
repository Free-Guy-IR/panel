import { describe, expect, it } from 'bun:test'
import { readFileSync, readdirSync } from 'node:fs'
import { join, relative } from 'node:path'

const srcRoot = join(import.meta.dir, '..', '..')

const sourceFiles = (dir: string): string[] =>
  readdirSync(dir, { withFileTypes: true }).flatMap(entry => {
    const path = join(dir, entry.name)
    if (entry.isDirectory()) return sourceFiles(path)
    return /\.(ts|tsx)$/.test(entry.name) && !/\.test\.tsx?$/.test(entry.name) ? [path] : []
  })

describe('charts read the theme from the mounted app provider', () => {
  it('no dashboard source reads the theme from next-themes, whose provider is never mounted', () => {
    const offenders = sourceFiles(srcRoot)
      .filter(file => readFileSync(file, 'utf8').includes("from 'next-themes'"))
      .map(file => relative(srcRoot, file))
    expect(offenders).toEqual([])
  })

  it('the area chart takes useTheme from the app theme provider', () => {
    const source = readFileSync(join(srcRoot, 'components', 'charts', 'area-costume-chart.tsx'), 'utf8')
    expect(source.includes("import { useTheme } from '@/app/providers/theme-provider'")).toBe(true)
  })
})
