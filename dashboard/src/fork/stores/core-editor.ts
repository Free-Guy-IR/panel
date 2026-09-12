import type { StateCreator } from 'zustand'
import type { CoreKind } from '@pasarguard/core-kit'
import type { SingBoxCoreDraft } from '@pasarguard/singbox-config-kit'
import type { OpenVPNCoreDraft } from '@pasarguard/openvpn-config-kit'
import type { MTProtoCoreDraft } from '@pasarguard/mtproto-config-kit'
import type { L2TPCoreDraft } from '@pasarguard/l2tp-config-kit'
import type { CoreResponse } from '@/service/api'
import { apiCoreTypeToKind } from '@/features/core-editor/kit/core-kind'
import { createNewSingBoxDraft, draftToPersistedConfig as sbDraftToPersistedConfig, singBoxConfigToDraft } from '@/features/core-editor/kit/singbox-adapter'
import { createNewOpenVPNDraft, draftToPersistedConfig as ovDraftToPersistedConfig, openVPNConfigToDraft } from '@/features/core-editor/kit/openvpn-adapter'
import { createNewMTProtoDraft, draftToPersistedConfig as mtDraftToPersistedConfig, mtprotoConfigToDraft } from '@/features/core-editor/kit/mtproto-adapter'
import { createNewL2TPDraft, draftToPersistedConfig as l2tpDraftToPersistedConfig, l2tpConfigToDraft } from '@/features/core-editor/kit/l2tp-adapter'
import {
  isForkCoreKind,
  type ForkCoreEditorSlice,
  type ForkCoreKind,
  type ForkCoreSection,
  type ForkPersistedFields,
} from './core-editor-types'

export type { ForkCoreEditorSlice, ForkCoreKind, ForkCoreSection, ForkPersistedFields, L2tpCoreSection, MtCoreSection, OvCoreSection, SbCoreSection } from './core-editor-types'
export { isForkCoreKind } from './core-editor-types'

type SetFn = (partial: Record<string, unknown>) => void
type GetFn = () => ForkStoreView

export interface ForkStoreView extends ForkCoreEditorSlice {
  kind: CoreKind
  coreId: number | null
  coreName: string
  restartNodes: boolean
  fallbacksInboundTags: string[]
  excludeInboundTags: string[]
  xrayProfile: unknown
  xrayBaseline: unknown
  wgDraft: unknown
  wgBaseline: unknown
  activeSection: string
  dirty: boolean
  monacoJson: string
  monacoDirty: boolean
  xrayImportWarnings: string[]
  serverHydratedConfigJson: string | null
  persistedSnapshot: (ForkPersistedFields & { kind: CoreKind; coreName: string; fallbacksInboundTags: string[]; excludeInboundTags: string[]; xrayProfile: unknown; wgDraft: unknown; activeSection: string; monacoJson: string; xrayImportWarnings: string[]; serverHydratedConfigJson: string | null }) | null
  syncMonacoFromDraft: () => void
}

type UpstreamSlice = {
  initFromCore: (core: CoreResponse, options?: { preserveNavigation?: boolean }) => void
  initNew: (kind: CoreKind, name?: string) => void
  reset: () => void
  markClean: () => void
  discardDraft: () => void
  switchKind: (nextKind: CoreKind) => void
  syncMonacoFromDraft: () => void
  applyMonacoJson: () => { ok: true } | { ok: false; error: string }
}

function cloneSb(d: SingBoxCoreDraft): SingBoxCoreDraft {
  return JSON.parse(JSON.stringify(d)) as SingBoxCoreDraft
}

function cloneOv(d: OpenVPNCoreDraft): OpenVPNCoreDraft {
  return JSON.parse(JSON.stringify(d)) as OpenVPNCoreDraft
}

function cloneMt(d: MTProtoCoreDraft): MTProtoCoreDraft {
  return JSON.parse(JSON.stringify(d)) as MTProtoCoreDraft
}

function cloneL2tp(d: L2TPCoreDraft): L2TPCoreDraft {
  return JSON.parse(JSON.stringify(d)) as L2TPCoreDraft
}

function emptyForkDraftFields(): ForkPersistedFields & {
  sbBaseline: null
  ovBaseline: null
  mtBaseline: null
  l2tpBaseline: null
} {
  return {
    sbDraft: null,
    sbBaseline: null,
    ovDraft: null,
    ovBaseline: null,
    mtDraft: null,
    mtBaseline: null,
    l2tpDraft: null,
    l2tpBaseline: null,
  }
}

