import { describe, expect, it } from 'bun:test'
import { readdirSync, readFileSync, statSync } from 'node:fs'
import { join, relative } from 'node:path'

import { getGetHostsQueryKey } from '@/service/api'

const srcRoot = join(import.meta.dir, '..', '..')

const sourceFiles = (dir: string): string[] =>
  readdirSync(dir).flatMap(entry => {
    const path = join(dir, entry)
    if (statSync(path).isDirectory()) return entry === 'api' && dir.endsWith('service') ? [] : sourceFiles(path)
    return /\.(ts|tsx)$/.test(entry) && !/\.test\.tsx?$/.test(entry) ? [path] : []
  })

describe('hosts query key', () => {
  it('is the generated /api/hosts key', () => {
    expect(getGetHostsQueryKey()).toEqual(['/api/hosts'])
  })

  it('no hosts query or invalidation uses a hand-written key literal', () => {
    const offenders: string[] = []
    for (const file of sourceFiles(srcRoot)) {
      const source = readFileSync(file, 'utf8')
      for (const match of source.matchAll(/queryKey:\s*\[\s*['"`](getHostsQueryKey|getGetHostsQueryKey|\/api\/hosts)['"`]\s*\]/g)) {
        offenders.push(`${relative(srcRoot, file)}: ${match[0]}`)
      }
    }
    expect(offenders).toEqual([])
  })
})
