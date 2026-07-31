export const DEFAULT_MTPROTO_CORE_CONFIG: Record<string, unknown> = {
  instances: [
    {
      tag: 'MTProto',
      port: 443,
      fake_tls_domain: '',
    },
  ],
}

export const DEFAULT_MTPROTO_CORE_CONFIG_JSON = JSON.stringify(DEFAULT_MTPROTO_CORE_CONFIG, null, 2)
