import { StringArrayPopoverInput } from '@/components/common/string-array-popover-input'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { PasswordInput } from '@/components/ui/password-input'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { Separator } from '@/components/ui/separator'
import { Switch } from '@/components/ui/switch'
import { Tabs, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { Textarea } from '@/components/ui/textarea'
import useDirDetection from '@/hooks/use-dir-detection'
import { cn } from '@/lib/utils'
import type {
  HysteriaInboundDraft,
  ShadowsocksInboundDraft,
  SingBoxCertMode,
  SingBoxInboundDraft,
  SingBoxMasqueradeType,
  SingBoxProtocol,
  SingBoxTransportType,
  SingBoxValidationIssue,
  TlsDraftFields,
  TransportDraftFields,
  TuicInboundDraft,
} from '@pasarguard/singbox-config-kit'
import { RefreshCcw } from 'lucide-react'
import { useTranslation } from 'react-i18next'

interface SingBoxInboundFormProps {
  inbound: SingBoxInboundDraft
  issues: SingBoxValidationIssue[]
  onChange: (updater: (draft: SingBoxInboundDraft) => SingBoxInboundDraft) => void
}

// Radix Select forbids an empty-string item value, so "unset" states use these sentinels.
const TLS_VERSION_DEFAULT = '__default'
const MASQ_SIMPLE = '__simple'
const ACME_PROVIDER_DEFAULT = '__default'
const DNS01_NONE = '__none'
const TRANSPORT_TCP = '__tcp'
const CC_DEFAULT = '__default'
const TLS_VERSIONS = ['1.0', '1.1', '1.2', '1.3']

// Protocols this editor can create. hysteria2 keeps its dedicated full form; the rest share the
// stream (transport + TLS/REALITY) sections below, matching the Xray inbound editor's layout.
const PROTOCOLS: readonly SingBoxProtocol[] = ['vless', 'vmess', 'trojan', 'shadowsocks', 'tuic', 'hysteria2']
const SHADOWSOCKS_METHODS = ['2022-blake3-aes-128-gcm', '2022-blake3-aes-256-gcm', 'aes-256-gcm', 'chacha20-ietf-poly1305']
const TUIC_CONGESTION = ['cubic', 'bbr', 'new_reno']
const TRANSPORTS: readonly SingBoxTransportType[] = ['', 'ws', 'grpc', 'http', 'httpupgrade']

/** The stream protocols that carry a V2Ray transport + full TLS/REALITY block (like Xray vless/vmess/trojan). */
type StreamDraft = Extract<SingBoxInboundDraft, { protocol: 'vless' | 'vmess' | 'trojan' }>

function issueFor(issues: SingBoxValidationIssue[], suffix: string): string | undefined {
  return issues.find(i => i.path.endsWith(`/${suffix}`))?.message
}

function randomPort(): number {
  return Math.floor(Math.random() * (65535 - 10000 + 1)) + 10000
}

function randomObfsPassword(): string {
  const bytes = new Uint8Array(16)
  globalThis.crypto.getRandomValues(bytes)
  return Array.from(bytes, b => b.toString(16).padStart(2, '0')).join('')
}

function SectionHeader({ title }: { title: string }) {
  return <h4 className="text-muted-foreground text-xs font-semibold tracking-wide uppercase">{title}</h4>
}

export function SingBoxInboundForm({ inbound, issues, onChange }: SingBoxInboundFormProps) {
  const { t } = useTranslation()
  const dir = useDirDetection()

  const set = <K extends keyof SingBoxInboundDraft>(key: K, value: SingBoxInboundDraft[K]) => {
    onChange(d => ({ ...d, [key]: value }) as SingBoxInboundDraft)
  }

  const tagError = issueFor(issues, 'tag')
  const portError = issueFor(issues, 'listenPort')

  const flipRow = cn('flex items-center gap-2', dir === 'rtl' ? 'flex-row-reverse' : 'flex-row')

  const changeProtocol = (protocol: SingBoxProtocol) => {
    if (protocol === inbound.protocol) return
    // Rebuild the draft for the new protocol, preserving the shared listen identity so the user
    // doesn't lose the tag/port they already set (mirrors the Xray editor's protocol switch).
    onChange(d => rebuildForProtocol(d, protocol))
  }

  return (
    <div className="space-y-5">
      {/* ---------------------------- Protocol + Listen ---------------------------- */}
      <div className="grid grid-cols-1 gap-x-4 gap-y-5 sm:grid-cols-2">
        <div className="space-y-1.5">
          <Label>{t('coreEditor.singbox.fields.protocol', { defaultValue: 'Protocol' })}</Label>
          <Select value={inbound.protocol} onValueChange={v => changeProtocol(v as SingBoxProtocol)}>
            <SelectTrigger className="text-xs">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {PROTOCOLS.map(p => (
                <SelectItem key={p} value={p}>
                  {p}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>

        <div className="space-y-1.5">
          <Label>{t('coreEditor.singbox.fields.tag', { defaultValue: 'Tag' })}</Label>
          <Input value={inbound.tag} dir="ltr" className="text-xs" isError={!!tagError} onChange={e => set('tag', e.target.value)} placeholder={inbound.protocol} />
          {tagError && <p className="text-destructive text-[0.8rem] font-medium">{tagError}</p>}
        </div>

        <div className="space-y-1.5">
          <Label>{t('coreEditor.singbox.fields.listen', { defaultValue: 'Listen address' })}</Label>
          <Input value={inbound.listen} dir="ltr" className="text-xs" onChange={e => set('listen', e.target.value)} placeholder="::" />
        </div>

        <div className="space-y-1.5">
          <Label>{t('coreEditor.singbox.fields.listenPort', { defaultValue: 'Listen port' })}</Label>
          <div dir="ltr" className={flipRow}>
            <div className="min-w-0 flex-1">
              <Input type="text" inputMode="numeric" value={String(inbound.listenPort)} className="text-xs" isError={!!portError} onChange={e => set('listenPort', e.target.value)} placeholder="8443" />
            </div>
            <Button type="button" size="icon" variant="ghost" className="h-9 w-9 shrink-0" onClick={() => set('listenPort', randomPort())} title={t('coreEditor.inbound.randomPort', { defaultValue: 'Generate random port' })}>
              <RefreshCcw className="h-3 w-3" />
            </Button>
          </div>
          {portError && <p className="text-destructive text-[0.8rem] font-medium">{portError}</p>}
        </div>
      </div>

      {inbound.protocol === 'hysteria2' ? (
        <Hysteria2Sections inbound={inbound} issues={issues} onChange={onChange} flipRow={flipRow} />
      ) : (
        <>
          {inbound.protocol === 'shadowsocks' && <ShadowsocksSection inbound={inbound} issues={issues} onChange={onChange} flipRow={flipRow} />}

          {(inbound.protocol === 'vless' || inbound.protocol === 'vmess' || inbound.protocol === 'trojan') && (
            <>
              <Separator />
              <TransportSection inbound={inbound as StreamDraft} onChange={onChange} />
            </>
          )}

          {inbound.protocol === 'tuic' && <TuicSection inbound={inbound} onChange={onChange} />}

          {inbound.protocol !== 'shadowsocks' && (
            <>
              <Separator />
              <TlsSection inbound={inbound as SingBoxInboundDraft & TlsDraftFields} issues={issues} onChange={onChange} required={inbound.protocol === 'tuic'} />
            </>
          )}
        </>
      )}
    </div>
  )
}

/** Rebuilds a draft when the protocol changes, keeping tag/listen/listenPort. */
function rebuildForProtocol(prev: SingBoxInboundDraft, protocol: SingBoxProtocol): SingBoxInboundDraft {
  const base = { tag: prev.tag, listen: prev.listen, listenPort: prev.listenPort }
  const carriedTls: Partial<TlsDraftFields> =
    'tlsServerName' in prev
      ? {
          tlsServerName: prev.tlsServerName,
          tlsAlpn: prev.tlsAlpn,
          tlsMinVersion: prev.tlsMinVersion,
          tlsMaxVersion: prev.tlsMaxVersion,
          tlsCipherSuites: prev.tlsCipherSuites,
          certMode: prev.certMode,
          certificateFile: prev.certificateFile,
          keyFile: prev.keyFile,
          certificate: prev.certificate,
          key: prev.key,
        }
      : {}
  return { ...defaultDraftForProtocol(protocol), ...base, ...carriedTls } as SingBoxInboundDraft
}

/** Local mirror of the kit's per-protocol default drafts (kept here so the form has no store dep). */
function defaultDraftForProtocol(protocol: SingBoxProtocol): SingBoxInboundDraft {
  const tls: TlsDraftFields = {
    tlsEnabled: true,
    tlsServerName: '',
    tlsAlpn: [],
    tlsMinVersion: '',
    tlsMaxVersion: '',
    tlsCipherSuites: [],
    certMode: 'path',
    certificateFile: '',
    keyFile: '',
    certificate: '',
    key: '',
    echEnabled: false,
    echKey: '',
    echPqSignatureSchemesEnabled: false,
    echDynamicRecordSizingDisabled: false,
    acmeEnabled: false,
    acmeDomain: [],
    acmeEmail: '',
    acmeProvider: '',
    acmeDns01Provider: '',
    acmeDns01ApiToken: '',
    acmeDns01AccessKeyId: '',
    acmeDns01AccessKeySecret: '',
    utlsEnabled: false,
    utlsFingerprint: '',
    realityEnabled: false,
    realityHandshakeServer: '',
    realityHandshakePort: '',
    realityPrivateKey: '',
    realityShortId: [],
    realityMaxTimeDifference: '',
  }
  const transport: TransportDraftFields = { transportType: '', transportPath: '', transportHost: '', transportServiceName: '', transportMethod: '' }
  const base = { tag: '', listen: '::', listenPort: '' as number | string }
  switch (protocol) {
    case 'vless':
      return { protocol, ...base, ...tls, ...transport }
    case 'vmess':
      return { protocol, ...base, ...tls, ...transport }
    case 'trojan':
      return { protocol, ...base, ...tls, ...transport }
    case 'tuic':
      return { protocol, ...base, ...tls, congestionControl: '' }
    case 'shadowsocks':
      return { protocol, ...base, method: '2022-blake3-aes-128-gcm', password: '' }
    case 'hysteria2':
      return hysteria2Default(base)
  }
}

function hysteria2Default(base: { tag: string; listen: string; listenPort: number | string }): HysteriaInboundDraft {
  return {
    protocol: 'hysteria2',
    ...base,
    upMbps: '',
    downMbps: '',
    ignoreClientBandwidth: false,
    udpTimeout: '',
    udpFragment: false,
    brutalDebug: false,
    portHoppingRange: '',
    obfsEnabled: false,
    obfsPassword: '',
    masquerade: '',
    masqueradeType: '',
    masqueradeDirectory: '',
    masqueradeRewriteHost: false,
    masqueradeStatusCode: '',
    masqueradeHeaders: '',
    masqueradeContent: '',
    tlsServerName: '',
    tlsAlpn: [],
    tlsMinVersion: '',
    tlsMaxVersion: '',
    tlsCipherSuites: [],
    certMode: 'path',
    certificateFile: '',
    keyFile: '',
    certificate: '',
    key: '',
    echEnabled: false,
    echKey: '',
    echPqSignatureSchemesEnabled: false,
    echDynamicRecordSizingDisabled: false,
    acmeEnabled: false,
    acmeDomain: [],
    acmeEmail: '',
    acmeProvider: '',
    acmeDns01Provider: '',
    acmeDns01ApiToken: '',
    acmeDns01AccessKeyId: '',
    acmeDns01AccessKeySecret: '',
  }
}

// ============================ Shadowsocks ============================
function ShadowsocksSection({
  inbound,
  issues,
  onChange,
  flipRow,
}: {
  inbound: ShadowsocksInboundDraft
  issues: SingBoxValidationIssue[]
  onChange: SingBoxInboundFormProps['onChange']
  flipRow: string
}) {
  const { t } = useTranslation()
  const set = <K extends keyof ShadowsocksInboundDraft>(key: K, value: ShadowsocksInboundDraft[K]) => onChange(d => ({ ...d, [key]: value }) as SingBoxInboundDraft)
  const methodError = issueFor(issues, 'method')
  const is2022 = inbound.method.startsWith('2022-')
  const randomPass = () => {
    const bytes = new Uint8Array(inbound.method.includes('256') ? 32 : 16)
    globalThis.crypto.getRandomValues(bytes)
    set('password', btoa(String.fromCharCode(...bytes)))
  }
  return (
    <>
      <Separator />
      <div className="space-y-3">
        <SectionHeader title={t('coreEditor.singbox.ss.title', { defaultValue: 'Shadowsocks' })} />
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
          <div className="space-y-1.5">
            <Label>{t('coreEditor.singbox.ss.method', { defaultValue: 'Encryption method' })}</Label>
            <Select value={inbound.method} onValueChange={v => set('method', v)}>
              <SelectTrigger className="text-xs">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {SHADOWSOCKS_METHODS.map(m => (
                  <SelectItem key={m} value={m}>
                    {m}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            {methodError && <p className="text-destructive text-[0.8rem] font-medium">{methodError}</p>}
          </div>
          <div className="space-y-1.5">
            <Label>{t('coreEditor.singbox.ss.password', { defaultValue: is2022 ? 'Server key (base64)' : 'Password' })}</Label>
            <div dir="ltr" className={flipRow}>
              <div className="min-w-0 flex-1">
                <PasswordInput className="text-xs" value={inbound.password} onChange={e => set('password', e.target.value)} />
              </div>
              <Button type="button" size="icon" variant="ghost" className="h-9 w-9 shrink-0" onClick={randomPass} title={t('coreEditor.singbox.obfs.generate', { defaultValue: 'Generate password' })}>
                <RefreshCcw className="h-3 w-3" />
              </Button>
            </div>
            <p className="text-muted-foreground text-[11px]">
              {is2022
                ? t('coreEditor.singbox.ss.serverKeyHint', { defaultValue: 'Base64 server key for the 2022 cipher. Per-user passwords are injected automatically.' })
                : t('coreEditor.singbox.ss.legacyHint', { defaultValue: 'Legacy single-user password. Prefer a 2022-blake3 method for multi-user.' })}
            </p>
          </div>
        </div>
      </div>
    </>
  )
}

// ============================ TUIC ============================
function TuicSection({ inbound, onChange }: { inbound: TuicInboundDraft; onChange: SingBoxInboundFormProps['onChange'] }) {
  const { t } = useTranslation()
  const set = <K extends keyof TuicInboundDraft>(key: K, value: TuicInboundDraft[K]) => onChange(d => ({ ...d, [key]: value }) as SingBoxInboundDraft)
  return (
    <>
      <Separator />
      <div className="space-y-3">
        <SectionHeader title={t('coreEditor.singbox.tuic.title', { defaultValue: 'TUIC' })} />
        <div className="space-y-1.5 sm:w-1/2">
          <Label>{t('coreEditor.singbox.tuic.congestion', { defaultValue: 'Congestion control' })}</Label>
          <Select value={inbound.congestionControl || CC_DEFAULT} onValueChange={v => set('congestionControl', v === CC_DEFAULT ? '' : v)}>
            <SelectTrigger className="text-xs">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value={CC_DEFAULT}>{t('coreEditor.singbox.tls.versionDefault', { defaultValue: 'Default' })} (cubic)</SelectItem>
              {TUIC_CONGESTION.map(c => (
                <SelectItem key={c} value={c}>
                  {c}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>
      </div>
    </>
  )
}

// ============================ Transport (V2Ray) ============================
function TransportSection({ inbound, onChange }: { inbound: StreamDraft; onChange: SingBoxInboundFormProps['onChange'] }) {
  const { t } = useTranslation()
  const set = <K extends keyof StreamDraft>(key: K, value: StreamDraft[K]) => onChange(d => ({ ...d, [key]: value }) as SingBoxInboundDraft)
  const tt = inbound.transportType
  return (
    <div className="space-y-3">
      <SectionHeader title={t('coreEditor.singbox.transport.title', { defaultValue: 'Transport' })} />
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
        <div className="space-y-1.5">
          <Label>{t('coreEditor.singbox.transport.type', { defaultValue: 'Type' })}</Label>
          <Select value={tt === '' ? TRANSPORT_TCP : tt} onValueChange={v => set('transportType', (v === TRANSPORT_TCP ? '' : v) as SingBoxTransportType)}>
            <SelectTrigger className="text-xs">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value={TRANSPORT_TCP}>TCP</SelectItem>
              {TRANSPORTS.filter(Boolean).map(tp => (
                <SelectItem key={tp} value={tp as string}>
                  {tp}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>
        {(tt === 'ws' || tt === 'http' || tt === 'httpupgrade') && (
          <div className="space-y-1.5">
            <Label>{t('coreEditor.singbox.transport.path', { defaultValue: 'Path' })}</Label>
            <Input value={inbound.transportPath} dir="ltr" className="text-xs" onChange={e => set('transportPath', e.target.value)} placeholder="/" />
          </div>
        )}
        {(tt === 'ws' || tt === 'http' || tt === 'httpupgrade') && (
          <div className="space-y-1.5">
            <Label>{t('coreEditor.singbox.transport.host', { defaultValue: 'Host' })}</Label>
            <Input value={inbound.transportHost} dir="ltr" className="text-xs" onChange={e => set('transportHost', e.target.value)} placeholder="example.com" />
          </div>
        )}
        {tt === 'grpc' && (
          <div className="space-y-1.5">
            <Label>{t('coreEditor.singbox.transport.serviceName', { defaultValue: 'Service name' })}</Label>
            <Input value={inbound.transportServiceName} dir="ltr" className="text-xs" onChange={e => set('transportServiceName', e.target.value)} placeholder="grpc" />
          </div>
        )}
        {tt === 'http' && (
          <div className="space-y-1.5">
            <Label>{t('coreEditor.singbox.transport.method', { defaultValue: 'Method' })}</Label>
            <Input value={inbound.transportMethod} dir="ltr" className="text-xs" onChange={e => set('transportMethod', e.target.value)} placeholder="GET" />
          </div>
        )}
      </div>
    </div>
  )
}

// ============================ TLS + REALITY (shared) ============================
function TlsSection({
  inbound,
  issues,
  onChange,
  required,
}: {
  inbound: SingBoxInboundDraft & TlsDraftFields
  issues: SingBoxValidationIssue[]
  onChange: SingBoxInboundFormProps['onChange']
  required: boolean
}) {
  const { t } = useTranslation()
  const set = <K extends keyof TlsDraftFields>(key: K, value: TlsDraftFields[K]) => onChange(d => ({ ...d, [key]: value }) as SingBoxInboundDraft)
  const realityKeyError = issueFor(issues, 'realityPrivateKey')
  const realityHandshakeError = issueFor(issues, 'realityHandshakeServer')
  const enabled = required || inbound.tlsEnabled

  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between">
        <SectionHeader title={t('coreEditor.singbox.tls.title', { defaultValue: 'TLS' })} />
        {!required && (
          <div className="flex items-center gap-2">
            <Label className="text-muted-foreground text-xs font-normal">{t('coreEditor.singbox.tls.enable', { defaultValue: 'Enable TLS' })}</Label>
            <Switch checked={inbound.tlsEnabled} onCheckedChange={c => set('tlsEnabled', c === true)} />
          </div>
        )}
      </div>

      {enabled && (
        <>
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
            <div className="space-y-1.5">
              <Label>{t('coreEditor.singbox.tls.serverName', { defaultValue: 'Server name (SNI)' })}</Label>
              <Input value={inbound.tlsServerName} dir="ltr" className="text-xs" onChange={e => set('tlsServerName', e.target.value)} placeholder="example.com" />
            </div>
            <div className="space-y-1.5">
              <Label>{t('coreEditor.singbox.tls.alpn', { defaultValue: 'ALPN' })}</Label>
              <StringArrayPopoverInput value={[...inbound.tlsAlpn]} onChange={next => set('tlsAlpn', next)} placeholder="h2" addPlaceholder="h2" itemsLabel={t('coreEditor.singbox.tls.alpn', { defaultValue: 'ALPN' })} />
            </div>
          </div>

          {/* REALITY */}
          <div className="space-y-2 rounded-md border p-3">
            <div className="flex items-center justify-between">
              <Label className="text-xs font-medium">{t('coreEditor.singbox.tls.reality', { defaultValue: 'REALITY' })}</Label>
              <Switch checked={inbound.realityEnabled} onCheckedChange={c => set('realityEnabled', c === true)} />
            </div>
            {inbound.realityEnabled && (
              <div className="space-y-2">
                <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
                  <div className="space-y-1">
                    <Label className="text-muted-foreground text-[11px] font-medium">{t('coreEditor.singbox.tls.realityHandshakeServer', { defaultValue: 'Handshake server' })}</Label>
                    <Input dir="ltr" className="text-xs" isError={!!realityHandshakeError} value={inbound.realityHandshakeServer} onChange={e => set('realityHandshakeServer', e.target.value)} placeholder="www.microsoft.com" />
                    {realityHandshakeError && <p className="text-destructive text-[0.8rem] font-medium">{realityHandshakeError}</p>}
                  </div>
                  <div className="space-y-1">
                    <Label className="text-muted-foreground text-[11px] font-medium">{t('coreEditor.singbox.tls.realityHandshakePort', { defaultValue: 'Handshake port' })}</Label>
                    <Input dir="ltr" type="text" inputMode="numeric" className="text-xs" value={inbound.realityHandshakePort} onChange={e => set('realityHandshakePort', e.target.value)} placeholder="443" />
                  </div>
                </div>
                <div className="space-y-1">
                  <Label className="text-muted-foreground text-[11px] font-medium">{t('coreEditor.singbox.tls.realityPrivateKey', { defaultValue: 'Private key' })}</Label>
                  <PasswordInput className="text-xs" isError={!!realityKeyError} value={inbound.realityPrivateKey} onChange={e => set('realityPrivateKey', e.target.value)} />
                  {realityKeyError && <p className="text-destructive text-[0.8rem] font-medium">{realityKeyError}</p>}
                </div>
                <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
                  <div className="space-y-1">
                    <Label className="text-muted-foreground text-[11px] font-medium">{t('coreEditor.singbox.tls.realityShortId', { defaultValue: 'Short IDs' })}</Label>
                    <StringArrayPopoverInput value={[...inbound.realityShortId]} onChange={next => set('realityShortId', next)} placeholder="0123abcd" addPlaceholder="0123abcd" itemsLabel={t('coreEditor.singbox.tls.realityShortId', { defaultValue: 'Short IDs' })} />
                  </div>
                  <div className="space-y-1">
                    <Label className="text-muted-foreground text-[11px] font-medium">{t('coreEditor.singbox.tls.realityMaxTimeDiff', { defaultValue: 'Max time difference' })}</Label>
                    <Input dir="ltr" className="text-xs" value={inbound.realityMaxTimeDifference} onChange={e => set('realityMaxTimeDifference', e.target.value)} placeholder="1m" />
                  </div>
                </div>
              </div>
            )}
          </div>

          {/* Certificate + uTLS (hidden when REALITY owns the handshake) */}
          {!inbound.realityEnabled && (
            <>
              <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
                <div className="space-y-1.5">
                  <Label>{t('coreEditor.singbox.tls.minVersion', { defaultValue: 'Min version' })}</Label>
                  <Select value={inbound.tlsMinVersion || TLS_VERSION_DEFAULT} onValueChange={v => set('tlsMinVersion', v === TLS_VERSION_DEFAULT ? '' : v)}>
                    <SelectTrigger className="text-xs">
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      <SelectItem value={TLS_VERSION_DEFAULT}>{t('coreEditor.singbox.tls.versionDefault', { defaultValue: 'Default' })}</SelectItem>
                      {TLS_VERSIONS.map(v => (
                        <SelectItem key={v} value={v}>
                          {v}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                </div>
                <div className="space-y-1.5">
                  <Label>{t('coreEditor.singbox.tls.maxVersion', { defaultValue: 'Max version' })}</Label>
                  <Select value={inbound.tlsMaxVersion || TLS_VERSION_DEFAULT} onValueChange={v => set('tlsMaxVersion', v === TLS_VERSION_DEFAULT ? '' : v)}>
                    <SelectTrigger className="text-xs">
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      <SelectItem value={TLS_VERSION_DEFAULT}>{t('coreEditor.singbox.tls.versionDefault', { defaultValue: 'Default' })}</SelectItem>
                      {TLS_VERSIONS.map(v => (
                        <SelectItem key={v} value={v}>
                          {v}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                </div>
                <div className="space-y-1.5">
                  <Label>{t('coreEditor.singbox.tls.utls', { defaultValue: 'uTLS fingerprint' })}</Label>
                  <div className="flex items-center gap-2">
                    <Switch checked={inbound.utlsEnabled} onCheckedChange={c => set('utlsEnabled', c === true)} />
                    <Input dir="ltr" className="text-xs" disabled={!inbound.utlsEnabled} value={inbound.utlsFingerprint} onChange={e => set('utlsFingerprint', e.target.value)} placeholder="chrome" />
                  </div>
                </div>
              </div>

              <div className="space-y-2 rounded-md border p-3">
                <Label className="text-xs font-medium">{t('coreEditor.singbox.tls.certificate', { defaultValue: 'Certificate' })}</Label>
                <Tabs value={inbound.certMode} onValueChange={v => set('certMode', (v === 'content' ? 'content' : 'path') as SingBoxCertMode)}>
                  <TabsList className="grid w-full grid-cols-2">
                    <TabsTrigger value="path">{t('coreEditor.inbound.tlsCertificates.filePathTab', { defaultValue: 'File path' })}</TabsTrigger>
                    <TabsTrigger value="content">{t('coreEditor.inbound.tlsCertificates.fileContentTab', { defaultValue: 'File content' })}</TabsTrigger>
                  </TabsList>
                </Tabs>
                {inbound.certMode === 'path' ? (
                  <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
                    <div className="space-y-1">
                      <Label className="text-muted-foreground text-[11px] font-medium">{t('coreEditor.inbound.tlsCertificates.certificateFile', { defaultValue: 'Certificate file' })}</Label>
                      <Input dir="ltr" className="text-xs" placeholder="/path/fullchain.pem" value={inbound.certificateFile} onChange={e => set('certificateFile', e.target.value)} />
                    </div>
                    <div className="space-y-1">
                      <Label className="text-muted-foreground text-[11px] font-medium">{t('coreEditor.inbound.tlsCertificates.keyFile', { defaultValue: 'Key file' })}</Label>
                      <Input dir="ltr" className="text-xs" placeholder="/path/key.pem" value={inbound.keyFile} onChange={e => set('keyFile', e.target.value)} />
                    </div>
                  </div>
                ) : (
                  <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
                    <div className="space-y-1">
                      <Label className="text-muted-foreground text-[11px] font-medium">{t('coreEditor.inbound.tlsCertificates.certificateContent', { defaultValue: 'Certificate content' })}</Label>
                      <Textarea dir="ltr" rows={5} className="text-xs" placeholder="-----BEGIN CERTIFICATE-----" value={inbound.certificate} onChange={e => set('certificate', e.target.value)} />
                    </div>
                    <div className="space-y-1">
                      <Label className="text-muted-foreground text-[11px] font-medium">{t('coreEditor.inbound.tlsCertificates.keyContent', { defaultValue: 'Key content' })}</Label>
                      <Textarea dir="ltr" rows={5} className="text-xs" placeholder="-----BEGIN PRIVATE KEY-----" value={inbound.key} onChange={e => set('key', e.target.value)} />
                    </div>
                  </div>
                )}
              </div>
            </>
          )}
        </>
      )}
    </div>
  )
}

// ============================ Hysteria2 (full dedicated form, unchanged) ============================
function Hysteria2Sections({
  inbound,
  issues,
  onChange,
  flipRow,
}: {
  inbound: HysteriaInboundDraft
  issues: SingBoxValidationIssue[]
  onChange: SingBoxInboundFormProps['onChange']
  flipRow: string
}) {
  const { t } = useTranslation()
  const set = <K extends keyof HysteriaInboundDraft>(key: K, value: HysteriaInboundDraft[K]) => onChange(d => ({ ...d, [key]: value }) as SingBoxInboundDraft)

  const upMbpsError = issueFor(issues, 'upMbps')
  const downMbpsError = issueFor(issues, 'downMbps')
  const udpTimeoutError = issueFor(issues, 'udpTimeout')
  const obfsPasswordError = issueFor(issues, 'obfsPassword')
  const masqueradeError = issueFor(issues, 'masquerade')
  const masqueradeDirectoryError = issueFor(issues, 'masqueradeDirectory')
  const masqueradeStatusError = issueFor(issues, 'masqueradeStatusCode')
  const masqueradeHeadersError = issueFor(issues, 'masqueradeHeaders')
  const portHoppingError = issueFor(issues, 'portHoppingRange')
  const acmeDomainError = issueFor(issues, 'acmeDomain')
  const acmeTokenError = issueFor(issues, 'acmeDns01ApiToken')
  const acmeKeysError = issueFor(issues, 'acmeDns01AccessKeyId')
  const masqType = inbound.masqueradeType

  return (
    <>
      {/* Bandwidth + UDP timeout */}
      <div className="grid grid-cols-1 gap-x-4 gap-y-5 sm:grid-cols-2">
        <div className="space-y-1.5">
          <Label>{t('coreEditor.singbox.fields.udpTimeout', { defaultValue: 'UDP timeout' })}</Label>
          <Input value={inbound.udpTimeout} dir="ltr" className="text-xs" isError={!!udpTimeoutError} onChange={e => set('udpTimeout', e.target.value)} placeholder="30s" />
          {udpTimeoutError && <p className="text-destructive text-[0.8rem] font-medium">{udpTimeoutError}</p>}
        </div>
        <div className="space-y-1.5">
          <Label>{t('coreEditor.singbox.fields.upMbps', { defaultValue: 'Up mbps' })}</Label>
          <Input type="text" inputMode="numeric" value={inbound.upMbps} dir="ltr" className="text-xs" isError={!!upMbpsError} onChange={e => set('upMbps', e.target.value)} placeholder="100" />
          {upMbpsError && <p className="text-destructive text-[0.8rem] font-medium">{upMbpsError}</p>}
        </div>
        <div className="space-y-1.5">
          <Label>{t('coreEditor.singbox.fields.downMbps', { defaultValue: 'Down mbps' })}</Label>
          <Input type="text" inputMode="numeric" value={inbound.downMbps} dir="ltr" className="text-xs" isError={!!downMbpsError} onChange={e => set('downMbps', e.target.value)} placeholder="100" />
          {downMbpsError && <p className="text-destructive text-[0.8rem] font-medium">{downMbpsError}</p>}
        </div>
        <div className="flex items-start gap-2 rounded-md border border-dashed px-3 py-2 sm:col-span-2">
          <Switch id={`sb-ignore-bandwidth-${inbound.tag}`} checked={inbound.ignoreClientBandwidth} onCheckedChange={checked => set('ignoreClientBandwidth', checked === true)} />
          <div className="grid gap-0.5 leading-tight">
            <label htmlFor={`sb-ignore-bandwidth-${inbound.tag}`} className="cursor-pointer text-xs font-medium">
              {t('coreEditor.singbox.fields.ignoreClientBandwidth', { defaultValue: 'Ignore client bandwidth' })}
            </label>
          </div>
        </div>
      </div>

      <Separator />

      {/* Obfuscation */}
      <div className="space-y-3">
        <div className="flex items-center justify-between">
          <SectionHeader title={t('coreEditor.singbox.obfs.title', { defaultValue: 'Obfuscation' })} />
          <div className="flex items-center gap-2">
            <Label htmlFor={`sb-obfs-enabled-${inbound.tag}`} className="text-muted-foreground text-xs font-normal">
              {t('coreEditor.singbox.obfs.enable', { defaultValue: 'Salamander obfuscation' })}
            </Label>
            <Switch id={`sb-obfs-enabled-${inbound.tag}`} checked={inbound.obfsEnabled} onCheckedChange={checked => set('obfsEnabled', checked === true)} />
          </div>
        </div>
        {inbound.obfsEnabled && (
          <div className="space-y-1.5">
            <Label>{t('coreEditor.singbox.obfs.password', { defaultValue: 'Obfuscation password' })}</Label>
            <div dir="ltr" className={flipRow}>
              <div className="min-w-0 flex-1">
                <PasswordInput className="text-xs" isError={!!obfsPasswordError} value={inbound.obfsPassword} onChange={e => set('obfsPassword', e.target.value)} />
              </div>
              <Button type="button" size="icon" variant="ghost" className="h-9 w-9 shrink-0" onClick={() => set('obfsPassword', randomObfsPassword())} title={t('coreEditor.singbox.obfs.generate', { defaultValue: 'Generate password' })}>
                <RefreshCcw className="h-3 w-3" />
              </Button>
            </div>
            {obfsPasswordError && <p className="text-destructive text-[0.8rem] font-medium">{obfsPasswordError}</p>}
          </div>
        )}
      </div>

      <Separator />

      {/* Masquerade */}
      <div className="space-y-3">
        <SectionHeader title={t('coreEditor.singbox.masquerade.title', { defaultValue: 'Masquerade' })} />
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
          <div className="space-y-1.5">
            <Label>{t('coreEditor.singbox.masquerade.type', { defaultValue: 'Type' })}</Label>
            <Select value={masqType === '' ? MASQ_SIMPLE : masqType} onValueChange={v => set('masqueradeType', (v === MASQ_SIMPLE ? '' : v) as SingBoxMasqueradeType)}>
              <SelectTrigger className="text-xs">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value={MASQ_SIMPLE}>{t('coreEditor.singbox.masquerade.simple', { defaultValue: 'URL (simple)' })}</SelectItem>
                <SelectItem value="proxy">{t('coreEditor.singbox.masquerade.proxy', { defaultValue: 'Reverse proxy' })}</SelectItem>
                <SelectItem value="file">{t('coreEditor.singbox.masquerade.file', { defaultValue: 'Static files' })}</SelectItem>
                <SelectItem value="string">{t('coreEditor.singbox.masquerade.string', { defaultValue: 'Fixed response' })}</SelectItem>
              </SelectContent>
            </Select>
          </div>
          {(masqType === '' || masqType === 'proxy') && (
            <div className="space-y-1.5">
              <Label>{t('coreEditor.singbox.masquerade.url', { defaultValue: 'URL' })}</Label>
              <Input value={inbound.masquerade} dir="ltr" className="text-xs" isError={!!masqueradeError} onChange={e => set('masquerade', e.target.value)} placeholder="https://example.com" />
              {masqueradeError && <p className="text-destructive text-[0.8rem] font-medium">{masqueradeError}</p>}
            </div>
          )}
          {masqType === 'file' && (
            <div className="space-y-1.5">
              <Label>{t('coreEditor.singbox.masquerade.directory', { defaultValue: 'Directory' })}</Label>
              <Input value={inbound.masqueradeDirectory} dir="ltr" className="text-xs" isError={!!masqueradeDirectoryError} onChange={e => set('masqueradeDirectory', e.target.value)} placeholder="/var/www" />
              {masqueradeDirectoryError && <p className="text-destructive text-[0.8rem] font-medium">{masqueradeDirectoryError}</p>}
            </div>
          )}
        </div>
        {masqType === 'proxy' && (
          <div className="flex items-start gap-2 rounded-md border border-dashed px-3 py-2">
            <Switch id={`sb-masq-rewrite-${inbound.tag}`} checked={inbound.masqueradeRewriteHost} onCheckedChange={checked => set('masqueradeRewriteHost', checked === true)} />
            <div className="grid gap-0.5 leading-tight">
              <label htmlFor={`sb-masq-rewrite-${inbound.tag}`} className="cursor-pointer text-xs font-medium">
                {t('coreEditor.singbox.masquerade.rewriteHost', { defaultValue: 'Rewrite Host header' })}
              </label>
            </div>
          </div>
        )}
        {masqType === 'string' && (
          <div className="space-y-3">
            <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
              <div className="space-y-1.5">
                <Label>{t('coreEditor.singbox.masquerade.statusCode', { defaultValue: 'Status code' })}</Label>
                <Input type="text" inputMode="numeric" value={inbound.masqueradeStatusCode} dir="ltr" className="text-xs" isError={!!masqueradeStatusError} onChange={e => set('masqueradeStatusCode', e.target.value)} placeholder="404" />
                {masqueradeStatusError && <p className="text-destructive text-[0.8rem] font-medium">{masqueradeStatusError}</p>}
              </div>
              <div className="space-y-1.5">
                <Label>{t('coreEditor.singbox.masquerade.headers', { defaultValue: 'Headers (JSON)' })}</Label>
                <Textarea dir="ltr" rows={2} className={cn('text-xs font-mono', masqueradeHeadersError && 'border-destructive')} value={inbound.masqueradeHeaders} onChange={e => set('masqueradeHeaders', e.target.value)} placeholder='{"Server": "nginx"}' />
                {masqueradeHeadersError && <p className="text-destructive text-[0.8rem] font-medium">{masqueradeHeadersError}</p>}
              </div>
            </div>
            <div className="space-y-1.5">
              <Label>{t('coreEditor.singbox.masquerade.content', { defaultValue: 'Response body' })}</Label>
              <Textarea dir="ltr" rows={3} className="text-xs" value={inbound.masqueradeContent} onChange={e => set('masqueradeContent', e.target.value)} placeholder="Not Found" />
            </div>
          </div>
        )}
      </div>

      <Separator />

      {/* TLS (hysteria2: always on, cert/ECH/ACME, no REALITY) */}
      <div className="space-y-3">
        <SectionHeader title={t('coreEditor.singbox.tls.title', { defaultValue: 'TLS' })} />
        <p className="text-muted-foreground text-[11px]">{t('coreEditor.singbox.tls.alwaysEnabledHint', { defaultValue: 'Hysteria2 always requires TLS; it cannot be disabled for this inbound.' })}</p>
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
          <div className="space-y-1.5">
            <Label>{t('coreEditor.singbox.tls.serverName', { defaultValue: 'Server name (SNI)' })}</Label>
            <Input value={inbound.tlsServerName} dir="ltr" className="text-xs" onChange={e => set('tlsServerName', e.target.value)} placeholder="example.com" />
          </div>
          <div className="space-y-1.5">
            <Label>{t('coreEditor.singbox.tls.alpn', { defaultValue: 'ALPN' })}</Label>
            <StringArrayPopoverInput value={[...inbound.tlsAlpn]} onChange={next => set('tlsAlpn', next)} placeholder="h3" addPlaceholder="h3" itemsLabel={t('coreEditor.singbox.tls.alpn', { defaultValue: 'ALPN' })} />
          </div>
        </div>
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
          <div className="space-y-1.5">
            <Label>{t('coreEditor.singbox.tls.minVersion', { defaultValue: 'Min version' })}</Label>
            <Select value={inbound.tlsMinVersion || TLS_VERSION_DEFAULT} onValueChange={v => set('tlsMinVersion', v === TLS_VERSION_DEFAULT ? '' : v)}>
              <SelectTrigger className="text-xs">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value={TLS_VERSION_DEFAULT}>{t('coreEditor.singbox.tls.versionDefault', { defaultValue: 'Default' })}</SelectItem>
                {TLS_VERSIONS.map(v => (
                  <SelectItem key={v} value={v}>
                    {v}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
          <div className="space-y-1.5">
            <Label>{t('coreEditor.singbox.tls.maxVersion', { defaultValue: 'Max version' })}</Label>
            <Select value={inbound.tlsMaxVersion || TLS_VERSION_DEFAULT} onValueChange={v => set('tlsMaxVersion', v === TLS_VERSION_DEFAULT ? '' : v)}>
              <SelectTrigger className="text-xs">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value={TLS_VERSION_DEFAULT}>{t('coreEditor.singbox.tls.versionDefault', { defaultValue: 'Default' })}</SelectItem>
                {TLS_VERSIONS.map(v => (
                  <SelectItem key={v} value={v}>
                    {v}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
          <div className="space-y-1.5">
            <Label>{t('coreEditor.singbox.tls.cipherSuites', { defaultValue: 'Cipher suites' })}</Label>
            <StringArrayPopoverInput value={[...inbound.tlsCipherSuites]} onChange={next => set('tlsCipherSuites', next)} placeholder="TLS_AES_128_GCM_SHA256" addPlaceholder="TLS_AES_128_GCM_SHA256" itemsLabel={t('coreEditor.singbox.tls.cipherSuites', { defaultValue: 'Cipher suites' })} />
          </div>
        </div>
        {inbound.acmeEnabled ? (
          <p className="text-muted-foreground rounded-md border border-dashed px-3 py-2 text-[11px]">
            {t('coreEditor.singbox.tls.acmeOwnsCert', { defaultValue: 'ACME is enabled below and manages the certificate automatically. The manual certificate fields are disabled.' })}
          </p>
        ) : (
          <div className="space-y-2 rounded-md border p-3">
            <Label className="text-xs font-medium">{t('coreEditor.singbox.tls.certificate', { defaultValue: 'Certificate' })}</Label>
            <Tabs value={inbound.certMode} onValueChange={v => set('certMode', (v === 'content' ? 'content' : 'path') as SingBoxCertMode)}>
              <TabsList className="grid w-full grid-cols-2">
                <TabsTrigger value="path">{t('coreEditor.inbound.tlsCertificates.filePathTab', { defaultValue: 'File path' })}</TabsTrigger>
                <TabsTrigger value="content">{t('coreEditor.inbound.tlsCertificates.fileContentTab', { defaultValue: 'File content' })}</TabsTrigger>
              </TabsList>
            </Tabs>
            {inbound.certMode === 'path' ? (
              <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
                <div className="space-y-1">
                  <Label className="text-muted-foreground text-[11px] font-medium">{t('coreEditor.inbound.tlsCertificates.certificateFile', { defaultValue: 'Certificate file' })}</Label>
                  <Input dir="ltr" className="text-xs" placeholder="/path/fullchain.pem" value={inbound.certificateFile} onChange={e => set('certificateFile', e.target.value)} />
                </div>
                <div className="space-y-1">
                  <Label className="text-muted-foreground text-[11px] font-medium">{t('coreEditor.inbound.tlsCertificates.keyFile', { defaultValue: 'Key file' })}</Label>
                  <Input dir="ltr" className="text-xs" placeholder="/path/key.pem" value={inbound.keyFile} onChange={e => set('keyFile', e.target.value)} />
                </div>
              </div>
            ) : (
              <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
                <div className="space-y-1">
                  <Label className="text-muted-foreground text-[11px] font-medium">{t('coreEditor.inbound.tlsCertificates.certificateContent', { defaultValue: 'Certificate content' })}</Label>
                  <Textarea dir="ltr" rows={5} className="text-xs" placeholder="-----BEGIN CERTIFICATE-----" value={inbound.certificate} onChange={e => set('certificate', e.target.value)} />
                </div>
                <div className="space-y-1">
                  <Label className="text-muted-foreground text-[11px] font-medium">{t('coreEditor.inbound.tlsCertificates.keyContent', { defaultValue: 'Key content' })}</Label>
                  <Textarea dir="ltr" rows={5} className="text-xs" placeholder="-----BEGIN PRIVATE KEY-----" value={inbound.key} onChange={e => set('key', e.target.value)} />
                </div>
              </div>
            )}
          </div>
        )}
        <div className="space-y-2 rounded-md border p-3">
          <div className="flex items-center justify-between">
            <Label className="text-xs font-medium">{t('coreEditor.singbox.tls.ech', { defaultValue: 'Encrypted Client Hello (ECH)' })}</Label>
            <Switch id={`sb-ech-${inbound.tag}`} checked={inbound.echEnabled} onCheckedChange={checked => set('echEnabled', checked === true)} />
          </div>
          {inbound.echEnabled && (
            <div className="space-y-2">
              <div className="space-y-1">
                <Label className="text-muted-foreground text-[11px] font-medium">{t('coreEditor.singbox.tls.echKey', { defaultValue: 'ECH key' })}</Label>
                <Textarea dir="ltr" rows={3} className="text-xs" placeholder="-----BEGIN ECH KEYS-----" value={inbound.echKey} onChange={e => set('echKey', e.target.value)} />
              </div>
              <div className="flex items-center gap-2">
                <Switch id={`sb-ech-pq-${inbound.tag}`} checked={inbound.echPqSignatureSchemesEnabled} onCheckedChange={checked => set('echPqSignatureSchemesEnabled', checked === true)} />
                <label htmlFor={`sb-ech-pq-${inbound.tag}`} className="cursor-pointer text-[11px] font-medium">
                  {t('coreEditor.singbox.tls.echPq', { defaultValue: 'Post-quantum signature schemes' })}
                </label>
              </div>
              <div className="flex items-center gap-2">
                <Switch id={`sb-ech-drs-${inbound.tag}`} checked={inbound.echDynamicRecordSizingDisabled} onCheckedChange={checked => set('echDynamicRecordSizingDisabled', checked === true)} />
                <label htmlFor={`sb-ech-drs-${inbound.tag}`} className="cursor-pointer text-[11px] font-medium">
                  {t('coreEditor.singbox.tls.echDrs', { defaultValue: 'Disable dynamic record sizing' })}
                </label>
              </div>
            </div>
          )}
        </div>
        <div className="space-y-2 rounded-md border p-3">
          <div className="flex items-center justify-between">
            <Label className="text-xs font-medium">{t('coreEditor.singbox.tls.acme', { defaultValue: 'ACME (automatic certificate)' })}</Label>
            <Switch id={`sb-acme-${inbound.tag}`} checked={inbound.acmeEnabled} onCheckedChange={checked => set('acmeEnabled', checked === true)} />
          </div>
          {inbound.acmeEnabled && (
            <div className="space-y-2">
              <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
                <div className="space-y-1">
                  <Label className="text-muted-foreground text-[11px] font-medium">{t('coreEditor.singbox.tls.acmeDomain', { defaultValue: 'Domains' })}</Label>
                  <StringArrayPopoverInput value={[...inbound.acmeDomain]} onChange={next => set('acmeDomain', next)} placeholder="his.example.com" addPlaceholder="his.example.com" itemsLabel={t('coreEditor.singbox.tls.acmeDomain', { defaultValue: 'Domains' })} />
                  {acmeDomainError && <p className="text-destructive text-[0.8rem] font-medium">{acmeDomainError}</p>}
                </div>
                <div className="space-y-1">
                  <Label className="text-muted-foreground text-[11px] font-medium">{t('coreEditor.singbox.tls.acmeEmail', { defaultValue: 'Email' })}</Label>
                  <Input dir="ltr" className="text-xs" placeholder="admin@example.com" value={inbound.acmeEmail} onChange={e => set('acmeEmail', e.target.value)} />
                </div>
              </div>
              <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
                <div className="space-y-1">
                  <Label className="text-muted-foreground text-[11px] font-medium">{t('coreEditor.singbox.tls.acmeDns01', { defaultValue: 'DNS-01 provider' })}</Label>
                  <Select value={inbound.acmeDns01Provider || DNS01_NONE} onValueChange={v => set('acmeDns01Provider', v === DNS01_NONE ? '' : v)}>
                    <SelectTrigger className="text-xs">
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      <SelectItem value={DNS01_NONE}>{t('coreEditor.singbox.tls.acmeDns01None', { defaultValue: 'None (HTTP challenge)' })}</SelectItem>
                      <SelectItem value="cloudflare">Cloudflare</SelectItem>
                      <SelectItem value="alidns">AliDNS</SelectItem>
                    </SelectContent>
                  </Select>
                </div>
                <div className="space-y-1">
                  <Label className="text-muted-foreground text-[11px] font-medium">{t('coreEditor.singbox.tls.acmeProvider', { defaultValue: 'CA provider' })}</Label>
                  <Select value={inbound.acmeProvider || ACME_PROVIDER_DEFAULT} onValueChange={v => set('acmeProvider', v === ACME_PROVIDER_DEFAULT ? '' : v)}>
                    <SelectTrigger className="text-xs">
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      <SelectItem value={ACME_PROVIDER_DEFAULT}>{t('coreEditor.singbox.tls.versionDefault', { defaultValue: 'Default' })}</SelectItem>
                      <SelectItem value="letsencrypt">Let's Encrypt</SelectItem>
                      <SelectItem value="zerossl">ZeroSSL</SelectItem>
                      <SelectItem value="google">Google</SelectItem>
                    </SelectContent>
                  </Select>
                </div>
              </div>
              {inbound.acmeDns01Provider === 'cloudflare' && (
                <div className="space-y-1">
                  <Label className="text-muted-foreground text-[11px] font-medium">{t('coreEditor.singbox.tls.acmeCfToken', { defaultValue: 'Cloudflare API token' })}</Label>
                  <PasswordInput className="text-xs" isError={!!acmeTokenError} value={inbound.acmeDns01ApiToken} onChange={e => set('acmeDns01ApiToken', e.target.value)} />
                  {acmeTokenError && <p className="text-destructive text-[0.8rem] font-medium">{acmeTokenError}</p>}
                </div>
              )}
              {inbound.acmeDns01Provider === 'alidns' && (
                <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
                  <div className="space-y-1">
                    <Label className="text-muted-foreground text-[11px] font-medium">{t('coreEditor.singbox.tls.acmeAliKeyId', { defaultValue: 'Access key id' })}</Label>
                    <Input dir="ltr" className="text-xs" isError={!!acmeKeysError} value={inbound.acmeDns01AccessKeyId} onChange={e => set('acmeDns01AccessKeyId', e.target.value)} />
                  </div>
                  <div className="space-y-1">
                    <Label className="text-muted-foreground text-[11px] font-medium">{t('coreEditor.singbox.tls.acmeAliKeySecret', { defaultValue: 'Access key secret' })}</Label>
                    <PasswordInput className="text-xs" isError={!!acmeKeysError} value={inbound.acmeDns01AccessKeySecret} onChange={e => set('acmeDns01AccessKeySecret', e.target.value)} />
                  </div>
                  {acmeKeysError && <p className="text-destructive text-[0.8rem] font-medium sm:col-span-2">{acmeKeysError}</p>}
                </div>
              )}
            </div>
          )}
        </div>
      </div>

      <Separator />

      {/* Advanced */}
      <div className="space-y-3">
        <SectionHeader title={t('coreEditor.singbox.advanced.title', { defaultValue: 'Advanced' })} />
        <div className="space-y-1.5">
          <Label>{t('coreEditor.singbox.advanced.portHopping', { defaultValue: 'Port hopping range' })}</Label>
          <Input value={inbound.portHoppingRange} dir="ltr" className="text-xs" isError={!!portHoppingError} onChange={e => set('portHoppingRange', e.target.value)} placeholder="20000-50000" />
          {portHoppingError && <p className="text-destructive text-[0.8rem] font-medium">{portHoppingError}</p>}
        </div>
        <div className="flex items-center gap-2">
          <Switch id={`sb-udp-fragment-${inbound.tag}`} checked={inbound.udpFragment} onCheckedChange={checked => set('udpFragment', checked === true)} />
          <label htmlFor={`sb-udp-fragment-${inbound.tag}`} className="cursor-pointer text-xs font-medium">
            {t('coreEditor.singbox.advanced.udpFragment', { defaultValue: 'UDP fragment' })}
          </label>
        </div>
        <div className="flex items-center gap-2">
          <Switch id={`sb-brutal-debug-${inbound.tag}`} checked={inbound.brutalDebug} onCheckedChange={checked => set('brutalDebug', checked === true)} />
          <label htmlFor={`sb-brutal-debug-${inbound.tag}`} className="cursor-pointer text-xs font-medium">
            {t('coreEditor.singbox.advanced.brutalDebug', { defaultValue: 'Brutal debug logging' })}
          </label>
        </div>
      </div>
    </>
  )
}
