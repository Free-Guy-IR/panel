import type { LucideIcon } from 'lucide-react'
import { ArrowDownToLine, ArrowUpFromLine, Braces, Cable, FlaskConical, Globe, Link2, ListTree, Scale, ScrollText, ShieldCheck, Waypoints } from 'lucide-react'
import type { L2tpCoreSection, MtCoreSection, OvCoreSection, SbCoreSection, WgCoreSection, XrayCoreSection } from '@/features/core-editor/state/core-editor-store'

export type XraySectionNavItem = {
  id: XrayCoreSection
  labelKey: string
  defaultLabel: string
  icon: LucideIcon
}

export type WgSectionNavItem = {
  id: WgCoreSection
  labelKey: string
  defaultLabel: string
  icon: LucideIcon
}

export type SbSectionNavItem = {
  id: SbCoreSection
  labelKey: string
  defaultLabel: string
  icon: LucideIcon
}

export type OvSectionNavItem = {
  id: OvCoreSection
  labelKey: string
  defaultLabel: string
  icon: LucideIcon
}

export type MtSectionNavItem = {
  id: MtCoreSection
  labelKey: string
  defaultLabel: string
  icon: LucideIcon
}

export type L2tpSectionNavItem = {
  id: L2tpCoreSection
  labelKey: string
  defaultLabel: string
  icon: LucideIcon
}

export const XRAY_CORE_SECTION_NAV: XraySectionNavItem[] = [
  { id: 'inbounds', labelKey: 'coreEditor.section.inbounds', defaultLabel: 'Inbounds', icon: ArrowDownToLine },
  { id: 'outbounds', labelKey: 'coreEditor.section.outbounds', defaultLabel: 'Outbounds', icon: ArrowUpFromLine },
  { id: 'routing', labelKey: 'coreEditor.section.routing', defaultLabel: 'Routing', icon: Waypoints },
  { id: 'balancers', labelKey: 'coreEditor.section.balancers', defaultLabel: 'Balancers', icon: Scale },
  { id: 'dns', labelKey: 'coreEditor.section.dns', defaultLabel: 'DNS', icon: Globe },
  { id: 'bindings', labelKey: 'coreEditor.section.bindings', defaultLabel: 'Bindings', icon: Link2 },
  { id: 'advanced', labelKey: 'coreEditor.section.advanced', defaultLabel: 'Advanced', icon: Braces },
]

export const WG_CORE_SECTION_NAV: WgSectionNavItem[] = [
  { id: 'interface', labelKey: 'coreEditor.section.interface', defaultLabel: 'Interface', icon: Cable },
  { id: 'advanced', labelKey: 'coreEditor.section.advanced', defaultLabel: 'Advanced', icon: Braces },
]

export const SING_BOX_CORE_SECTION_NAV: SbSectionNavItem[] = [
  { id: 'inbounds', labelKey: 'coreEditor.section.inbounds', defaultLabel: 'Inbounds', icon: ArrowDownToLine },
  { id: 'outbounds', labelKey: 'coreEditor.section.outbounds', defaultLabel: 'Outbounds', icon: ArrowUpFromLine },
  { id: 'balancers', labelKey: 'coreEditor.section.balancers', defaultLabel: 'Balancers', icon: Scale },
  { id: 'route', labelKey: 'coreEditor.section.route', defaultLabel: 'Route', icon: Waypoints },
  { id: 'ruleSets', labelKey: 'coreEditor.section.ruleSets', defaultLabel: 'Rule Sets', icon: ListTree },
  { id: 'dns', labelKey: 'coreEditor.section.dns', defaultLabel: 'DNS', icon: Globe },
  { id: 'bindings', labelKey: 'coreEditor.section.bindings', defaultLabel: 'Bindings', icon: Link2 },
  { id: 'experimental', labelKey: 'coreEditor.section.experimental', defaultLabel: 'Experimental', icon: FlaskConical },
  { id: 'advanced', labelKey: 'coreEditor.section.advanced', defaultLabel: 'Advanced', icon: Braces },
]

export const OPENVPN_CORE_SECTION_NAV: OvSectionNavItem[] = [
  { id: 'instances', labelKey: 'coreEditor.section.instances', defaultLabel: 'Instances', icon: ScrollText },
  { id: 'pki', labelKey: 'coreEditor.section.pki', defaultLabel: 'PKI', icon: ShieldCheck },
  { id: 'advanced', labelKey: 'coreEditor.section.advanced', defaultLabel: 'Advanced', icon: Braces },
]

export const MTPROTO_CORE_SECTION_NAV: MtSectionNavItem[] = [
  { id: 'instances', labelKey: 'coreEditor.section.instances', defaultLabel: 'Instances', icon: ScrollText },
  { id: 'advanced', labelKey: 'coreEditor.section.advanced', defaultLabel: 'Advanced', icon: Braces },
]

export const L2TP_CORE_SECTION_NAV: L2tpSectionNavItem[] = [
  { id: 'settings', labelKey: 'coreEditor.section.settings', defaultLabel: 'Settings', icon: Cable },
  { id: 'advanced', labelKey: 'coreEditor.section.advanced', defaultLabel: 'Advanced', icon: Braces },
]
