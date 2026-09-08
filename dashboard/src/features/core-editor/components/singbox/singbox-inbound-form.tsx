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
import type { LucideIcon } from 'lucide-react'
import { Cable, Dices, Gauge, KeyRound, Settings2, Shield, Shuffle, VenetianMask } from 'lucide-react'
import type { ReactNode } from 'react'
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

const BASE_LABEL_CLASS = 'text-sm font-medium'
const SUB_LABEL_CLASS = 'text-muted-foreground text-xs font-semibold tracking-wide'
const CONTROL_CLASS = 'h-10'
const SELECT_TRIGGER_CLASS = 'h-10 w-full min-w-0 py-2'
const LIST_CONTROL_CLASS = 'min-h-10'
const SWITCH_ROW_CLASS = 'flex min-h-10 flex-row items-center justify-between gap-3 space-y-0 rounded-md border px-3 py-2'
const GROUP_BOX_CLASS = 'space-y-3 rounded-lg border px-3 py-3 sm:col-span-2'
const NOTE_CLASS = 'text-muted-foreground rounded-md border border-dashed px-3 py-2 text-[11px] sm:col-span-2'
const HINT_CLASS = 'text-muted-foreground text-[11px]'
const ERROR_CLASS = 'text-destructive text-[0.8rem] font-medium'

// Protocols this editor can create. hysteria2 keeps its dedicated full form; the rest share the
// stream (transport + TLS/REALITY) sections below, matching the Xray inbound editor's layout.
const PROTOCOLS: readonly SingBoxProtocol[] = ['vless', 'vmess', 'trojan', 'shadowsocks', 'tuic', 'hysteria2']
const SHADOWSOCKS_METHODS = ['2022-blake3-aes-128-gcm', '2022-blake3-aes-256-gcm', 'aes-256-gcm', 'chacha20-ietf-poly1305']
const TUIC_CONGESTION = ['cubic', 'bbr', 'new_reno']
const TRANSPORTS: readonly SingBoxTransportType[] = ['', 'ws', 'grpc', 'http', 'httpupgrade']

/** The stream protocols that carry a V2Ray transport + full TLS/REALITY block (like Xray vless/vmess/trojan). */
type StreamDraft = Extract<SingBoxInboundDraft, { protocol: 'vless' | 'vmess' | 'trojan' }>

type TlsIdentitySlice = Pick<TlsDraftFields, 'tlsServerName' | 'tlsAlpn'>
type TlsVersionSlice = Pick<TlsDraftFields, 'tlsMinVersion' | 'tlsMaxVersion' | 'tlsCipherSuites'>
type CertificateSlice = Pick<TlsDraftFields, 'certMode' | 'certificateFile' | 'keyFile' | 'certificate' | 'key'>
type EchSlice = Pick<TlsDraftFields, 'echEnabled' | 'echKey' | 'echPqSignatureSchemesEnabled' | 'echDynamicRecordSizingDisabled'>
type AcmeSlice = Pick<TlsDraftFields, 'acmeEnabled' | 'acmeDomain' | 'acmeEmail' | 'acmeProvider' | 'acmeDns01Provider' | 'acmeDns01ApiToken' | 'acmeDns01AccessKeyId' | 'acmeDns01AccessKeySecret'>

type DraftChange = SingBoxInboundFormProps['onChange']

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

function SectionTitle({ icon: Icon, title }: { icon: LucideIcon; title: string }) {
  return (
    <div className="mb-1 flex items-center gap-2 sm:col-span-2">
      <Icon className="text-muted-foreground h-3.5 w-3.5 shrink-0" aria-hidden />
      <h3 className="text-sm font-semibold">{title}</h3>
    </div>
  )
}

function SectionHeader({ title }: { title: string }) {
  return <h4 className="text-muted-foreground text-xs font-semibold tracking-wide uppercase">{title}</h4>
}

function SectionDivider() {
  return <Separator className="my-2 sm:col-span-2" />
}

function Field({
  label,
  htmlFor,
  error,
  hint,
  className,
  labelClassName,
  children,
}: {
  label: ReactNode
  htmlFor?: string
  error?: string
  hint?: ReactNode
  className?: string
  labelClassName?: string
  children: ReactNode
}) {
  return (
    <div className={cn('w-full min-w-0 space-y-2', className)}>
      <Label htmlFor={htmlFor} className={cn('block', labelClassName ?? SUB_LABEL_CLASS)}>
        {label}
      </Label>
      {children}
      {hint ? <p className={HINT_CLASS}>{hint}</p> : null}
      {error ? <p className={ERROR_CLASS}>{error}</p> : null}
    </div>
  )
}

function SwitchField({
  id,
  label,
  hint,
  checked,
  onCheckedChange,
  className,
}: {
  id: string
  label: ReactNode
  hint?: ReactNode
  checked: boolean
  onCheckedChange: (checked: boolean) => void
  className?: string
}) {
  return (
    <div className={cn(SWITCH_ROW_CLASS, className)}>
      <div className="min-w-0 space-y-0.5">
        <Label htmlFor={id} className="cursor-pointer text-sm font-medium">
          {label}
        </Label>
        {hint ? <p className={HINT_CLASS}>{hint}</p> : null}
      </div>
      <Switch id={id} checked={checked} onCheckedChange={c => onCheckedChange(c === true)} />
    </div>
  )
}

function GenerateButton({ onClick, title }: { onClick: () => void; title: string }) {
  return (
    <Button type="button" variant="outline" size="icon" className="h-10 w-10 shrink-0" onClick={onClick} title={title}>
      <Dices className="text-muted-foreground h-4 w-4" />
    </Button>
  )
}

