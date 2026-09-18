import { fetcher } from '@/service/http'
import { useQuery } from '@tanstack/react-query'
import { useCallback, useMemo } from 'react'
import { useTranslation } from 'react-i18next'

export type CapabilityReason = 'node_disconnected' | 'core_not_xray' | 'node_outdated' | 'pre_routed' | 'no_nodes'

export type CapabilityNode = {
  id: number
  name: string
  core_config_id: number | null
  core_type: string | null
  node_version: string | null
  status: string | null
  supported: boolean
  reason: CapabilityReason | null
}

export type CapabilityInbound = {
  tag: string
  supported: boolean
  supported_node_ids: number[]
  unsupported_node_ids: number[]
  reason: CapabilityReason | null
}

export type Capability = { nodes: CapabilityNode[]; inbound_tags: CapabilityInbound[] }

export type NodeSupport = { supported: boolean; reason: CapabilityReason | null }

export type TagSupport = {
  supported: boolean
  reason: CapabilityReason | null
  supportedNodeIds: number[]
  unsupportedNodeIds: number[]
}

export type CapabilityIndex = {
  available: boolean
  nodeSupport: (id: number) => NodeSupport
  tagSupport: (tag: string) => TagSupport
}

const REASONS: CapabilityReason[] = ['node_disconnected', 'core_not_xray', 'node_outdated', 'pre_routed', 'no_nodes']

const OPEN_NODE: NodeSupport = { supported: true, reason: null }
const OPEN_TAG: TagSupport = { supported: true, reason: null, supportedNodeIds: [], unsupportedNodeIds: [] }

export function asCapabilityReason(value: unknown): CapabilityReason | null {
  return typeof value === 'string' && (REASONS as string[]).includes(value) ? (value as CapabilityReason) : null
}

function asText(value: unknown): string | null {
  return typeof value === 'string' && value.length > 0 ? value : null
}

function asIds(value: unknown): number[] {
  return Array.isArray(value) ? value.filter((x): x is number => typeof x === 'number') : []
}

function parseNode(raw: unknown): CapabilityNode | null {
  if (!raw || typeof raw !== 'object') return null
  const item = raw as Record<string, unknown>
  if (typeof item.id !== 'number') return null
  return {
    id: item.id,
    name: asText(item.name) ?? `#${item.id}`,
    core_config_id: typeof item.core_config_id === 'number' ? item.core_config_id : null,
    core_type: asText(item.core_type),
    node_version: asText(item.node_version),
    status: asText(item.status),
    supported: item.supported !== false,
    reason: asCapabilityReason(item.reason),
  }
}

function parseInbound(raw: unknown): CapabilityInbound | null {
  if (!raw || typeof raw !== 'object') return null
  const item = raw as Record<string, unknown>
  const tag = asText(item.tag)
  if (tag === null) return null
  return {
    tag,
    supported: item.supported !== false,
    supported_node_ids: asIds(item.supported_node_ids),
    unsupported_node_ids: asIds(item.unsupported_node_ids),
    reason: asCapabilityReason(item.reason),
  }
}

export function parseCapability(raw: unknown): Capability | null {
  if (!raw || typeof raw !== 'object') return null
  const data = raw as Record<string, unknown>
  if (!Array.isArray(data.nodes) || !Array.isArray(data.inbound_tags)) return null
  return {
    nodes: data.nodes.map(parseNode).filter((node): node is CapabilityNode => node !== null),
    inbound_tags: data.inbound_tags.map(parseInbound).filter((tag): tag is CapabilityInbound => tag !== null),
  }
}

export function useContentFilterCapability(base: string) {
  return useQuery({
    queryKey: ['content-filter', 'capability'],
    queryFn: () =>
      fetcher<unknown>(`${base}/capability`)
        .then(parseCapability)
        .catch(() => null),
    staleTime: 60_000,
    retry: false,
  })
}

export function useCapabilityIndex(capability: Capability | null | undefined): CapabilityIndex {
  return useMemo<CapabilityIndex>(() => {
    if (!capability) {
      return { available: false, nodeSupport: () => OPEN_NODE, tagSupport: () => OPEN_TAG }
    }
    const nodes = new Map<number, NodeSupport>(
      capability.nodes.map(node => [node.id, { supported: node.supported, reason: node.reason }]),
    )
    const tags = new Map<string, TagSupport>(
      capability.inbound_tags.map(tag => [
        tag.tag,
        {
          supported: tag.supported,
          reason: tag.reason,
          supportedNodeIds: tag.supported_node_ids,
          unsupportedNodeIds: tag.unsupported_node_ids,
        },
      ]),
    )
    return {
      available: true,
      nodeSupport: id => nodes.get(id) ?? OPEN_NODE,
      tagSupport: tag => tags.get(tag) ?? OPEN_TAG,
    }
  }, [capability])
}

export function useCapabilityReasonText() {
  const { t } = useTranslation()
  return useCallback(
    (reason: CapabilityReason | null | undefined): string | null => {
      switch (reason) {
        case 'node_disconnected':
          return t('contentFilter.reason.nodeDisconnected', { defaultValue: 'node is disconnected' })
        case 'core_not_xray':
          return t('contentFilter.reason.coreNotXray', { defaultValue: 'this core is not Xray' })
        case 'node_outdated':
          return t('contentFilter.reason.nodeOutdated', { defaultValue: 'core is not up to date' })
        case 'pre_routed':
          return t('contentFilter.reason.preRouted', {
            defaultValue: "this endpoint's traffic is already routed before the filter",
          })
        case 'no_nodes':
          return t('contentFilter.reason.noNodes', { defaultValue: 'no node carries this endpoint' })
        default:
          return null
      }
    },
    [t],
  )
}
