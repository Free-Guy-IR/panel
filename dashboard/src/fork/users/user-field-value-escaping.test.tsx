import { describe, expect, it } from 'bun:test'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import i18next from 'i18next'
import { renderToStaticMarkup } from 'react-dom/server'
import { I18nextProvider } from 'react-i18next'

import { UserFieldValue } from '@/features/users/components/user-field-value'
import type { UserListItem } from '@/service/api'

const dashboardRoot = join(import.meta.dir, '..', '..', '..')
const englishResources = JSON.parse(readFileSync(join(dashboardRoot, 'public', 'statics', 'locales', 'en.json'), 'utf8'))

const IMG_PAYLOAD = '<img src=x onerror="alert(1)">'
const SCRIPT_PAYLOAD = '<script>alert(1)</script>'

const makeI18n = async () => {
  const instance = i18next.createInstance()
  await instance.init({
    lng: 'en',
    fallbackLng: 'en',
    resources: { en: { translation: englishResources } },
    interpolation: { escapeValue: true },
  })
  return instance
}

const baseUser = (overrides: Partial<UserListItem>) =>
  ({
    id: 1,
    username: 'probe',
    status: 'active',
    used_traffic: 0,
    lifetime_used_traffic: 0,
    created_at: '2026-01-01T00:00:00',
    ...overrides,
  }) as unknown as UserListItem

const renderField = async (field: Parameters<typeof UserFieldValue>[0]['field'], user: UserListItem, groupNames?: ReadonlyMap<number, string>) => {
  const instance = await makeI18n()
  return renderToStaticMarkup(
    <I18nextProvider i18n={instance}>
      <UserFieldValue user={user} field={field} groupNames={groupNames} />
    </I18nextProvider>,
  )
}

describe('optional user column values reach the DOM as text', () => {
  it('escapes a note that carries an img payload, in the cell and in its title', async () => {
    const html = await renderField('note', baseUser({ note: IMG_PAYLOAD }))

    expect(html).not.toContain('<img')
    expect(html).toContain('&lt;img src=x onerror=&quot;alert(1)&quot;&gt;')
    expect(html).toContain('title="&lt;img src=x onerror=&quot;alert(1)&quot;&gt;"')
  })

  it('escapes a group name that carries a script payload', async () => {
    const groupNames = new Map<number, string>([[7, SCRIPT_PAYLOAD]])
    const html = await renderField('groups', baseUser({ group_ids: [7] }), groupNames)

    expect(html).not.toContain('<script')
    expect(html).not.toContain('</script>')
    expect(html).toContain('&lt;script&gt;alert(1)&lt;/script&gt;')
  })

  it('escapes a data limit reset strategy that is not a known key', async () => {
    const html = await renderField('data_limit_reset_strategy', baseUser({ data_limit_reset_strategy: IMG_PAYLOAD as UserListItem['data_limit_reset_strategy'] }))

    expect(html).not.toContain('<img')
  })
})
