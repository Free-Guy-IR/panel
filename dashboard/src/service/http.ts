import { getAuthToken } from '@/utils/authStorage'
import { dateUtils } from '@/utils/dateFormatter'
import { FetchError, FetchOptions, $fetch as ofetch } from 'ofetch'

export const $fetch = ofetch.create({
  baseURL: import.meta.env.VITE_BASE_API,
  onRequest({ options }) {
    const token = getAuthToken()
    options.headers.set('X-Client-Timezone', dateUtils.getSystemTimeZone())
    options.headers.set('X-Client-Timezone-Offset-Minutes', String(-new Date().getTimezoneOffset()))
    if (token) {
      options.headers.set('Authorization', `Bearer ${getAuthToken()}`)
    }
  },
})

export const fetcher = <T>(url: string, ops: FetchOptions<'json'> = {}) => {
  return $fetch<T>(url, ops).catch(e => {
    if (e.status === 401) {
      const url = new URL(window.location.href)
      if (url.hash !== '#/login') {
        url.hash = '#/login'
        window.location.href = url.href
      }
    }
    throw e
  })
}

export const fetch = fetcher

export type ErrorType<Error> = FetchError<{ detail: Error }>
export type BodyType<BodyData> = BodyData

type OvalFetcherParams = FetchOptions<'json'> & {
  url: string
  method: 'GET' | 'POST' | 'PUT' | 'DELETE' | 'PATCH'
  params?: Record<string, unknown>
  data?: FetchOptions<'json'>['body']
}
export const orvalFetcher = async <T>({ url, method, params, data: body }: OvalFetcherParams): Promise<T> => {
  if (method === 'GET') {
    // 1. If we have data in a GET request, it means arguments were shifted or
    // we manually passed data to rescue dropped parameters.
    if (body && typeof body === 'object' && !Array.isArray(body)) {
      params = { ...params, ...(body as Record<string, unknown>) }
      body = undefined
    } else if (body) {
      // If it's not an object (e.g. just a string or array), we can't easily merge it
      // unless we know the key. But for the known cases (status), it's usually an object
      // when passed from the frontend filters.
    }

    // 2. If 'query' is present in params, check if it looks like React Query options.
    if (params && 'query' in params) {
      const queryVal = (params as any).query
      if (queryVal && typeof queryVal === 'object' && ('staleTime' in queryVal || 'gcTime' in queryVal || 'retry' in queryVal)) {
        const { query: _query, ...rest } = params as any
        params = rest
      }
    }
  }

  return fetcher(url, {
    method,
    params,
    body,
  })
}

export default orvalFetcher
