import { beforeAll, describe, expect, it } from 'bun:test'
import { renderToStaticMarkup } from 'react-dom/server'

import { ChartStyle } from '@/components/ui/chart'

beforeAll(() => {
  if (typeof globalThis.CSS?.escape !== 'function') {
    globalThis.CSS = { escape: (value: string) => value.replace(/[^a-zA-Z0-9_-]/g, ch => `\\${ch}`) } as unknown as typeof CSS
  }
})

const hostileNodeName = 'de1: red; } body { display: none } </style><script>alert(1)</script> .x {'

describe('ChartStyle', () => {
  it('keeps a node or admin name used as a series key inside one escaped custom property', () => {
    const markup = renderToStaticMarkup(<ChartStyle id="chart-t" config={{ [hostileNodeName]: { label: hostileNodeName, color: 'hsl(var(--chart-1))' } }} />)
    const css = markup.replace(/^<style>/, '').replace(/<\/style>$/, '')

    expect(css).not.toContain('body { display: none }')
    expect(css).not.toContain('</style>')
    expect(css).not.toContain('</')
    expect(css).not.toContain('<script>')
    expect(css.match(/(?<!\\)\}/g)?.length).toBe(2)
    expect(css).toContain('--color-de1\\:\\ red\\;\\ \\}')
  })

  it('leaves plain series keys readable by var(--color-key)', () => {
    const markup = renderToStaticMarkup(<ChartStyle id="chart-t" config={{ usage: { label: 'Usage', color: 'hsl(var(--chart-1))' } }} />)
    expect(markup).toContain('--color-usage: hsl(var(--chart-1));')
  })
})
