import { afterEach, beforeEach, describe, expect, it } from 'bun:test'

import { getHosts } from '@/service/api'
import { orvalFetcher } from '@/service/http'

const realFetch = globalThis.fetch
let seenSignals: (AbortSignal | null | undefined)[] = []

beforeEach(() => {
  seenSignals = []
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
    const signal = init?.signal ?? (input instanceof Request ? input.signal : undefined)
    seenSignals.push(signal)
    if (signal?.aborted) throw new DOMException('The operation was aborted.', 'AbortError')
    return new Response('[]', { status: 200, headers: { 'content-type': 'application/json' } })
  }) as typeof fetch
})

afterEach(() => {
  globalThis.fetch = realFetch
})

describe('orvalFetcher', () => {
  it('hands the caller abort signal to the transport', async () => {
    const controller = new AbortController()
    await orvalFetcher('/api/hosts', { method: 'GET', signal: controller.signal })
    expect(seenSignals).toHaveLength(1)
    expect(seenSignals[0]).toBe(controller.signal)
  })

  it('a query cancelled by react-query does not complete at the transport', async () => {
    const controller = new AbortController()
    controller.abort()
    await expect(getHosts(undefined, { signal: controller.signal })).rejects.toThrow()
    expect(seenSignals[0]?.aborted).toBe(true)
  })
})
