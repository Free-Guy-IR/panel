import { registerHostSection } from "../registry"
import { HOST_PROTOCOL_REGISTRY as _hostProtocolRegistry } from "./registry"

export { HostProtocolExtraSection } from "./extra-section"
export { applyHostProtocolPayload } from "./payload"
export {
  getHostProtocolEntry,
  getHostProtocolEntryByMode,
  hasHostProtocolExtraSection,
  HOST_PROTOCOL_REGISTRY,
  resolveHostMode,
} from "./registry"
export { isForkHostProtocol } from "./resolve-mode"
export type { ForkHostProtocol, HostProtocolMode } from "./resolve-mode"
export type { ExtraSectionProps, HostProtocolEntry } from "./types"
export { useHostProtocolFormEffects } from "./use-protocol-effects"

for (const entry of _hostProtocolRegistry) {
  registerHostSection(entry.id, entry.ExtraSection)
}
