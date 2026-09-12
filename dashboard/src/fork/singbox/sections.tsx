import type { ComponentType } from 'react'
import { SINGBOX_BALANCER_OUTBOUND_TYPES } from '@pasarguard/singbox-config-kit'
import type { SectionHeaderAddPulse } from '@/features/core-editor/hooks/use-section-header-add-pulse'
import type { SbCoreSection } from '@/features/core-editor/state/core-editor-store'
import { SbBindingsSection } from './sb-bindings-section'
import { SbDnsSection } from './sb-dns-section'
import { SbExperimentalSection } from './sb-experimental-section'
import { SbOutboundsSection } from './sb-outbounds-section'
import { SbRouteSection } from './sb-route-section'
import { SbRuleSetsSection } from './sb-rulesets-section'
import { registerComponent } from '@/fork/registry'

export type ForkSingboxSectionProps = {
  headerAddPulse?: SectionHeaderAddPulse
  headerAddEpoch?: number
}

type ForkSingboxSectionRenderer = ComponentType<ForkSingboxSectionProps>

function SbForkOutboundsSection(props: ForkSingboxSectionProps) {
  return <SbOutboundsSection {...props} sectionId="outbounds" />
}

function SbForkBalancersSection(props: ForkSingboxSectionProps) {
  return <SbOutboundsSection {...props} sectionId="balancers" onlyTypes={SINGBOX_BALANCER_OUTBOUND_TYPES} />
}

export const forkSingboxSections: Partial<Record<SbCoreSection, ForkSingboxSectionRenderer>> = {
  outbounds: SbForkOutboundsSection,
  balancers: SbForkBalancersSection,
  route: SbRouteSection,
  ruleSets: SbRuleSetsSection,
  dns: SbDnsSection,
  bindings: SbBindingsSection,
  experimental: SbExperimentalSection,
}

export function registerForkSingboxSections() {
  for (const [id, component] of Object.entries(forkSingboxSections)) {
    if (component) registerComponent('singbox-section', id, component)
  }
  return forkSingboxSections
}

registerForkSingboxSections()