function emptyUpstreamDrafts() {
  return {
    xrayProfile: null,
    xrayBaseline: null,
    wgDraft: null,
    wgBaseline: null,
  }
}

function openvpnInitMonaco(draft: OpenVPNCoreDraft): string {
  return JSON.stringify({ instances: draft.instances, pki: { ca_cert: '', server_cert: '', server_key: '', tls_crypt_key: '' } }, null, 2)
}

type Parsed<T> = { ok: true; draft: T } | { ok: false; message: string }

type ForkKindHandler = {
  defaultSection: ForkCoreSection
  overviewSection: ForkCoreSection
  create: () => unknown
  parse: (config: unknown) => Parsed<unknown>
  persist: (draft: unknown) => unknown
  clone: (draft: unknown) => unknown
  initMonaco?: (draft: unknown) => string
  assign: (draft: unknown, baseline: unknown) => Record<string, unknown>
  read: (s: ForkStoreView) => unknown
}

const handlers: Record<ForkCoreKind, ForkKindHandler> = {
  singbox: {
    defaultSection: 'inbounds',
    overviewSection: 'inbounds',
    create: createNewSingBoxDraft,
    parse: singBoxConfigToDraft,
    persist: d => sbDraftToPersistedConfig(d as SingBoxCoreDraft),
    clone: d => cloneSb(d as SingBoxCoreDraft),
    assign: (draft, baseline) => ({ sbDraft: draft, sbBaseline: baseline }),
    read: s => s.sbDraft,
  },
  openvpn: {
    defaultSection: 'instances',
    overviewSection: 'instances',
    create: createNewOpenVPNDraft,
    parse: openVPNConfigToDraft,
    persist: d => ovDraftToPersistedConfig(d as OpenVPNCoreDraft),
    clone: d => cloneOv(d as OpenVPNCoreDraft),
    initMonaco: d => openvpnInitMonaco(d as OpenVPNCoreDraft),
    assign: (draft, baseline) => ({ ovDraft: draft, ovBaseline: baseline }),
    read: s => s.ovDraft,
  },
  mtproto: {
    defaultSection: 'instances',
    overviewSection: 'instances',
    create: createNewMTProtoDraft,
    parse: mtprotoConfigToDraft,
    persist: d => mtDraftToPersistedConfig(d as MTProtoCoreDraft),
    clone: d => cloneMt(d as MTProtoCoreDraft),
    assign: (draft, baseline) => ({ mtDraft: draft, mtBaseline: baseline }),
    read: s => s.mtDraft,
  },
  l2tp: {
    defaultSection: 'settings',
    overviewSection: 'settings',
    create: createNewL2TPDraft,
    parse: l2tpConfigToDraft,
    persist: d => l2tpDraftToPersistedConfig(d as L2TPCoreDraft),
    clone: d => cloneL2tp(d as L2TPCoreDraft),
    assign: (draft, baseline) => ({ l2tpDraft: draft, l2tpBaseline: baseline }),
    read: s => s.l2tpDraft,
  },
}

export function forkDefaultSection(kind: CoreKind): ForkCoreSection | null {
  if (!isForkCoreKind(kind)) return null
  return handlers[kind].defaultSection
}

export function forkNormalizeActiveSection(kind: CoreKind, section: string): string {
  if (!isForkCoreKind(kind)) return section
  if (section === 'overview') return handlers[kind].overviewSection
  return section
}

function monacoForDraft(kind: ForkCoreKind, draft: unknown, fallback?: unknown): string {
  const h = handlers[kind]
  if (h.initMonaco) return h.initMonaco(draft)
  return JSON.stringify(fallback ?? h.persist(draft), null, 2)
}

function captureForkFields(s: ForkStoreView): ForkPersistedFields {
  return {
    sbDraft: s.sbDraft ? cloneSb(s.sbDraft) : null,
    ovDraft: s.ovDraft ? cloneOv(s.ovDraft) : null,
    mtDraft: s.mtDraft ? cloneMt(s.mtDraft) : null,
    l2tpDraft: s.l2tpDraft ? cloneL2tp(s.l2tpDraft) : null,
  }
}