export function SingBoxInboundForm({ inbound, issues, onChange }: SingBoxInboundFormProps) {
  const { t } = useTranslation()

  const set = <K extends keyof SingBoxInboundDraft>(key: K, value: SingBoxInboundDraft[K]) => {
    onChange(d => ({ ...d, [key]: value }) as SingBoxInboundDraft)
  }

  const tagError = issueFor(issues, 'tag')
  const portError = issueFor(issues, 'listenPort')
  const uid = inbound.tag || inbound.protocol

  const changeProtocol = (protocol: SingBoxProtocol) => {
    if (protocol === inbound.protocol) return
    // Rebuild the draft for the new protocol, preserving the shared listen identity so the user
    // doesn't lose the tag/port they already set (mirrors the Xray editor's protocol switch).
    onChange(d => rebuildForProtocol(d, protocol))
  }

  return (
    <div className="grid gap-4 pb-2 sm:grid-cols-2">
      <Field label={t('coreEditor.singbox.fields.tag', { defaultValue: 'Tag' })} labelClassName={BASE_LABEL_CLASS} error={tagError}>
        <Input value={inbound.tag} dir="ltr" className={CONTROL_CLASS} isError={!!tagError} onChange={e => set('tag', e.target.value)} placeholder={inbound.protocol} />
      </Field>

      <Field label={t('coreEditor.singbox.fields.protocol', { defaultValue: 'Protocol' })} labelClassName={BASE_LABEL_CLASS}>
        <Select dir="ltr" value={inbound.protocol} onValueChange={v => changeProtocol(v as SingBoxProtocol)}>
          <SelectTrigger className={SELECT_TRIGGER_CLASS} dir="ltr">
            <SelectValue />
          </SelectTrigger>
          <SelectContent dir="ltr">
            {PROTOCOLS.map(p => (
              <SelectItem key={p} value={p}>
                {p}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      </Field>

      <Field label={t('coreEditor.singbox.fields.listen', { defaultValue: 'Listen address' })} labelClassName={BASE_LABEL_CLASS}>
        <Input value={inbound.listen} dir="ltr" className={CONTROL_CLASS} onChange={e => set('listen', e.target.value)} placeholder="::" />
      </Field>

      <Field label={t('coreEditor.singbox.fields.listenPort', { defaultValue: 'Listen port' })} labelClassName={BASE_LABEL_CLASS} error={portError}>
        <div className="relative">
          <Input
            type="text"
            inputMode="numeric"
            dir="ltr"
            className="h-10 pr-10"
            isError={!!portError}
            value={String(inbound.listenPort)}
            onChange={e => set('listenPort', e.target.value)}
            placeholder="8443"
          />
          <Button
            type="button"
            variant="ghost"
            size="sm"
            className="absolute top-0 right-0 h-10 px-3 hover:bg-transparent"
            onClick={() => set('listenPort', randomPort())}
            title={t('coreEditor.inbound.randomPort', { defaultValue: 'Generate random port' })}
          >
            <Dices className="text-muted-foreground h-4 w-4" />
          </Button>
        </div>
      </Field>

      {inbound.protocol === 'hysteria2' ? (
        <Hysteria2Sections inbound={inbound} issues={issues} onChange={onChange} uid={uid} />
      ) : (
        <>
          {inbound.protocol === 'shadowsocks' && <ShadowsocksSection inbound={inbound} issues={issues} onChange={onChange} />}

          {(inbound.protocol === 'vless' || inbound.protocol === 'vmess' || inbound.protocol === 'trojan') && <TransportSection inbound={inbound as StreamDraft} onChange={onChange} />}

          {inbound.protocol === 'tuic' && <TuicSection inbound={inbound} onChange={onChange} />}

          {inbound.protocol !== 'shadowsocks' && (
            <TlsSection inbound={inbound as SingBoxInboundDraft & TlsDraftFields} issues={issues} onChange={onChange} required={inbound.protocol === 'tuic'} uid={uid} />
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

function TlsIdentityFields({ fields, onChange, alpnPlaceholder }: { fields: TlsIdentitySlice; onChange: DraftChange; alpnPlaceholder: string }) {
  const { t } = useTranslation()
  const set = <K extends keyof TlsIdentitySlice>(key: K, value: TlsIdentitySlice[K]) => onChange(d => ({ ...d, [key]: value }) as SingBoxInboundDraft)
  const alpnLabel = t('coreEditor.singbox.tls.alpn', { defaultValue: 'ALPN' })
  return (
    <>
      <Field label={t('coreEditor.singbox.tls.serverName', { defaultValue: 'Server name (SNI)' })}>
        <Input value={fields.tlsServerName} dir="ltr" className={CONTROL_CLASS} onChange={e => set('tlsServerName', e.target.value)} placeholder="example.com" />
      </Field>
      <Field label={alpnLabel}>
        <StringArrayPopoverInput
          className={LIST_CONTROL_CLASS}
          value={[...fields.tlsAlpn]}
          onChange={next => set('tlsAlpn', next)}
          placeholder={alpnPlaceholder}
          addPlaceholder={alpnPlaceholder}
          itemsLabel={alpnLabel}
        />
      </Field>
    </>
  )
}

function TlsVersionFields({ fields, onChange }: { fields: TlsVersionSlice; onChange: DraftChange }) {
  const { t } = useTranslation()
  const set = <K extends keyof TlsVersionSlice>(key: K, value: TlsVersionSlice[K]) => onChange(d => ({ ...d, [key]: value }) as SingBoxInboundDraft)
  const defaultLabel = t('coreEditor.singbox.tls.versionDefault', { defaultValue: 'Default' })
  const cipherLabel = t('coreEditor.singbox.tls.cipherSuites', { defaultValue: 'Cipher suites' })
  return (
    <>
      <Field label={t('coreEditor.singbox.tls.minVersion', { defaultValue: 'Min version' })}>
        <Select dir="ltr" value={fields.tlsMinVersion || TLS_VERSION_DEFAULT} onValueChange={v => set('tlsMinVersion', v === TLS_VERSION_DEFAULT ? '' : v)}>
          <SelectTrigger className={SELECT_TRIGGER_CLASS} dir="ltr">
            <SelectValue />
          </SelectTrigger>
          <SelectContent dir="ltr">
            <SelectItem value={TLS_VERSION_DEFAULT}>{defaultLabel}</SelectItem>
            {TLS_VERSIONS.map(v => (
              <SelectItem key={v} value={v}>
                {v}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      </Field>
      <Field label={t('coreEditor.singbox.tls.maxVersion', { defaultValue: 'Max version' })}>
        <Select dir="ltr" value={fields.tlsMaxVersion || TLS_VERSION_DEFAULT} onValueChange={v => set('tlsMaxVersion', v === TLS_VERSION_DEFAULT ? '' : v)}>
          <SelectTrigger className={SELECT_TRIGGER_CLASS} dir="ltr">
            <SelectValue />
          </SelectTrigger>
          <SelectContent dir="ltr">
            <SelectItem value={TLS_VERSION_DEFAULT}>{defaultLabel}</SelectItem>
            {TLS_VERSIONS.map(v => (
              <SelectItem key={v} value={v}>
                {v}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      </Field>
      <Field className="sm:col-span-2" label={cipherLabel}>
        <StringArrayPopoverInput
          className={LIST_CONTROL_CLASS}
          value={[...fields.tlsCipherSuites]}
          onChange={next => set('tlsCipherSuites', next)}
          placeholder="TLS_AES_128_GCM_SHA256"
          addPlaceholder="TLS_AES_128_GCM_SHA256"
          itemsLabel={cipherLabel}
        />
      </Field>
    </>
  )
}

function CertificateFields({ fields, onChange }: { fields: CertificateSlice; onChange: DraftChange }) {
  const { t } = useTranslation()
  const set = <K extends keyof CertificateSlice>(key: K, value: CertificateSlice[K]) => onChange(d => ({ ...d, [key]: value }) as SingBoxInboundDraft)
  return (
    <div className={GROUP_BOX_CLASS}>
      <SectionHeader title={t('coreEditor.singbox.tls.certificate', { defaultValue: 'Certificate' })} />
      <Tabs value={fields.certMode} onValueChange={v => set('certMode', (v === 'content' ? 'content' : 'path') as SingBoxCertMode)}>
        <TabsList className="grid w-full grid-cols-2">
          <TabsTrigger value="path">{t('coreEditor.inbound.tlsCertificates.filePathTab', { defaultValue: 'File path' })}</TabsTrigger>
          <TabsTrigger value="content">{t('coreEditor.inbound.tlsCertificates.fileContentTab', { defaultValue: 'File content' })}</TabsTrigger>
        </TabsList>
      </Tabs>
      {fields.certMode === 'path' ? (
        <div className="grid gap-3 sm:grid-cols-2">
          <Field label={t('coreEditor.inbound.tlsCertificates.certificateFile', { defaultValue: 'Certificate file' })}>
            <Input dir="ltr" className={CONTROL_CLASS} placeholder="/path/fullchain.pem" value={fields.certificateFile} onChange={e => set('certificateFile', e.target.value)} />
          </Field>
          <Field label={t('coreEditor.inbound.tlsCertificates.keyFile', { defaultValue: 'Key file' })}>
            <Input dir="ltr" className={CONTROL_CLASS} placeholder="/path/key.pem" value={fields.keyFile} onChange={e => set('keyFile', e.target.value)} />
          </Field>
        </div>
      ) : (
        <div className="grid gap-3 sm:grid-cols-2">
          <Field label={t('coreEditor.inbound.tlsCertificates.certificateContent', { defaultValue: 'Certificate content' })}>
            <Textarea dir="ltr" rows={5} className="text-xs" placeholder="-----BEGIN CERTIFICATE-----" value={fields.certificate} onChange={e => set('certificate', e.target.value)} />
          </Field>
          <Field label={t('coreEditor.inbound.tlsCertificates.keyContent', { defaultValue: 'Key content' })}>
            <Textarea dir="ltr" rows={5} className="text-xs" placeholder="-----BEGIN PRIVATE KEY-----" value={fields.key} onChange={e => set('key', e.target.value)} />
          </Field>
        </div>
      )}
    </div>
  )
}

function EchFields({ fields, onChange, uid }: { fields: EchSlice; onChange: DraftChange; uid: string }) {
  const { t } = useTranslation()
  const set = <K extends keyof EchSlice>(key: K, value: EchSlice[K]) => onChange(d => ({ ...d, [key]: value }) as SingBoxInboundDraft)
  return (
    <div className={GROUP_BOX_CLASS}>
      <div className="flex items-center justify-between gap-3">
        <SectionHeader title={t('coreEditor.singbox.tls.ech', { defaultValue: 'Encrypted Client Hello (ECH)' })} />
        <Switch id={`sb-ech-${uid}`} checked={fields.echEnabled} onCheckedChange={checked => set('echEnabled', checked === true)} />
      </div>
      {fields.echEnabled && (
        <div className="space-y-3">
          <Field label={t('coreEditor.singbox.tls.echKey', { defaultValue: 'ECH key' })}>
            <Textarea dir="ltr" rows={3} className="text-xs" placeholder="-----BEGIN ECH KEYS-----" value={fields.echKey} onChange={e => set('echKey', e.target.value)} />
          </Field>
          <div className="grid gap-3 sm:grid-cols-2">
            <SwitchField
              id={`sb-ech-pq-${uid}`}
              label={t('coreEditor.singbox.tls.echPq', { defaultValue: 'Post-quantum signature schemes' })}
              checked={fields.echPqSignatureSchemesEnabled}
              onCheckedChange={checked => set('echPqSignatureSchemesEnabled', checked)}
            />
            <SwitchField
              id={`sb-ech-drs-${uid}`}
              label={t('coreEditor.singbox.tls.echDrs', { defaultValue: 'Disable dynamic record sizing' })}
              checked={fields.echDynamicRecordSizingDisabled}
              onCheckedChange={checked => set('echDynamicRecordSizingDisabled', checked)}
            />
          </div>
        </div>
      )}
    </div>
  )
}

function AcmeFields({
  fields,
  onChange,
  uid,
  domainError,
  tokenError,
  keysError,
}: {
  fields: AcmeSlice
  onChange: DraftChange
  uid: string
  domainError?: string
  tokenError?: string
  keysError?: string
}) {
  const { t } = useTranslation()
  const set = <K extends keyof AcmeSlice>(key: K, value: AcmeSlice[K]) => onChange(d => ({ ...d, [key]: value }) as SingBoxInboundDraft)
  const domainLabel = t('coreEditor.singbox.tls.acmeDomain', { defaultValue: 'Domains' })
  return (
    <div className={GROUP_BOX_CLASS}>
      <div className="flex items-center justify-between gap-3">
        <SectionHeader title={t('coreEditor.singbox.tls.acme', { defaultValue: 'ACME (automatic certificate)' })} />
        <Switch id={`sb-acme-${uid}`} checked={fields.acmeEnabled} onCheckedChange={checked => set('acmeEnabled', checked === true)} />
      </div>
      {fields.acmeEnabled && (
        <div className="grid gap-3 sm:grid-cols-2">
          <Field label={domainLabel} error={domainError}>
            <StringArrayPopoverInput
              className={LIST_CONTROL_CLASS}
              value={[...fields.acmeDomain]}
              onChange={next => set('acmeDomain', next)}
              placeholder="his.example.com"
              addPlaceholder="his.example.com"
              itemsLabel={domainLabel}
            />
          </Field>
          <Field label={t('coreEditor.singbox.tls.acmeEmail', { defaultValue: 'Email' })}>
            <Input dir="ltr" className={CONTROL_CLASS} placeholder="admin@example.com" value={fields.acmeEmail} onChange={e => set('acmeEmail', e.target.value)} />
          </Field>
          <Field label={t('coreEditor.singbox.tls.acmeDns01', { defaultValue: 'DNS-01 provider' })}>
            <Select dir="ltr" value={fields.acmeDns01Provider || DNS01_NONE} onValueChange={v => set('acmeDns01Provider', v === DNS01_NONE ? '' : v)}>
              <SelectTrigger className={SELECT_TRIGGER_CLASS} dir="ltr">
                <SelectValue />
              </SelectTrigger>
              <SelectContent dir="ltr">
                <SelectItem value={DNS01_NONE}>{t('coreEditor.singbox.tls.acmeDns01None', { defaultValue: 'None (HTTP challenge)' })}</SelectItem>
                <SelectItem value="cloudflare">Cloudflare</SelectItem>
                <SelectItem value="alidns">AliDNS</SelectItem>
              </SelectContent>
            </Select>
          </Field>
          <Field label={t('coreEditor.singbox.tls.acmeProvider', { defaultValue: 'CA provider' })}>
            <Select dir="ltr" value={fields.acmeProvider || ACME_PROVIDER_DEFAULT} onValueChange={v => set('acmeProvider', v === ACME_PROVIDER_DEFAULT ? '' : v)}>
              <SelectTrigger className={SELECT_TRIGGER_CLASS} dir="ltr">
                <SelectValue />
              </SelectTrigger>
              <SelectContent dir="ltr">
                <SelectItem value={ACME_PROVIDER_DEFAULT}>{t('coreEditor.singbox.tls.versionDefault', { defaultValue: 'Default' })}</SelectItem>
                <SelectItem value="letsencrypt">Let's Encrypt</SelectItem>
                <SelectItem value="zerossl">ZeroSSL</SelectItem>
                <SelectItem value="google">Google</SelectItem>
              </SelectContent>
            </Select>
          </Field>
          {fields.acmeDns01Provider === 'cloudflare' && (
            <Field className="sm:col-span-2" label={t('coreEditor.singbox.tls.acmeCfToken', { defaultValue: 'Cloudflare API token' })} error={tokenError}>
              <PasswordInput
                dir="ltr"
                autoComplete="new-password"
                className={CONTROL_CLASS}
                isError={!!tokenError}
                value={fields.acmeDns01ApiToken}
                onChange={e => set('acmeDns01ApiToken', e.target.value)}
              />
            </Field>
          )}
          {fields.acmeDns01Provider === 'alidns' && (
            <>
              <Field label={t('coreEditor.singbox.tls.acmeAliKeyId', { defaultValue: 'Access key id' })} error={keysError}>
                <Input dir="ltr" className={CONTROL_CLASS} isError={!!keysError} value={fields.acmeDns01AccessKeyId} onChange={e => set('acmeDns01AccessKeyId', e.target.value)} />
              </Field>
              <Field label={t('coreEditor.singbox.tls.acmeAliKeySecret', { defaultValue: 'Access key secret' })}>
                <PasswordInput
                  dir="ltr"
                  autoComplete="new-password"
                  className={CONTROL_CLASS}
                  isError={!!keysError}
                  value={fields.acmeDns01AccessKeySecret}
                  onChange={e => set('acmeDns01AccessKeySecret', e.target.value)}
                />
              </Field>
            </>
          )}
        </div>
      )}
    </div>
  )
}

// ============================ Shadowsocks ============================
function ShadowsocksSection({ inbound, issues, onChange }: { inbound: ShadowsocksInboundDraft; issues: SingBoxValidationIssue[]; onChange: DraftChange }) {
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
      <SectionDivider />
      <SectionTitle icon={KeyRound} title={t('coreEditor.singbox.ss.title', { defaultValue: 'Shadowsocks' })} />
      <Field label={t('coreEditor.singbox.ss.method', { defaultValue: 'Encryption method' })} error={methodError}>
        <Select dir="ltr" value={inbound.method} onValueChange={v => set('method', v)}>
          <SelectTrigger className={SELECT_TRIGGER_CLASS} dir="ltr">
            <SelectValue />
          </SelectTrigger>
          <SelectContent dir="ltr">
            {SHADOWSOCKS_METHODS.map(m => (
              <SelectItem key={m} value={m}>
                {m}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      </Field>
      <Field
        label={is2022 ? t('coreEditor.singbox.ss.serverKey', { defaultValue: 'Server key (base64)' }) : t('coreEditor.singbox.ss.password', { defaultValue: 'Password' })}
        hint={
          is2022
            ? t('coreEditor.singbox.ss.serverKeyHint', { defaultValue: 'Base64 server key for the 2022 cipher. Per-user passwords are injected automatically.' })
            : t('coreEditor.singbox.ss.legacyHint', { defaultValue: 'Legacy single-user password. Prefer a 2022-blake3 method for multi-user.' })
        }
      >
        <div className="flex items-center gap-2">
          <PasswordInput dir="ltr" autoComplete="new-password" className={CONTROL_CLASS} value={inbound.password} onChange={e => set('password', e.target.value)} />
          <GenerateButton onClick={randomPass} title={t('coreEditor.singbox.obfs.generate', { defaultValue: 'Generate password' })} />
        </div>
      </Field>
    </>
  )
}

// ============================ TUIC ============================
function TuicSection({ inbound, onChange }: { inbound: TuicInboundDraft; onChange: DraftChange }) {
  const { t } = useTranslation()
  const set = <K extends keyof TuicInboundDraft>(key: K, value: TuicInboundDraft[K]) => onChange(d => ({ ...d, [key]: value }) as SingBoxInboundDraft)
  return (
    <>
      <SectionDivider />
      <SectionTitle icon={Gauge} title={t('coreEditor.singbox.tuic.title', { defaultValue: 'TUIC' })} />
      <Field label={t('coreEditor.singbox.tuic.congestion', { defaultValue: 'Congestion control' })}>
        <Select dir="ltr" value={inbound.congestionControl || CC_DEFAULT} onValueChange={v => set('congestionControl', v === CC_DEFAULT ? '' : v)}>
          <SelectTrigger className={SELECT_TRIGGER_CLASS} dir="ltr">
            <SelectValue />
          </SelectTrigger>
          <SelectContent dir="ltr">
            <SelectItem value={CC_DEFAULT}>{t('coreEditor.singbox.tls.versionDefault', { defaultValue: 'Default' })} (cubic)</SelectItem>
            {TUIC_CONGESTION.map(c => (
              <SelectItem key={c} value={c}>
                {c}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      </Field>
    </>
  )
}

// ============================ Transport (V2Ray) ============================
function TransportSection({ inbound, onChange }: { inbound: StreamDraft; onChange: DraftChange }) {
  const { t } = useTranslation()
  const set = <K extends keyof StreamDraft>(key: K, value: StreamDraft[K]) => onChange(d => ({ ...d, [key]: value }) as SingBoxInboundDraft)
  const tt = inbound.transportType
  return (
    <>
      <SectionDivider />
      <SectionTitle icon={Cable} title={t('coreEditor.singbox.transport.title', { defaultValue: 'Transport' })} />
      <Field label={t('coreEditor.singbox.transport.type', { defaultValue: 'Type' })}>
        <Select dir="ltr" value={tt === '' ? TRANSPORT_TCP : tt} onValueChange={v => set('transportType', (v === TRANSPORT_TCP ? '' : v) as SingBoxTransportType)}>
          <SelectTrigger className={SELECT_TRIGGER_CLASS} dir="ltr">
            <SelectValue />
          </SelectTrigger>
          <SelectContent dir="ltr">
            <SelectItem value={TRANSPORT_TCP}>TCP</SelectItem>
            {TRANSPORTS.filter(Boolean).map(tp => (
              <SelectItem key={tp} value={tp as string}>
                {tp}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      </Field>
      {(tt === 'ws' || tt === 'http' || tt === 'httpupgrade') && (
        <Field label={t('coreEditor.singbox.transport.path', { defaultValue: 'Path' })}>
          <Input value={inbound.transportPath} dir="ltr" className={CONTROL_CLASS} onChange={e => set('transportPath', e.target.value)} placeholder="/" />
        </Field>
      )}
      {(tt === 'ws' || tt === 'http' || tt === 'httpupgrade') && (
        <Field label={t('coreEditor.singbox.transport.host', { defaultValue: 'Host' })}>
          <Input value={inbound.transportHost} dir="ltr" className={CONTROL_CLASS} onChange={e => set('transportHost', e.target.value)} placeholder="example.com" />
        </Field>
      )}
      {tt === 'grpc' && (
        <Field label={t('coreEditor.singbox.transport.serviceName', { defaultValue: 'Service name' })}>
          <Input value={inbound.transportServiceName} dir="ltr" className={CONTROL_CLASS} onChange={e => set('transportServiceName', e.target.value)} placeholder="grpc" />
        </Field>
      )}
      {tt === 'http' && (
        <Field label={t('coreEditor.singbox.transport.method', { defaultValue: 'Method' })}>
          <Input value={inbound.transportMethod} dir="ltr" className={CONTROL_CLASS} onChange={e => set('transportMethod', e.target.value)} placeholder="GET" />
        </Field>
      )}
    </>
  )
}

// ============================ TLS + REALITY (shared) ============================
function TlsSection({
  inbound,
  issues,
  onChange,
  required,
  uid,
}: {
  inbound: SingBoxInboundDraft & TlsDraftFields
  issues: SingBoxValidationIssue[]
  onChange: DraftChange
  required: boolean
  uid: string
}) {
  const { t } = useTranslation()
  const set = <K extends keyof TlsDraftFields>(key: K, value: TlsDraftFields[K]) => onChange(d => ({ ...d, [key]: value }) as SingBoxInboundDraft)
  const realityKeyError = issueFor(issues, 'realityPrivateKey')
  const realityHandshakeError = issueFor(issues, 'realityHandshakeServer')
  const acmeDomainError = issueFor(issues, 'acmeDomain')
  const acmeTokenError = issueFor(issues, 'acmeDns01ApiToken')
  const acmeKeysError = issueFor(issues, 'acmeDns01AccessKeyId')
  const enabled = required || inbound.tlsEnabled
  const shortIdLabel = t('coreEditor.singbox.tls.realityShortId', { defaultValue: 'Short IDs' })

  return (
    <>
      <SectionDivider />
      <SectionTitle icon={Shield} title={t('coreEditor.singbox.tls.title', { defaultValue: 'TLS' })} />

      {required ? (
        <p className={NOTE_CLASS}>{t('coreEditor.singbox.tls.requiredHint', { defaultValue: 'This protocol always requires TLS; it cannot be disabled for this inbound.' })}</p>
      ) : (
        <SwitchField
          className="sm:col-span-2"
          id={`sb-tls-${uid}`}
          label={t('coreEditor.singbox.tls.enable', { defaultValue: 'Enable TLS' })}
          checked={inbound.tlsEnabled}
          onCheckedChange={checked => set('tlsEnabled', checked)}
        />
      )}

      {enabled && (
        <>
          <TlsIdentityFields fields={inbound} onChange={onChange} alpnPlaceholder="h2" />

          <div className={GROUP_BOX_CLASS}>
            <div className="flex items-center justify-between gap-3">
              <SectionHeader title={t('coreEditor.singbox.tls.reality', { defaultValue: 'REALITY' })} />
              <Switch id={`sb-reality-${uid}`} checked={inbound.realityEnabled} onCheckedChange={checked => set('realityEnabled', checked === true)} />
            </div>
            {inbound.realityEnabled && (
              <div className="grid gap-3 sm:grid-cols-2">
                <Field label={t('coreEditor.singbox.tls.realityHandshakeServer', { defaultValue: 'Handshake server' })} error={realityHandshakeError}>
                  <Input
                    dir="ltr"
                    className={CONTROL_CLASS}
                    isError={!!realityHandshakeError}
                    value={inbound.realityHandshakeServer}
                    onChange={e => set('realityHandshakeServer', e.target.value)}
                    placeholder="www.microsoft.com"
                  />
                </Field>
                <Field label={t('coreEditor.singbox.tls.realityHandshakePort', { defaultValue: 'Handshake port' })}>
                  <Input
                    dir="ltr"
                    type="text"
                    inputMode="numeric"
                    className={CONTROL_CLASS}
                    value={inbound.realityHandshakePort}
                    onChange={e => set('realityHandshakePort', e.target.value)}
                    placeholder="443"
                  />
                </Field>
                <Field className="sm:col-span-2" label={t('coreEditor.singbox.tls.realityPrivateKey', { defaultValue: 'Private key' })} error={realityKeyError}>
                  <PasswordInput
                    dir="ltr"
                    autoComplete="new-password"
                    className={CONTROL_CLASS}
                    isError={!!realityKeyError}
                    value={inbound.realityPrivateKey}
                    onChange={e => set('realityPrivateKey', e.target.value)}
                  />
                </Field>
                <Field label={shortIdLabel}>
                  <StringArrayPopoverInput
                    className={LIST_CONTROL_CLASS}
                    value={[...inbound.realityShortId]}
                    onChange={next => set('realityShortId', next)}
                    placeholder="0123abcd"
                    addPlaceholder="0123abcd"
                    itemsLabel={shortIdLabel}
                  />
                </Field>
                <Field label={t('coreEditor.singbox.tls.realityMaxTimeDiff', { defaultValue: 'Max time difference' })}>
                  <Input dir="ltr" className={CONTROL_CLASS} value={inbound.realityMaxTimeDifference} onChange={e => set('realityMaxTimeDifference', e.target.value)} placeholder="1m" />
                </Field>
              </div>
            )}
          </div>

          {/* Certificate + uTLS (hidden when REALITY owns the handshake) */}
          {!inbound.realityEnabled && (
            <>
              <TlsVersionFields fields={inbound} onChange={onChange} />

              <Field className="sm:col-span-2" label={t('coreEditor.singbox.tls.utls', { defaultValue: 'uTLS fingerprint' })}>
                <div className="flex items-center gap-3">
                  <Switch id={`sb-utls-${uid}`} checked={inbound.utlsEnabled} onCheckedChange={checked => set('utlsEnabled', checked === true)} />
                  <Input
                    dir="ltr"
                    className={CONTROL_CLASS}
                    disabled={!inbound.utlsEnabled}
                    value={inbound.utlsFingerprint}
                    onChange={e => set('utlsFingerprint', e.target.value)}
                    placeholder="chrome"
                  />
                </div>
              </Field>

              {inbound.acmeEnabled ? (
                <p className={NOTE_CLASS}>
                  {t('coreEditor.singbox.tls.acmeOwnsCert', { defaultValue: 'ACME is enabled below and manages the certificate automatically. The manual certificate fields are disabled.' })}
                </p>
              ) : (
                <CertificateFields fields={inbound} onChange={onChange} />
              )}

              <EchFields fields={inbound} onChange={onChange} uid={uid} />
              <AcmeFields fields={inbound} onChange={onChange} uid={uid} domainError={acmeDomainError} tokenError={acmeTokenError} keysError={acmeKeysError} />
            </>
          )}
        </>
      )}
    </>
  )
}

// ============================ Hysteria2 ============================
function Hysteria2Sections({ inbound, issues, onChange, uid }: { inbound: HysteriaInboundDraft; issues: SingBoxValidationIssue[]; onChange: DraftChange; uid: string }) {
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
      <SectionDivider />
      <SectionTitle icon={Gauge} title={t('coreEditor.singbox.bandwidth.title', { defaultValue: 'Bandwidth' })} />
      <Field label={t('coreEditor.singbox.fields.upMbps', { defaultValue: 'Up mbps' })} error={upMbpsError}>
        <Input type="text" inputMode="numeric" value={inbound.upMbps} dir="ltr" className={CONTROL_CLASS} isError={!!upMbpsError} onChange={e => set('upMbps', e.target.value)} placeholder="100" />
      </Field>
      <Field label={t('coreEditor.singbox.fields.downMbps', { defaultValue: 'Down mbps' })} error={downMbpsError}>
        <Input
          type="text"
          inputMode="numeric"
          value={inbound.downMbps}
          dir="ltr"
          className={CONTROL_CLASS}
          isError={!!downMbpsError}
          onChange={e => set('downMbps', e.target.value)}
          placeholder="100"
        />
      </Field>
      <Field className="sm:col-span-2" label={t('coreEditor.singbox.fields.udpTimeout', { defaultValue: 'UDP timeout' })} error={udpTimeoutError}>
        <Input value={inbound.udpTimeout} dir="ltr" className={CONTROL_CLASS} isError={!!udpTimeoutError} onChange={e => set('udpTimeout', e.target.value)} placeholder="30s" />
      </Field>
      <SwitchField
        className="sm:col-span-2"
        id={`sb-ignore-bandwidth-${uid}`}
        label={t('coreEditor.singbox.fields.ignoreClientBandwidth', { defaultValue: 'Ignore client bandwidth' })}
        hint={t('coreEditor.singbox.fields.ignoreClientBandwidthHint', { defaultValue: 'Ignore the bandwidth values reported by the client and always use the values above.' })}
        checked={inbound.ignoreClientBandwidth}
        onCheckedChange={checked => set('ignoreClientBandwidth', checked)}
      />

      <SectionDivider />
      <SectionTitle icon={Shuffle} title={t('coreEditor.singbox.obfs.title', { defaultValue: 'Obfuscation' })} />
      <SwitchField
        className="sm:col-span-2"
        id={`sb-obfs-enabled-${uid}`}
        label={t('coreEditor.singbox.obfs.enable', { defaultValue: 'Salamander obfuscation' })}
        hint={t('coreEditor.singbox.obfs.enableHint', { defaultValue: 'Wraps the QUIC handshake in the Salamander obfuscator. Every client must use the same password.' })}
        checked={inbound.obfsEnabled}
        onCheckedChange={checked => set('obfsEnabled', checked)}
      />
      {inbound.obfsEnabled && (
        <Field className="sm:col-span-2" label={t('coreEditor.singbox.obfs.password', { defaultValue: 'Obfuscation password' })} error={obfsPasswordError}>
          <div className="flex items-center gap-2">
            <PasswordInput
              dir="ltr"
              autoComplete="new-password"
              className={CONTROL_CLASS}
              isError={!!obfsPasswordError}
              value={inbound.obfsPassword}
              onChange={e => set('obfsPassword', e.target.value)}
            />
            <GenerateButton onClick={() => set('obfsPassword', randomObfsPassword())} title={t('coreEditor.singbox.obfs.generate', { defaultValue: 'Generate password' })} />
          </div>
        </Field>
      )}

      <SectionDivider />
      <SectionTitle icon={VenetianMask} title={t('coreEditor.singbox.masquerade.title', { defaultValue: 'Masquerade' })} />
      <Field label={t('coreEditor.singbox.masquerade.type', { defaultValue: 'Type' })}>
        <Select dir="ltr" value={masqType === '' ? MASQ_SIMPLE : masqType} onValueChange={v => set('masqueradeType', (v === MASQ_SIMPLE ? '' : v) as SingBoxMasqueradeType)}>
          <SelectTrigger className={SELECT_TRIGGER_CLASS} dir="ltr">
            <SelectValue />
          </SelectTrigger>
          <SelectContent dir="ltr">
            <SelectItem value={MASQ_SIMPLE}>{t('coreEditor.singbox.masquerade.simple', { defaultValue: 'URL (simple)' })}</SelectItem>
            <SelectItem value="proxy">{t('coreEditor.singbox.masquerade.proxy', { defaultValue: 'Reverse proxy' })}</SelectItem>
            <SelectItem value="file">{t('coreEditor.singbox.masquerade.file', { defaultValue: 'Static files' })}</SelectItem>
            <SelectItem value="string">{t('coreEditor.singbox.masquerade.string', { defaultValue: 'Fixed response' })}</SelectItem>
          </SelectContent>
        </Select>
      </Field>
      {(masqType === '' || masqType === 'proxy') && (
        <Field label={t('coreEditor.singbox.masquerade.url', { defaultValue: 'URL' })} error={masqueradeError}>
          <Input value={inbound.masquerade} dir="ltr" className={CONTROL_CLASS} isError={!!masqueradeError} onChange={e => set('masquerade', e.target.value)} placeholder="https://example.com" />
        </Field>
      )}
      {masqType === 'file' && (
        <Field label={t('coreEditor.singbox.masquerade.directory', { defaultValue: 'Directory' })} error={masqueradeDirectoryError}>
          <Input
            value={inbound.masqueradeDirectory}
            dir="ltr"
            className={CONTROL_CLASS}
            isError={!!masqueradeDirectoryError}
            onChange={e => set('masqueradeDirectory', e.target.value)}
            placeholder="/var/www"
          />
        </Field>
      )}
      {masqType === 'proxy' && (
        <SwitchField
          className="sm:col-span-2"
          id={`sb-masq-rewrite-${uid}`}
          label={t('coreEditor.singbox.masquerade.rewriteHost', { defaultValue: 'Rewrite Host header' })}
          checked={inbound.masqueradeRewriteHost}
          onCheckedChange={checked => set('masqueradeRewriteHost', checked)}
        />
      )}
      {masqType === 'string' && (
        <>
          <Field label={t('coreEditor.singbox.masquerade.statusCode', { defaultValue: 'Status code' })} error={masqueradeStatusError}>
            <Input
              type="text"
              inputMode="numeric"
              value={inbound.masqueradeStatusCode}
              dir="ltr"
              className={CONTROL_CLASS}
              isError={!!masqueradeStatusError}
              onChange={e => set('masqueradeStatusCode', e.target.value)}
              placeholder="404"
            />
          </Field>
          <Field label={t('coreEditor.singbox.masquerade.headers', { defaultValue: 'Headers (JSON)' })} error={masqueradeHeadersError}>
            <Textarea
              dir="ltr"
              rows={3}
              className={cn('font-mono text-xs', masqueradeHeadersError && 'border-destructive')}
              value={inbound.masqueradeHeaders}
              onChange={e => set('masqueradeHeaders', e.target.value)}
              placeholder='{"Server": "nginx"}'
            />
          </Field>
          <Field className="sm:col-span-2" label={t('coreEditor.singbox.masquerade.content', { defaultValue: 'Response body' })}>
            <Textarea dir="ltr" rows={4} className="text-xs" value={inbound.masqueradeContent} onChange={e => set('masqueradeContent', e.target.value)} placeholder="Not Found" />
          </Field>
        </>
      )}

      <SectionDivider />
      <SectionTitle icon={Shield} title={t('coreEditor.singbox.tls.title', { defaultValue: 'TLS' })} />
      <p className={NOTE_CLASS}>{t('coreEditor.singbox.tls.alwaysEnabledHint', { defaultValue: 'Hysteria2 always requires TLS; it cannot be disabled for this inbound.' })}</p>
      <TlsIdentityFields fields={inbound} onChange={onChange} alpnPlaceholder="h3" />
      <TlsVersionFields fields={inbound} onChange={onChange} />
      {inbound.acmeEnabled ? (
        <p className={NOTE_CLASS}>
          {t('coreEditor.singbox.tls.acmeOwnsCert', { defaultValue: 'ACME is enabled below and manages the certificate automatically. The manual certificate fields are disabled.' })}
        </p>
      ) : (
        <CertificateFields fields={inbound} onChange={onChange} />
      )}
      <EchFields fields={inbound} onChange={onChange} uid={uid} />
      <AcmeFields fields={inbound} onChange={onChange} uid={uid} domainError={acmeDomainError} tokenError={acmeTokenError} keysError={acmeKeysError} />

      <SectionDivider />
      <SectionTitle icon={Settings2} title={t('coreEditor.singbox.advanced.title', { defaultValue: 'Advanced' })} />
      <Field className="sm:col-span-2" label={t('coreEditor.singbox.advanced.portHopping', { defaultValue: 'Port hopping range' })} error={portHoppingError}>
        <Input value={inbound.portHoppingRange} dir="ltr" className={CONTROL_CLASS} isError={!!portHoppingError} onChange={e => set('portHoppingRange', e.target.value)} placeholder="20000-50000" />
      </Field>
      <SwitchField
        id={`sb-udp-fragment-${uid}`}
        label={t('coreEditor.singbox.advanced.udpFragment', { defaultValue: 'UDP fragment' })}
        checked={inbound.udpFragment}
        onCheckedChange={checked => set('udpFragment', checked)}
      />
      <SwitchField
        id={`sb-brutal-debug-${uid}`}
        label={t('coreEditor.singbox.advanced.brutalDebug', { defaultValue: 'Brutal debug logging' })}
        checked={inbound.brutalDebug}
        onCheckedChange={checked => set('brutalDebug', checked)}
      />
    </>
  )
}
