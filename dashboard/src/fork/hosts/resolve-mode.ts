export const FORK_HOST_PROTOCOLS = ['wireguard', 'openvpn', 'mtproto', 'l2tp'] as const

export type ForkHostProtocol = (typeof FORK_HOST_PROTOCOLS)[number]

export type HostProtocolMode = 'xray' | ForkHostProtocol

export function isForkHostProtocol(protocol?: string | null): protocol is ForkHostProtocol {
  return protocol === 'wireguard' || protocol === 'openvpn' || protocol === 'mtproto' || protocol === 'l2tp'
}

export function resolveHostMode(protocol?: string | null): HostProtocolMode {
  return isForkHostProtocol(protocol) ? protocol : 'xray'
}
