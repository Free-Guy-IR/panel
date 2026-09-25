import { describe, expect, it } from 'bun:test'
import { readFileSync } from 'node:fs'
import { createFormControl } from 'react-hook-form'

import type { UserFormValues } from '@/features/users/forms/user-form'
import { type CredentialGenerators, type ProxySettingsValues, regenerateAllProxyCredentials, revokeSubscriptionIntoForm } from './proxy-credentials'

const storedProxySettings = (): ProxySettingsValues => ({
  vmess: { id: 'old-vmess-id' },
  vless: { id: 'old-vless-id', flow: 'xtls-rprx-vision' },
  trojan: { password: 'old-trojan-password' },
  shadowsocks: { password: 'old-shadowsocks-password', method: 'aes-128-gcm' },
  hysteria: { auth: 'old-hysteria-auth' },
  hysteria2: { password: 'old-hysteria2-password' },
  tuic: { uuid: 'old-tuic-uuid', password: 'old-tuic-password' },
  wireguard: { private_key: 'old-wg-private', public_key: 'old-wg-public', peer_ips: ['peer-a', 'peer-b'] },
  openvpn: { password: 'old-openvpn-password' },
  mtproto: { secret: 'old-mtproto-secret' },
  l2tp: { password: 'old-l2tp-password' },
})

const CREDENTIAL_PATHS = [
  ['vmess', 'id'],
  ['vless', 'id'],
  ['trojan', 'password'],
  ['shadowsocks', 'password'],
  ['hysteria', 'auth'],
  ['hysteria2', 'password'],
  ['tuic', 'uuid'],
  ['tuic', 'password'],
  ['wireguard', 'private_key'],
  ['wireguard', 'public_key'],
  ['openvpn', 'password'],
  ['mtproto', 'secret'],
  ['l2tp', 'password'],
] as const

const countingGenerators = (): CredentialGenerators => {
  let counter = 0
  const next = (kind: string) => {
    counter += 1
    return `new-${kind}-${counter}`
  }
  return {
    uuid: () => next('uuid'),
    password: () => next('password'),
    wireguardKeyPair: () => {
      const privateKey = next('wg-private')
      return { privateKey, publicKey: `${privateKey}-public` }
    },
    openvpnPassword: () => next('openvpn'),
    mtprotoSecret: () => next('mtproto'),
    l2tpPassword: () => next('l2tp'),
  }
}

const valueAt = (settings: ProxySettingsValues | undefined, [protocol, field]: readonly [string, string]) => settings?.[protocol]?.[field]

const formWith = (proxySettings: ProxySettingsValues) =>
  createFormControl<UserFormValues>({
    defaultValues: {
      username: 'alice',
      group_ids: [1],
      data_limit: 0,
      proxy_settings: proxySettings as UserFormValues['proxy_settings'],
    },
  })

const submittedProxySettings = async (form: ReturnType<typeof formWith>) => {
  let submitted: UserFormValues | undefined
  await form.handleSubmit(values => {
    submitted = values
  })()
  return submitted?.proxy_settings as ProxySettingsValues | undefined
}

