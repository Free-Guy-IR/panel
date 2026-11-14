export const DEFAULT_OPENVPN_CORE_CONFIG: Record<string, unknown> = {
  instances: [
    {
      tag: 'OpenVPN',
      protocol: 'udp',
      port: 1194,
      network: '10.8.0.0/24',
      cipher: 'AES-256-GCM',
      auth: 'SHA256',
      keepalive: '10 60',
      max_clients: 500,
      dns_servers: ['1.1.1.1', '8.8.8.8'],
      redirect_gateway: true,
      duplicate_cn: true,
      verb: 3,
    },
  ],
  pki: {
    ca_cert: '',
    server_cert: '',
    server_key: '',
    tls_crypt_key: '',
  },
}

export const DEFAULT_OPENVPN_CORE_CONFIG_JSON = JSON.stringify(DEFAULT_OPENVPN_CORE_CONFIG, null, 2)
