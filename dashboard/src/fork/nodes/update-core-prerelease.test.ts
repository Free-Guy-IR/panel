import { describe, expect, it } from 'bun:test'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'

const dashboardRoot = join(import.meta.dir, '..', '..', '..')
const modalSource = readFileSync(join(dashboardRoot, 'src', 'features', 'nodes', 'dialogs', 'update-core-modal.tsx'), 'utf8')

const extractUpdateButtonDisabled = (source: string) => {
  const anchor = source.indexOf('onClick={handleUpdate}')
  if (anchor < 0) throw new Error('update button not found')
  const start = source.indexOf('disabled={', anchor)
  if (start < 0) throw new Error('update button has no disabled guard')
  let depth = 0
  for (let i = start + 'disabled='.length; i < source.length; i++) {
    if (source[i] === '{') depth++
    if (source[i] === '}') depth--
    if (depth === 0) return source.slice(start + 'disabled={'.length, i)
  }
  throw new Error('unbalanced disabled guard')
}

type GuardState = {
  isPending?: boolean
  isLoadingReleases?: boolean
  versionMode: 'list' | 'custom'
  selectedVersion?: string
  latestVersion: string | null
  customVersion?: string
  customVersionError?: string
}

const guardExpression = extractUpdateButtonDisabled(modalSource)
const evaluateGuard = new Function(
  'updateCoreMutation',
  'isLoadingReleases',
  'versionMode',
  'selectedVersion',
  'latestVersion',
  'customVersion',
  'customVersionError',
  `return Boolean(${guardExpression})`,
) as (...args: unknown[]) => boolean

const isUpdateDisabled = (state: GuardState) =>
  evaluateGuard(
    { isPending: state.isPending ?? false },
    state.isLoadingReleases ?? false,
    state.versionMode,
    state.selectedVersion ?? 'latest',
    state.latestVersion,
    state.customVersion ?? '',
    state.customVersionError ?? '',
  )

describe('Xray core update dialog Update button', () => {
  it('lets an explicitly selected pre-release be installed when no stable release is listed', () => {
    expect(isUpdateDisabled({ versionMode: 'list', selectedVersion: '26.1.18-beta', latestVersion: null })).toBe(false)
  })

  it('lets an explicitly selected older release be installed while the stable latest is unknown', () => {
    expect(isUpdateDisabled({ versionMode: 'list', selectedVersion: '25.9.11', latestVersion: null })).toBe(false)
  })

  it('still blocks the Latest choice when no stable release is known', () => {
    expect(isUpdateDisabled({ versionMode: 'list', selectedVersion: 'latest', latestVersion: null })).toBe(true)
  })

  it('allows the Latest choice once a stable release is known', () => {
    expect(isUpdateDisabled({ versionMode: 'list', selectedVersion: 'latest', latestVersion: '25.10.15' })).toBe(false)
  })

  it('blocks while an update is running or releases are loading', () => {
    expect(isUpdateDisabled({ versionMode: 'list', selectedVersion: '26.1.18-beta', latestVersion: null, isPending: true })).toBe(true)
    expect(isUpdateDisabled({ versionMode: 'list', selectedVersion: '26.1.18-beta', latestVersion: null, isLoadingReleases: true })).toBe(true)
  })

  it('keeps the custom version checks unchanged', () => {
    expect(isUpdateDisabled({ versionMode: 'custom', latestVersion: null, customVersion: '  ' })).toBe(true)
    expect(isUpdateDisabled({ versionMode: 'custom', latestVersion: null, customVersion: 'v1', customVersionError: 'bad' })).toBe(true)
    expect(isUpdateDisabled({ versionMode: 'custom', latestVersion: null, customVersion: 'v26.1.18' })).toBe(false)
  })
})
