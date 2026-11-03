import { afterEach, beforeEach, describe, expect, it } from 'bun:test'
import { renderToStaticMarkup } from 'react-dom/server'

import { useClipboard } from '@/hooks/use-clipboard'

type CopyListener = (event: { clipboardData: { setData: (type: string, value: string) => void } | null; preventDefault: () => void }) => void

const saved = { document: globalThis.document, window: globalThis.window, navigator: globalThis.navigator }
let clipboard: string | null
let focusedSelection: string

const installInsecureFocusTrappedPage = () => {
  clipboard = null
  focusedSelection = ''
  const listeners = new Set<CopyListener>()
  const fakeDocument = {
    body: { appendChild: () => undefined, removeChild: () => undefined },
    createElement: () => ({ style: {}, value: '', setAttribute: () => undefined, focus: () => undefined, select: () => undefined }),
    addEventListener: (type: string, listener: CopyListener) => type === 'copy' && listeners.add(listener),
    removeEventListener: (type: string, listener: CopyListener) => type === 'copy' && listeners.delete(listener),
    execCommand: (command: string) => {
      if (command !== 'copy') return false
      let prevented = false
      const pending = { value: focusedSelection }
      for (const listener of listeners) {
        listener({ clipboardData: { setData: (_type, value) => (pending.value = value) }, preventDefault: () => (prevented = true) })
      }
      clipboard = prevented ? pending.value : focusedSelection
      return true
    },
  }
  Object.defineProperty(globalThis, 'document', { value: fakeDocument, configurable: true, writable: true })
  Object.defineProperty(globalThis, 'window', { value: { isSecureContext: false, setTimeout: () => 0, clearTimeout: () => undefined }, configurable: true, writable: true })
  Object.defineProperty(globalThis, 'navigator', { value: {}, configurable: true, writable: true })
}

const captureCopy = () => {
  let copy: ((text: string) => Promise<boolean>) | undefined
  const Probe = () => {
    copy = useClipboard().copy
    return null
  }
  renderToStaticMarkup(<Probe />)
  return copy!
}

beforeEach(installInsecureFocusTrappedPage)

afterEach(() => {
  Object.defineProperty(globalThis, 'document', { value: saved.document, configurable: true, writable: true })
  Object.defineProperty(globalThis, 'window', { value: saved.window, configurable: true, writable: true })
  Object.defineProperty(globalThis, 'navigator', { value: saved.navigator, configurable: true, writable: true })
})

describe('useClipboard on an insecure context inside a focus trap', () => {
  it('reports success only when the text really reached the clipboard', async () => {
    const copy = captureCopy()
    const ok = await copy('vless://user-link')
    expect(clipboard === 'vless://user-link').toBe(ok)
    expect(clipboard).toBe('vless://user-link')
  })

  it('keeps multiline content intact', async () => {
    const copy = captureCopy()
    expect(await copy('line one\nline two')).toBe(true)
    expect(clipboard).toBe('line one\nline two')
  })
})