describe('regenerate all proxy credentials', () => {
  it('replaces every credential the user holds, OpenVPN included', () => {
    const stored = storedProxySettings()
    const regenerated = regenerateAllProxyCredentials(stored, countingGenerators())
    for (const path of CREDENTIAL_PATHS) {
      const value = valueAt(regenerated, path)
      expect({ path: path.join('.'), fresh: typeof value === 'string' && value.startsWith('new-') }).toEqual({ path: path.join('.'), fresh: true })
      expect(value).not.toBe(valueAt(stored, path))
    }
    expect(regenerated.openvpn?.password).toStartWith('new-openvpn-')
    expect(regenerated.mtproto?.secret).toStartWith('new-mtproto-')
    expect(regenerated.l2tp?.password).toStartWith('new-l2tp-')
    expect(regenerated.wireguard?.public_key).toBe(`${regenerated.wireguard?.private_key}-public`)
  })

  it('keeps the settings that are not credentials', () => {
    const regenerated = regenerateAllProxyCredentials(storedProxySettings(), countingGenerators())
    expect(regenerated.vless?.flow).toBe('xtls-rprx-vision')
    expect(regenerated.shadowsocks?.method).toBe('aes-128-gcm')
    expect(regenerated.wireguard?.peer_ips).toEqual(['peer-a', 'peer-b'])
  })

  it('issues a fresh OpenVPN, L2TP and MTProto secret even when the form holds none', () => {
    const stored = { ...storedProxySettings(), openvpn: { password: null }, mtproto: {}, l2tp: { password: '' } }
    const regenerated = regenerateAllProxyCredentials(stored, countingGenerators())
    const entitled = [
      ['openvpn', 'password'],
      ['l2tp', 'password'],
      ['mtproto', 'secret'],
    ] as const
    for (const path of entitled) {
      const value = valueAt(regenerated, path)
      expect({ path: path.join('.'), fresh: typeof value === 'string' && value.startsWith('new-') }).toEqual({ path: path.join('.'), fresh: true })
    }
  })

  it('rotates a credential for every protocol the API proxy table defines', () => {
    const apiSource = readFileSync(new URL('../../service/api/index.ts', import.meta.url), 'utf8')
    const proxyTable = apiSource.match(/export interface ProxyTable \{([^}]*)\}/)
    expect(proxyTable).not.toBeNull()
    const apiProtocols = [...(proxyTable?.[1] ?? '').matchAll(/^\s*(\w+)\?:/gm)].map(match => match[1]).sort()
    expect(apiProtocols.length).toBeGreaterThan(0)
    expect([...new Set<string>(CREDENTIAL_PATHS.map(([protocol]) => protocol))].sort()).toEqual(apiProtocols)
    const regenerated = regenerateAllProxyCredentials(undefined, countingGenerators())
    for (const path of CREDENTIAL_PATHS) {
      const value = valueAt(regenerated, path)
      expect({ path: path.join('.'), fresh: typeof value === 'string' && value.startsWith('new-') }).toEqual({ path: path.join('.'), fresh: true })
    }
  })

  it('does not mutate the settings it was given', () => {
    const stored = storedProxySettings()
    regenerateAllProxyCredentials(stored, countingGenerators())
    expect(stored).toEqual(storedProxySettings())
  })

  it('reaches the save payload with a fresh OpenVPN password', async () => {
    const form = formWith(storedProxySettings())
    form.setValue('proxy_settings', regenerateAllProxyCredentials(storedProxySettings(), countingGenerators()) as UserFormValues['proxy_settings'])
    const submitted = await submittedProxySettings(form)
    expect(submitted?.openvpn?.password).toStartWith('new-openvpn-')
    expect(submitted?.shadowsocks?.method).toBe('aes-128-gcm')
  })
})

describe('revoke subscription inside the user modal', () => {
  const revokedProxySettings: ProxySettingsValues = {
    ...storedProxySettings(),
    vmess: { id: 'revoked-vmess-id' },
    trojan: { password: 'revoked-trojan-password' },
    openvpn: { password: 'revoked-openvpn-password' },
    mtproto: { secret: 'revoked-mtproto-secret' },
  }

  it('makes a later save send the revoked credentials, not the pre-revoke ones', async () => {
    const form = formWith(storedProxySettings())
    const revokedUser = { id: 7, username: 'alice', proxy_settings: revokedProxySettings }
    const result = await revokeSubscriptionIntoForm(async () => revokedUser, form)
    expect(result).toBe(revokedUser)
    const submitted = await submittedProxySettings(form)
    expect(submitted).toEqual(revokedProxySettings)
    expect(submitted?.vmess?.id).not.toBe('old-vmess-id')
  })

  it('drops the stale credentials when the revoke response carries none', async () => {
    const form = formWith(storedProxySettings())
    await revokeSubscriptionIntoForm(async () => ({ id: 7, username: 'alice' }), form)
    expect(await submittedProxySettings(form)).toBeUndefined()
  })

  it('leaves another user alone when the form moved on before the revoke returned', async () => {
    const form = formWith(storedProxySettings())
    const bobSettings: ProxySettingsValues = { ...storedProxySettings(), vmess: { id: 'bob-vmess-id' }, openvpn: { password: 'bob-openvpn-password' } }
    await revokeSubscriptionIntoForm(async () => {
      form.reset({ username: 'bob', group_ids: [2], data_limit: 0, proxy_settings: bobSettings as UserFormValues['proxy_settings'] })
      return { id: 7, username: 'alice', proxy_settings: revokedProxySettings }
    }, form)
    expect(await submittedProxySettings(form)).toEqual(bobSettings)
  })

  it('leaves the form untouched when the revoke fails', async () => {
    const form = formWith(storedProxySettings())
    const failure = new Error('revoke failed')
    await expect(
      revokeSubscriptionIntoForm(async () => {
        throw failure
      }, form),
    ).rejects.toBe(failure)
    expect(await submittedProxySettings(form)).toEqual(storedProxySettings())
  })
})