export function composeForkPersistedSnapshot(s: ForkStoreView): ForkStoreView['persistedSnapshot'] {
  return {
    kind: s.kind,
    coreName: s.coreName,
    fallbacksInboundTags: [...s.fallbacksInboundTags],
    excludeInboundTags: [...s.excludeInboundTags],
    xrayProfile: s.xrayProfile,
    wgDraft: s.wgDraft,
    activeSection: s.activeSection,
    monacoJson: s.monacoJson,
    xrayImportWarnings: [...s.xrayImportWarnings],
    serverHydratedConfigJson: s.serverHydratedConfigJson,
    ...captureForkFields(s),
  }
}

function snapshotDraft(snapshot: NonNullable<ForkStoreView['persistedSnapshot']>, kind: ForkCoreKind): unknown {
  if (kind === 'singbox') return snapshot.sbDraft
  if (kind === 'openvpn') return snapshot.ovDraft
  if (kind === 'mtproto') return snapshot.mtDraft
  return snapshot.l2tpDraft
}

function applyForkSnapshot(snapshot: NonNullable<ForkStoreView['persistedSnapshot']>, set: SetFn): boolean {
  if (!isForkCoreKind(snapshot.kind)) return false
  const h = handlers[snapshot.kind]
  const raw = snapshotDraft(snapshot, snapshot.kind)
  if (!raw) return false
  const d = h.clone(raw)
  set({
    kind: snapshot.kind,
    coreName: snapshot.coreName,
    fallbacksInboundTags: [...snapshot.fallbacksInboundTags],
    excludeInboundTags: [...snapshot.excludeInboundTags],
    ...emptyUpstreamDrafts(),
    ...emptyForkDraftFields(),
    ...h.assign(d, h.clone(d)),
    activeSection: forkNormalizeActiveSection(snapshot.kind, snapshot.activeSection),
    monacoJson: snapshot.monacoJson,
    monacoDirty: false,
    xrayImportWarnings: [...snapshot.xrayImportWarnings],
    serverHydratedConfigJson: snapshot.serverHydratedConfigJson ?? null,
    dirty: false,
  })
  return true
}

function hydrateForkKind(args: {
  set: SetFn
  get: GetFn
  kind: ForkCoreKind
  coreId: number | null
  coreName: string
  isNew: boolean
  restartNodes: boolean
  activeSection: string
  serverJson: string | null
  config?: unknown
  rawMonaco?: string
}): void {
  const { set, get, kind, coreId, coreName, isNew, restartNodes, activeSection, serverJson, config, rawMonaco } = args
  const h = handlers[kind]
  if (config !== undefined) {
    const parsed = h.parse(config)
    if (!parsed.ok) {
      const fallbackDraft = h.create()
      set({
        hydrated: true,
        isNew,
        coreId,
        coreName,
        kind,
        restartNodes,
        fallbacksInboundTags: [],
        excludeInboundTags: [],
        ...emptyUpstreamDrafts(),
        ...emptyForkDraftFields(),
        ...h.assign(fallbackDraft, h.clone(fallbackDraft)),
        activeSection,
        dirty: false,
        monacoJson: rawMonaco ?? JSON.stringify(config, null, 2),
        monacoDirty: false,
        xrayImportWarnings: [parsed.message],
        serverHydratedConfigJson: serverJson,
      })
      set({ persistedSnapshot: composeForkPersistedSnapshot(get()) })
      return
    }
    const draft = parsed.draft
    set({
      hydrated: true,
      isNew,
      coreId,
      coreName,
      kind,
      restartNodes,
      fallbacksInboundTags: [],
      excludeInboundTags: [],
      ...emptyUpstreamDrafts(),
      ...emptyForkDraftFields(),
      ...h.assign(draft, h.clone(draft)),
      activeSection,
      dirty: false,
      monacoJson: JSON.stringify(h.persist(draft), null, 2),
      monacoDirty: false,
      xrayImportWarnings: [],
      serverHydratedConfigJson: serverJson,
    })
    set({ persistedSnapshot: composeForkPersistedSnapshot(get()) })
    return
  }
  const draft = h.create()
  set({
    hydrated: true,
    isNew,
    coreId,
    coreName,
    kind,
    restartNodes,
    fallbacksInboundTags: [],
    excludeInboundTags: [],
    ...emptyUpstreamDrafts(),
    ...emptyForkDraftFields(),
    ...h.assign(draft, h.clone(draft)),
    activeSection,
    dirty: false,
    monacoJson: monacoForDraft(kind, draft),
    monacoDirty: false,
    xrayImportWarnings: [],
    serverHydratedConfigJson: serverJson,
  })
  set({ persistedSnapshot: composeForkPersistedSnapshot(get()) })
}

