import { describe, expect, it } from 'bun:test'
import { readFileSync } from 'node:fs'
import config from '../../../vite.config.mts'

type BuildConfig = { build?: { emptyOutDir?: boolean; rollupOptions?: { output?: { manualChunks?: unknown } } } }

const viteSource = readFileSync(new URL('../../../vite.config.mts', import.meta.url), 'utf8')

describe('dashboard build chunking', () => {
  it('keeps clearing the output directory on every build', () => {
    expect((config as BuildConfig).build?.emptyOutDir).toBe(true)
  })

  it('leaves lazy import boundaries to the bundler instead of manual vendor groups', () => {
    expect((config as BuildConfig).build?.rollupOptions?.output?.manualChunks).toBeUndefined()
    expect(viteSource).not.toContain('manualChunks')
  })

  it('caches at runtime only same-origin static scripts, never API responses', () => {
    expect(viteSource).toContain("sameOrigin && request.destination === 'script' && /\\/statics\\/[^/]+\\.js$/.test(url.pathname)")
    expect(viteSource.match(/urlPattern:/g)?.length).toBe(1)
  })
})
