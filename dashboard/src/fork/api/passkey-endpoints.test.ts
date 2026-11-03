import { afterEach, beforeEach, describe, expect, it } from 'bun:test'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'

import * as api from '@/service/api'

type CapturedRequest = { url: string; method: string; body: unknown }

const realFetch = globalThis.fetch
let captured: CapturedRequest[] = []

beforeEach(() => {
  captured = []
  const store = new Map<string, string>()
  globalThis.localStorage = {
    getItem: (key: string) => store.get(key) ?? null,
    setItem: (key: string, value: string) => void store.set(key, value),
    removeItem: (key: string) => void store.delete(key),
    clear: () => store.clear(),
    key: () => null,
    length: 0,
  } as Storage
  globalThis.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
    const request = input instanceof Request ? input : new Request(new URL(String(input), 'http://panel.test'), init)
    const text = init?.body ? String(init.body) : ''
    captured.push({ url: new URL(request.url).pathname, method: (init?.method ?? request.method).toUpperCase(), body: text ? JSON.parse(text) : undefined })
    return new Response('{}', { status: 200, headers: { 'content-type': 'application/json' } })
  }) as typeof fetch
})

afterEach(() => {
  globalThis.fetch = realFetch
})

const callApi = async (name: string, ...args: unknown[]) => {
  const fn = (api as Record<string, unknown>)[name]
  expect(typeof fn).toBe('function')
  await (fn as (...a: unknown[]) => Promise<unknown>)(...args)
  expect(captured).toHaveLength(1)
  return captured[0]
}

describe('passkey API client', () => {
  it('deletes another admin passkey through the admin-scoped route only', async () => {
    expect(await callApi('deleteAdminPasskey', 7, 9)).toEqual({ url: '/api/admin/passkey/admins/7/9', method: 'DELETE', body: undefined })
  })

  it('deletes an own passkey through the self route', async () => {
    expect(await callApi('deletePasskey', 9)).toEqual({ url: '/api/admin/passkey/9', method: 'DELETE', body: undefined })
  })

  it('lists and registers passkeys for a target admin', async () => {
    expect(await callApi('listAdminPasskeys', 7)).toEqual({ url: '/api/admin/passkey/admins/7', method: 'GET', body: undefined })
    captured = []
    expect(await callApi('adminPasskeyRegisterOptions', 7)).toEqual({ url: '/api/admin/passkey/admins/7/register/options', method: 'POST', body: undefined })
    captured = []
    expect(await callApi('adminPasskeyRegisterVerify', 7, { credential: { id: 'c' }, name: 'key' })).toEqual({
      url: '/api/admin/passkey/admins/7/register/verify',
      method: 'POST',
      body: { credential: { id: 'c' }, name: 'key' },
    })
  })

  it('runs the passkey login flow', async () => {
    expect(await callApi('passkeyLoginOptions', { username: 'owner' })).toEqual({ url: '/api/admin/passkey/login/options', method: 'POST', body: { username: 'owner' } })
    captured = []
    expect(await callApi('passkeyLoginVerify', { username: 'owner', credential: { id: 'c' } })).toEqual({
      url: '/api/admin/passkey/login/verify',
      method: 'POST',
      body: { username: 'owner', credential: { id: 'c' } },
    })
  })

  it('every name the dashboard imports from the API client is exported', () => {
    const files = ['src/components/layout/nav-user.tsx', 'src/features/admins/dialogs/admin-modal.tsx', 'src/pages/login.tsx']
    const missing: string[] = []
    for (const file of files) {
      const source = readFileSync(join(import.meta.dir, '..', '..', '..', file), 'utf8')
      for (const match of source.matchAll(/import\s*\{([^}]*)\}\s*from\s*'@\/service\/api'/g)) {
        for (const specifier of match[1].split(',')) {
          const name = specifier.trim().split(/\s+as\s+/)[0]
          if (name && !name.startsWith('type ') && !(name in api)) missing.push(`${file}: ${name}`)
        }
      }
    }
    expect(missing).toEqual([])
  })
})