export function createForkCoreEditorSlice(set: SetFn, get: GetFn): ForkCoreEditorSlice {
  return {
    sbDraft: null,
    sbBaseline: null,
    ovDraft: null,
    ovBaseline: null,
    mtDraft: null,
    mtBaseline: null,
    l2tpDraft: null,
    l2tpBaseline: null,
    setSbDraft: sbDraft => {
      set({ sbDraft, dirty: true })
      get().syncMonacoFromDraft()
    },
    updateSbDraft: updater => {
      const cur = get().sbDraft
      if (!cur) return
      const next = updater(cloneSb(cur))
      set({ sbDraft: next, dirty: true })
      get().syncMonacoFromDraft()
    },
    setOvDraft: ovDraft => {
      set({ ovDraft, dirty: true })
      get().syncMonacoFromDraft()
    },
    updateOvDraft: updater => {
      const cur = get().ovDraft
      if (!cur) return
      const next = updater(cloneOv(cur))
      set({ ovDraft: next, dirty: true })
      get().syncMonacoFromDraft()
    },
    setMtDraft: mtDraft => {
      set({ mtDraft, dirty: true })
      get().syncMonacoFromDraft()
    },
    updateMtDraft: updater => {
      const cur = get().mtDraft
      if (!cur) return
      const next = updater(cloneMt(cur))
      set({ mtDraft: next, dirty: true })
      get().syncMonacoFromDraft()
    },
    setL2tpDraft: l2tpDraft => {
      set({ l2tpDraft, dirty: true })
      get().syncMonacoFromDraft()
    },
    updateL2tpDraft: updater => {
      const cur = get().l2tpDraft
      if (!cur) return
      const next = updater(cloneL2tp(cur))
      set({ l2tpDraft: next, dirty: true })
      get().syncMonacoFromDraft()
    },
  }
}

function tryForkInitFromCore(core: CoreResponse, options: { preserveNavigation?: boolean } | undefined, set: SetFn, get: GetFn): boolean {
  const kind = apiCoreTypeToKind(core.type)
  if (!isForkCoreKind(kind)) return false
  const forkKind = kind
  const preserveNavigation = options?.preserveNavigation === true
  const prev = preserveNavigation ? get() : null
  const serverJson = JSON.stringify(core.config)
  const nav =
    preserveNavigation && prev && prev.coreId === core.id
      ? { activeSection: prev.activeSection, restartNodes: prev.restartNodes }
      : { activeSection: handlers[forkKind].defaultSection, restartNodes: true }
  hydrateForkKind({
    set,
    get,
    kind: forkKind,
    coreId: core.id,
    coreName: core.name,
    isNew: false,
    restartNodes: nav.restartNodes,
    activeSection: nav.activeSection,
    serverJson,
    config: core.config,
  })
  return true
}

function tryForkInitNew(kind: CoreKind, name: string, set: SetFn, get: GetFn): boolean {
  if (!isForkCoreKind(kind)) return false
  hydrateForkKind({
    set,
    get,
    kind,
    coreId: null,
    coreName: name,
    isNew: true,
    restartNodes: true,
    activeSection: handlers[kind].defaultSection,
    serverJson: null,
  })
  return true
}

function tryForkSwitchKind(nextKind: CoreKind, set: SetFn): boolean {
  if (!isForkCoreKind(nextKind)) return false
  const h = handlers[nextKind]
  const draft = h.create()
  set({
    kind: nextKind,
    fallbacksInboundTags: [],
    excludeInboundTags: [],
    ...emptyUpstreamDrafts(),
    ...emptyForkDraftFields(),
    ...h.assign(draft, h.clone(draft)),
    activeSection: h.defaultSection,
    dirty: true,
    monacoJson: monacoForDraft(nextKind, draft),
    monacoDirty: false,
    xrayImportWarnings: [],
  })
  return true
}

