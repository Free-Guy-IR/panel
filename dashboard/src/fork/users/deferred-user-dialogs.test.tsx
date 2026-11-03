import { describe, expect, it } from 'bun:test'
import { readFileSync } from 'node:fs'
import { createInstance } from 'i18next'
import type { ReactElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { I18nextProvider } from 'react-i18next'
import { DeferredDialog } from '@/components/common/deferred-dialog'

const noop = () => {}
const i18n = createInstance()
await i18n.init({ lng: 'en', resources: { en: { translation: {} } }, initImmediate: false, react: { useSuspense: false } })
const render = (element: ReactElement) => renderToStaticMarkup(<I18nextProvider i18n={i18n}>{element}</I18nextProvider>)
const source = (path: string) => readFileSync(new URL(`../../${path}`, import.meta.url), 'utf8')

describe('deferred user dialogs', () => {
  it('renders nothing for a dialog that was never opened', () => {
    const markup = render(
      <DeferredDialog open={false} onOpenChange={noop}>
        <span>user-modal-body</span>
      </DeferredDialog>,
    )
    expect(markup).toBe('')
  })

  it('renders the dialog body once it is open', () => {
    const markup = render(
      <DeferredDialog open onOpenChange={noop}>
        <span>user-modal-body</span>
      </DeferredDialog>,
    )
    expect(markup).toContain('user-modal-body')
  })

  it('opens the user, usage and subscription dialogs only through their lazy wrappers', () => {
    const importers = [
      'features/users/components/action-buttons.tsx',
      'features/users/components/users-table.tsx',
      'pages/_dashboard._index.tsx',
      'pages/_dashboard.users.tsx',
    ]
    const eager = /from '[^']*\/(user-modal|usage-modal|subscription-modal)'/
    for (const file of importers) {
      expect({ file, eager: eager.test(source(file)) }).toEqual({ file, eager: false })
    }
    expect(source('features/users/components/action-buttons.tsx')).toContain("from '@/features/users/dialogs/user-modal.lazy'")
    expect(source('features/users/components/action-buttons.tsx')).toContain("from '@/features/subscriptions/dialogs/subscription-modal.lazy'")
  })
})
