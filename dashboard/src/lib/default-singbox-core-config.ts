export const DEFAULT_SING_BOX_CORE_CONFIG: Record<string, unknown> = {
  log: {
    level: 'info',
  },
  inbounds: [
    {
      type: 'hysteria2',
      tag: 'Hysteria2',
      listen: '::',
      listen_port: 8443,
      users: [],
      tls: {
        enabled: true,
        server_name: '',
        certificate_path: '',
        key_path: '',
      },
    },
  ],
  outbounds: [
    {
      type: 'direct',
    },
  ],
}

export const DEFAULT_SING_BOX_CORE_CONFIG_JSON = JSON.stringify(DEFAULT_SING_BOX_CORE_CONFIG, null, 2)