function tryForkMarkClean(set: SetFn, get: GetFn): boolean {
  const s = get()
  if (!isForkCoreKind(s.kind)) return false
  const h = handlers[s.kind]
  const draft = h.read(s)
  if (!draft) return false
  const baselineKey = s.kind === 'singbox' ? 'sbBaseline' : s.kind === 'openvpn' ? 'ovBaseline' : s.kind === 'mtproto' ? 'mtBaseline' : 'l2tpBaseline'
  set({ [baselineKey]: h.clone(draft), dirty: false, monacoDirty: false })
  return true
}

function tryForkSyncMonaco(set: SetFn, get: GetFn): boolean {
  const s = get()
  if (!isForkCoreKind(s.kind)) return false
  const h = handlers[s.kind]
  const draft = h.read(s)
  if (!draft) return false
  try {
    set({ monacoJson: JSON.stringify(h.persist(draft), null, 2), monacoDirty: false })
  } catch {
    return true
  }
  return true
}

function tryForkApplyMonaco(set: SetFn, get: GetFn): { ok: true } | { ok: false; error: string } | null {
  const s = get()
  if (!isForkCoreKind(s.kind)) return null
  let parsed: unknown
  try {
    parsed = JSON.parse(s.monacoJson)
  } catch (e) {
    return { ok: false, error: e instanceof Error ? e.message : 'Invalid JSON' }
  }
  const r = handlers[s.kind].parse(parsed)
  if (!r.ok) return { ok: false, error: r.message }
  const draftKey = s.kind === 'singbox' ? 'sbDraft' : s.kind === 'openvpn' ? 'ovDraft' : s.kind === 'mtproto' ? 'mtDraft' : 'l2tpDraft'
  set({ [draftKey]: r.draft, dirty: true, monacoDirty: false })
  return { ok: true }
}

export function composeForkCoreEditor<TView extends ForkStoreView, T extends UpstreamSlice>(
  createUpstream: (set: SetFn, get: () => TView) => T,
): StateCreator<TView> {
  return (setState, get) => {
    const set = setState as SetFn
    const upstream = createUpstream(set, get)
    const fork = createForkCoreEditorSlice(set, get)
    return {
      ...upstream,
      ...fork,
      initFromCore: (core: CoreResponse, options?: { preserveNavigation?: boolean }) => {
        if (tryForkInitFromCore(core, options, set, get)) return
        upstream.initFromCore(core, options)
        const snap = get().persistedSnapshot
        set({ ...emptyForkDraftFields(), persistedSnapshot: snap ? { ...snap, ...captureForkFields(get()) } : composeForkPersistedSnapshot(get()) })
      },
      initNew: (kind: CoreKind, name = '') => {
        if (tryForkInitNew(kind, name, set, get)) return
        upstream.initNew(kind, name)
        const snap = get().persistedSnapshot
        set({ ...emptyForkDraftFields(), persistedSnapshot: snap ? { ...snap, ...captureForkFields(get()) } : composeForkPersistedSnapshot(get()) })
      },
      reset: () => {
        upstream.reset()
        set(emptyForkDraftFields())
      },
      markClean: () => {
        if (tryForkMarkClean(set, get)) {
          get().syncMonacoFromDraft()
          set({ persistedSnapshot: composeForkPersistedSnapshot(get()) })
          return
        }
        upstream.markClean()
        const snap = get().persistedSnapshot
        set({ persistedSnapshot: snap ? { ...snap, ...captureForkFields(get()) } : composeForkPersistedSnapshot(get()) })
      },
      discardDraft: () => {
        const snap = get().persistedSnapshot
        if (!snap) return
        if (applyForkSnapshot(snap, set)) return
        upstream.discardDraft()
        set(emptyForkDraftFields())
      },
      switchKind: (nextKind: CoreKind) => {
        const cur = get().kind
        if (nextKind === cur) return
        if (tryForkSwitchKind(nextKind, set)) return
        upstream.switchKind(nextKind)
        set(emptyForkDraftFields())
      },
      syncMonacoFromDraft: () => {
        if (tryForkSyncMonaco(set, get)) return
        upstream.syncMonacoFromDraft()
      },
      applyMonacoJson: () => {
        const forkResult = tryForkApplyMonaco(set, get)
        if (forkResult) return forkResult
        return upstream.applyMonacoJson()
      },
    } as unknown as TView
  }
}
