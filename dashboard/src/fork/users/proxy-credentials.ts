import type { UseFormReturn } from 'react-hook-form'
import type { UserFormValues } from '@/features/users/forms/user-form'

export type ProxySettingsSection = Record<string, unknown>
export type ProxySettingsValues = Record<string, ProxySettingsSection | undefined>

export type CredentialGenerators = {
  uuid: () => string
  password: () => string
  wireguardKeyPair: () => { privateKey: string; publicKey: string }
  openvpnPassword: () => string
  mtprotoSecret: () => string
  l2tpPassword: () => string
}

export type ProxySettingsForm = Pick<UseFormReturn<UserFormValues>, 'setValue' | 'getValues'>

const sectionOf = (settings: ProxySettingsValues | undefined, protocol: string): ProxySettingsSection => ({ ...(settings?.[protocol] ?? {}) })

export const regenerateAllProxyCredentials = (current: ProxySettingsValues | undefined, generate: CredentialGenerators): ProxySettingsValues => {
  const keyPair = generate.wireguardKeyPair()
  return {
    ...current,
    vmess: { ...sectionOf(current, 'vmess'), id: generate.uuid() },
    vless: { ...sectionOf(current, 'vless'), id: generate.uuid() },
    trojan: { ...sectionOf(current, 'trojan'), password: generate.password() },
    shadowsocks: { ...sectionOf(current, 'shadowsocks'), password: generate.password() },
    hysteria: { ...sectionOf(current, 'hysteria'), auth: generate.uuid() },
    hysteria2: { ...sectionOf(current, 'hysteria2'), password: generate.password() },
    tuic: { ...sectionOf(current, 'tuic'), uuid: generate.uuid(), password: generate.password() },
    wireguard: { ...sectionOf(current, 'wireguard'), private_key: keyPair.privateKey, public_key: keyPair.publicKey },
    openvpn: { ...sectionOf(current, 'openvpn'), password: generate.openvpnPassword() },
    mtproto: { ...sectionOf(current, 'mtproto'), secret: generate.mtprotoSecret() },
    l2tp: { ...sectionOf(current, 'l2tp'), password: generate.l2tpPassword() },
  }
}

const fieldOf = (user: unknown, field: string): unknown => (user !== null && typeof user === 'object' ? Reflect.get(user, field) : undefined)

export const revokeSubscriptionIntoForm = async <T>(revoke: () => Promise<T>, form: ProxySettingsForm): Promise<T> => {
  const revokedUser = await revoke()
  const revokedUsername = fieldOf(revokedUser, 'username')
  if (revokedUsername !== undefined && revokedUsername !== form.getValues('username')) {
    return revokedUser
  }
  const proxySettings = fieldOf(revokedUser, 'proxy_settings')
  form.setValue('proxy_settings', (proxySettings !== null && typeof proxySettings === 'object' ? proxySettings : undefined) as UserFormValues['proxy_settings'], { shouldDirty: false })
  return revokedUser
}
