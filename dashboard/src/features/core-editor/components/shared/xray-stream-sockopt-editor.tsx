import { Accordion, AccordionContent, AccordionItem, AccordionTrigger } from '@/components/ui/accordion'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { Separator } from '@/components/ui/separator'
import { Switch } from '@/components/ui/switch'
import { Textarea } from '@/components/ui/textarea'
import { cn } from '@/lib/utils'
import { SlidersHorizontal } from 'lucide-react'
import { useCallback, useEffect, useMemo, useState } from 'react'
import { toast } from 'sonner'

export type SockoptJson = Record<string, unknown>

const DOMAIN_STRATEGIES = ['AsIs', 'UseIP', 'UseIPv6v4', 'UseIPv6', 'UseIPv4v6', 'UseIPv4', 'ForceIP', 'ForceIPv6v4', 'ForceIPv6', 'ForceIPv4v6', 'ForceIPv4'] as const

const ADDRESS_PORT_STRATEGIES = ['none', 'SrvPortOnly', 'SrvAddressOnly', 'SrvPortAndAddress', 'TxtPortOnly', 'TxtAddressOnly', 'TxtPortAndAddress'] as const

const TPROXY_VALUES = ['off', 'redirect', 'tproxy'] as const

function pruneHappyEyeballs(h: Record<string, unknown>): Record<string, unknown> | undefined {
  const out: Record<string, unknown> = {}
  if (typeof h.tryDelayMs === 'number' && Number.isFinite(h.tryDelayMs)) out.tryDelayMs = h.tryDelayMs
  if (typeof h.prioritizeIPv6 === 'boolean') out.prioritizeIPv6 = h.prioritizeIPv6
  if (typeof h.interleave === 'number' && Number.isFinite(h.interleave)) out.interleave = h.interleave
  if (typeof h.maxConcurrentTry === 'number' && Number.isFinite(h.maxConcurrentTry)) out.maxConcurrentTry = h.maxConcurrentTry
  return Object.keys(out).length > 0 ? out : undefined
}

/** Drop empty / unset sockopt keys before persisting. */
export function pruneSockoptObject(raw: SockoptJson | undefined): SockoptJson | undefined {
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return undefined
  const out: SockoptJson = {}
  for (const [k, v] of Object.entries(raw)) {
    if (v === undefined || v === null) continue
    if (v === '') continue
    if (k === 'happyEyeballs' && typeof v === 'object' && v !== null && !Array.isArray(v)) {
      const pr = pruneHappyEyeballs(v as Record<string, unknown>)
      if (pr) out[k] = pr
      continue
    }
    if (k === 'customSockopt' && Array.isArray(v)) {
      if (v.length > 0) out[k] = v
      continue
    }
    if (typeof v === 'number' && !Number.isFinite(v)) continue
    out[k] = v
  }
  return Object.keys(out).length > 0 ? out : undefined
}

function numOrUndef(raw: string): number | undefined {
  const t = raw.trim()
  if (t === '') return undefined
  const n = Number(t)
  return Number.isFinite(n) ? n : undefined
}

function tcpFastOpenSelectValue(v: unknown): 'default' | 'false' | 'true' | 'number' {
  if (v === undefined) return 'default'
  if (v === false) return 'false'
  if (v === true) return 'true'
  if (typeof v === 'number' && Number.isFinite(v)) return 'number'
  return 'default'
}

export interface XrayStreamSockoptFieldsProps {
  variant: 'inbound' | 'outbound'
  /** Current `streamSettings.sockopt` (outbound) or `streamAdvanced.sockopt` (inbound). */
  value: SockoptJson | undefined
  onChange: (next: SockoptJson | undefined) => void
  t: (key: string, opts?: Record<string, unknown>) => string
  /** Outbound tags for `dialerProxy` (excluding current outbound tag when applicable). */
  dialerProxyTags?: readonly string[]
  /** When false, only the enable row is shown until the user turns options on. */
  showEnableRow?: boolean
  /** Optional class on the outer wrapper (plain layout). */
  className?: string
}

/**
 * Form controls for Xray `SockoptObject` (`streamSettings.sockopt` / inbound `streamAdvanced.sockopt`).
 */
export function XrayStreamSockoptFields({ variant, value, onChange, t, dialerProxyTags = [], showEnableRow = true, className }: XrayStreamSockoptFieldsProps) {
  const cur = useMemo(() => ({ ...(value ?? {}) }) as SockoptJson, [value])
  const [tfoNumber, setTfoNumber] = useState(() => (typeof value?.tcpFastOpen === 'number' && Number.isFinite(value.tcpFastOpen) ? String(value.tcpFastOpen) : ''))
  const [customSockoptText, setCustomSockoptText] = useState(() => {
    const c = value?.customSockopt
    if (Array.isArray(c)) {
      try {
        return JSON.stringify(c, null, 2)
      } catch {
        return '[]'
      }
    }
    return ''
  })

  useEffect(() => {
    const c = value?.customSockopt
    if (Array.isArray(c)) {
      try {
        setCustomSockoptText(JSON.stringify(c, null, 2))
      } catch {
        setCustomSockoptText('[]')
      }
    } else {
      setCustomSockoptText('')
    }
  }, [value?.customSockopt])

  useEffect(() => {
    if (typeof value?.tcpFastOpen === 'number' && Number.isFinite(value.tcpFastOpen)) {
      setTfoNumber(String(value.tcpFastOpen))
    } else if (value?.tcpFastOpen !== true && value?.tcpFastOpen !== false) {
      setTfoNumber('')
    }
  }, [value?.tcpFastOpen])

  const patch = useCallback(
    (partial: SockoptJson) => {
      const next = { ...(value ?? {}), ...partial } as SockoptJson
      for (const k of Object.keys(partial)) {
        if (partial[k] === undefined) delete next[k]
      }
      onChange(pruneSockoptObject(next))
    },
    [value, onChange],
  )

  const sockoptConfigured = pruneSockoptObject(value) != null

  const hb = (cur.happyEyeballs && typeof cur.happyEyeballs === 'object' && !Array.isArray(cur.happyEyeballs) ? (cur.happyEyeballs as Record<string, unknown>) : {}) as Record<string, unknown>
  const happyEnabled = pruneHappyEyeballs(hb) != null

  const domainStrategy = typeof cur.domainStrategy === 'string' && (DOMAIN_STRATEGIES as readonly string[]).includes(cur.domainStrategy) ? cur.domainStrategy : 'AsIs'
  const tproxy = typeof cur.tproxy === 'string' && (TPROXY_VALUES as readonly string[]).includes(cur.tproxy) ? cur.tproxy : 'off'
  const addrPort = typeof cur.addressPortStrategy === 'string' && (ADDRESS_PORT_STRATEGIES as readonly string[]).includes(cur.addressPortStrategy) ? cur.addressPortStrategy : 'none'

  return (
    <div className={cn('flex flex-col gap-4', className)}>
      {showEnableRow ? (
        <div className="flex items-center justify-between rounded-lg border px-3 py-2.5">
          <div>
            <p className="text-sm font-medium">{t('coreEditor.sockopt.enable')}</p>
            <p className="text-muted-foreground text-xs">{t('coreEditor.sockopt.enableHint')}</p>
          </div>
          <Switch
            checked={sockoptConfigured}
            onCheckedChange={checked => {
              if (checked) onChange({ domainStrategy: 'AsIs' })
              else {
                setTfoNumber('')
                setCustomSockoptText('')
                onChange(undefined)
              }
            }}
          />
        </div>
      ) : null}

      {sockoptConfigured ? (
        <div className="grid gap-3 sm:grid-cols-2">
          <div className="space-y-1.5 sm:col-span-2">
            <Label className="text-muted-foreground text-xs font-semibold tracking-wide">{t('coreEditor.sockopt.domainStrategy')}</Label>
            <Select dir="ltr" value={domainStrategy} onValueChange={v => patch({ domainStrategy: v })}>
              <SelectTrigger className="h-10 w-full min-w-0" dir="ltr">
                <SelectValue />
              </SelectTrigger>
              <SelectContent dir="ltr" className="max-h-72">
                {DOMAIN_STRATEGIES.map(ds => (
                  <SelectItem key={ds} value={ds}>
                    {ds}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            <p className="text-muted-foreground text-[11px]">{t('coreEditor.sockopt.domainStrategyHint')}</p>
          </div>

          <div className="space-y-1.5">
            <Label className="text-muted-foreground text-xs font-semibold tracking-wide">{t('coreEditor.sockopt.tproxy')}</Label>
            <Select dir="ltr" value={tproxy} onValueChange={v => patch({ tproxy: v })}>
              <SelectTrigger className="h-10 w-full min-w-0" dir="ltr">
                <SelectValue />
              </SelectTrigger>
              <SelectContent dir="ltr">
                {TPROXY_VALUES.map(x => (
                  <SelectItem key={x} value={x}>
                    {x}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>

          <div className="space-y-1.5">
            <Label className="text-muted-foreground text-xs font-semibold tracking-wide">{t('coreEditor.sockopt.addressPortStrategy')}</Label>
            <Select dir="ltr" value={addrPort} onValueChange={v => patch({ addressPortStrategy: v })}>
              <SelectTrigger className="h-10 w-full min-w-0" dir="ltr">
                <SelectValue />
              </SelectTrigger>
              <SelectContent dir="ltr">
                {ADDRESS_PORT_STRATEGIES.map(x => (
                  <SelectItem key={x} value={x}>
                    {x}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>

          {variant === 'inbound' ? (
            <div className="flex items-center justify-between rounded-lg border px-3 py-2.5 sm:col-span-2">
              <div>
                <p className="text-sm font-medium">{t('coreEditor.sockopt.acceptProxyProtocol')}</p>
                <p className="text-muted-foreground text-xs">{t('coreEditor.sockopt.acceptProxyProtocolHint')}</p>
              </div>
              <Switch checked={cur.acceptProxyProtocol === true} onCheckedChange={checked => patch({ acceptProxyProtocol: checked ? true : undefined })} />
            </div>
          ) : null}

          {variant === 'outbound' ? (
            <div className="space-y-1.5 sm:col-span-2">
              <Label className="text-muted-foreground text-xs font-semibold tracking-wide">{t('coreEditor.sockopt.dialerProxy')}</Label>
              {dialerProxyTags.length > 0 ? (
                <Select
                  dir="ltr"
                  value={typeof cur.dialerProxy === 'string' && cur.dialerProxy ? cur.dialerProxy : '__none'}
                  onValueChange={v => patch({ dialerProxy: v === '__none' ? undefined : v })}
                >
                  <SelectTrigger className="h-10 w-full min-w-0" dir="ltr">
                    <SelectValue placeholder={t('coreEditor.sockopt.dialerProxyPlaceholder')} />
                  </SelectTrigger>
                  <SelectContent dir="ltr">
                    <SelectItem value="__none">{t('coreEditor.sockopt.dialerProxyNone')}</SelectItem>
                    {dialerProxyTags.map(tag => (
                      <SelectItem key={tag} value={tag}>
                        {tag}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              ) : (
                <Input
                  dir="ltr"
                  className="h-10 font-mono text-xs"
                  placeholder={t('coreEditor.sockopt.dialerProxyPlaceholder')}
                  defaultValue={typeof cur.dialerProxy === 'string' ? cur.dialerProxy : ''}
                  key={String(cur.dialerProxy ?? '')}
                  onBlur={e => {
                    const v = e.target.value.trim()
                    patch({ dialerProxy: v === '' ? undefined : v })
                  }}
                />
              )}
              <p className="text-muted-foreground text-[11px]">{t('coreEditor.sockopt.dialerProxyHint')}</p>
            </div>
          ) : null}

          <div className="space-y-1.5">
            <Label className="text-muted-foreground text-xs font-semibold tracking-wide">{t('coreEditor.sockopt.mark')}</Label>
            <Input
              dir="ltr"
              className="h-10 font-mono text-xs"
              inputMode="numeric"
              placeholder="0"
              defaultValue={typeof cur.mark === 'number' ? String(cur.mark) : ''}
              key={`mark-${String(cur.mark ?? '')}`}
              onBlur={e => {
                const n = numOrUndef(e.target.value)
                patch({ mark: n === undefined || n === 0 ? undefined : Math.trunc(n) })
              }}
            />
            <p className="text-muted-foreground text-[11px]">{t('coreEditor.sockopt.markHint')}</p>
          </div>

          <div className="space-y-1.5">
            <Label className="text-muted-foreground text-xs font-semibold tracking-wide">{t('coreEditor.sockopt.interface')}</Label>
            <Input
              dir="ltr"
              className="h-10 font-mono text-xs"
              placeholder="wg0"
              defaultValue={typeof cur.interface === 'string' ? cur.interface : ''}
              key={`if-${String(cur.interface ?? '')}`}
              onBlur={e => {
                const v = e.target.value.trim()
                patch({ interface: v === '' ? undefined : v })
              }}
            />
          </div>

          <div className="space-y-1.5">
            <Label className="text-muted-foreground text-xs font-semibold tracking-wide">{t('coreEditor.sockopt.tcpCongestion')}</Label>
            <Input
              dir="ltr"
              className="h-10 font-mono text-xs"
              placeholder="bbr"
              defaultValue={typeof cur.tcpCongestion === 'string' ? cur.tcpCongestion : ''}
              key={`cc-${String(cur.tcpCongestion ?? '')}`}
              onBlur={e => {
                const v = e.target.value.trim()
                patch({ tcpCongestion: v === '' ? undefined : v })
              }}
            />
            <p className="text-muted-foreground text-[11px]">{t('coreEditor.sockopt.tcpCongestionHint')}</p>
          </div>

          <div className="space-y-1.5">
            <Label className="text-muted-foreground text-xs font-semibold tracking-wide">{t('coreEditor.sockopt.tcpMaxSeg')}</Label>
            <Input
              dir="ltr"
              className="h-10 font-mono text-xs"
              inputMode="numeric"
              defaultValue={typeof cur.tcpMaxSeg === 'number' ? String(cur.tcpMaxSeg) : ''}
              key={`mss-${String(cur.tcpMaxSeg ?? '')}`}
              onBlur={e => patch({ tcpMaxSeg: numOrUndef(e.target.value) })}
            />
          </div>

          <div className="space-y-1.5">
            <Label className="text-muted-foreground text-xs font-semibold tracking-wide">{t('coreEditor.sockopt.tcpUserTimeout')}</Label>
            <Input
              dir="ltr"
              className="h-10 font-mono text-xs"
              inputMode="numeric"
              placeholder="ms"
              defaultValue={typeof cur.tcpUserTimeout === 'number' ? String(cur.tcpUserTimeout) : ''}
              key={`tut-${String(cur.tcpUserTimeout ?? '')}`}
              onBlur={e => patch({ tcpUserTimeout: numOrUndef(e.target.value) })}
            />
          </div>

          <div className="space-y-1.5">
            <Label className="text-muted-foreground text-xs font-semibold tracking-wide">{t('coreEditor.sockopt.tcpKeepAliveIdle')}</Label>
            <Input
              dir="ltr"
              className="h-10 font-mono text-xs"
              inputMode="numeric"
              defaultValue={typeof cur.tcpKeepAliveIdle === 'number' ? String(cur.tcpKeepAliveIdle) : ''}
              key={`kai-${String(cur.tcpKeepAliveIdle ?? '')}`}
              onBlur={e => patch({ tcpKeepAliveIdle: numOrUndef(e.target.value) })}
            />
          </div>

          <div className="space-y-1.5">
            <Label className="text-muted-foreground text-xs font-semibold tracking-wide">{t('coreEditor.sockopt.tcpKeepAliveInterval')}</Label>
            <Input
              dir="ltr"
              className="h-10 font-mono text-xs"
              inputMode="numeric"
              defaultValue={typeof cur.tcpKeepAliveInterval === 'number' ? String(cur.tcpKeepAliveInterval) : ''}
              key={`kiv-${String(cur.tcpKeepAliveInterval ?? '')}`}
              onBlur={e => patch({ tcpKeepAliveInterval: numOrUndef(e.target.value) })}
            />
          </div>

          <div className="space-y-1.5">
            <Label className="text-muted-foreground text-xs font-semibold tracking-wide">{t('coreEditor.sockopt.tcpWindowClamp')}</Label>
            <Input
              dir="ltr"
              className="h-10 font-mono text-xs"
              inputMode="numeric"
              defaultValue={typeof cur.tcpWindowClamp === 'number' ? String(cur.tcpWindowClamp) : ''}
              key={`twc-${String(cur.tcpWindowClamp ?? '')}`}
              onBlur={e => patch({ tcpWindowClamp: numOrUndef(e.target.value) })}
            />
          </div>

          <div className="space-y-1.5">
            <Label className="text-muted-foreground text-xs font-semibold tracking-wide">{t('coreEditor.sockopt.tcpFastOpen')}</Label>
            <div className="flex flex-col gap-2 sm:flex-row sm:items-center">
              <Select
                dir="ltr"
                value={tcpFastOpenSelectValue(cur.tcpFastOpen)}
                onValueChange={mode => {
                  if (mode === 'default') {
                    setTfoNumber('')
                    patch({ tcpFastOpen: undefined })
                  } else if (mode === 'false') {
                    setTfoNumber('')
                    patch({ tcpFastOpen: false })
                  } else if (mode === 'true') {
                    setTfoNumber('')
                    patch({ tcpFastOpen: true })
                  } else {
                    const n = numOrUndef(tfoNumber)
                    if (n !== undefined) patch({ tcpFastOpen: n })
                  }
                }}
              >
                <SelectTrigger className="h-10 w-full min-w-0 sm:max-w-xs" dir="ltr">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent dir="ltr">
                  <SelectItem value="default">{t('coreEditor.sockopt.tcpFastOpenDefault')}</SelectItem>
                  <SelectItem value="false">{t('coreEditor.sockopt.tcpFastOpenOff')}</SelectItem>
                  <SelectItem value="true">{t('coreEditor.sockopt.tcpFastOpenOn')}</SelectItem>
                  <SelectItem value="number">{t('coreEditor.sockopt.tcpFastOpenNumber')}</SelectItem>
                </SelectContent>
              </Select>
              {tcpFastOpenSelectValue(cur.tcpFastOpen) === 'number' ? (
                <Input
                  dir="ltr"
                  className="h-10 font-mono text-xs sm:max-w-[10rem]"
                  inputMode="numeric"
                  value={tfoNumber}
                  onChange={e => setTfoNumber(e.target.value)}
                  onBlur={() => {
                    const n = numOrUndef(tfoNumber)
                    patch({ tcpFastOpen: n ?? undefined })
                  }}
                />
              ) : null}
            </div>
          </div>

          <div className="flex items-center justify-between rounded-lg border px-3 py-2.5">
            <div>
              <p className="text-sm font-medium">{t('coreEditor.sockopt.tcpMptcp')}</p>
              <p className="text-muted-foreground text-xs">{t('coreEditor.sockopt.tcpMptcpHint')}</p>
            </div>
            <Switch checked={cur.tcpMptcp === true} onCheckedChange={checked => patch({ tcpMptcp: checked ? true : undefined })} />
          </div>

          <div className="flex items-center justify-between rounded-lg border px-3 py-2.5">
            <div>
              <p className="text-sm font-medium">{t('coreEditor.sockopt.v6only')}</p>
              <p className="text-muted-foreground text-xs">{t('coreEditor.sockopt.v6onlyHint')}</p>
            </div>
            <Switch checked={cur.v6only === true} onCheckedChange={checked => patch({ v6only: checked ? true : undefined })} />
          </div>

          <Separator className="sm:col-span-2" />
          <div className="flex flex-col gap-2 sm:col-span-2">
            <div className="flex items-center justify-between rounded-lg border px-3 py-2.5">
              <div>
                <p className="text-sm font-medium">{t('coreEditor.sockopt.happyEyeballs')}</p>
                <p className="text-muted-foreground text-xs">{t('coreEditor.sockopt.happyEyeballsHint')}</p>
              </div>
              <Switch
                checked={happyEnabled}
                onCheckedChange={checked => {
                  if (!checked) patch({ happyEyeballs: undefined })
                  else patch({ happyEyeballs: { tryDelayMs: 250, prioritizeIPv6: false, interleave: 1, maxConcurrentTry: 4 } })
                }}
              />
            </div>
            {happyEnabled ? (
              <div className="grid gap-3 rounded-md border border-dashed p-3 sm:grid-cols-2">
                <div className="space-y-1.5">
                  <Label className="text-muted-foreground text-xs font-semibold">{t('coreEditor.sockopt.hbTryDelayMs')}</Label>
                  <Input
                    dir="ltr"
                    className="h-9 font-mono text-xs"
                    inputMode="numeric"
                    defaultValue={typeof hb.tryDelayMs === 'number' ? String(hb.tryDelayMs) : '250'}
                    key={`hbtd-${String(hb.tryDelayMs)}`}
                    onBlur={e => {
                      const n = numOrUndef(e.target.value)
                      patch({
                        happyEyeballs: {
                          ...hb,
                          tryDelayMs: n ?? 250,
                        },
                      })
                    }}
                  />
                </div>
                <div className="flex items-center justify-between gap-3 rounded-md border px-2 py-1.5">
                  <span className="text-xs font-medium">{t('coreEditor.sockopt.hbPrioritizeIPv6')}</span>
                  <Switch
                    checked={hb.prioritizeIPv6 === true}
                    onCheckedChange={checked =>
                      patch({
                        happyEyeballs: { ...hb, prioritizeIPv6: checked },
                      })
                    }
                  />
                </div>
                <div className="space-y-1.5">
                  <Label className="text-muted-foreground text-xs font-semibold">{t('coreEditor.sockopt.hbInterleave')}</Label>
                  <Input
                    dir="ltr"
                    className="h-9 font-mono text-xs"
                    inputMode="numeric"
                    defaultValue={typeof hb.interleave === 'number' ? String(hb.interleave) : '1'}
                    key={`hbi-${String(hb.interleave)}`}
                    onBlur={e => {
                      const n = numOrUndef(e.target.value)
                      patch({ happyEyeballs: { ...hb, interleave: n ?? 1 } })
                    }}
                  />
                </div>
                <div className="space-y-1.5">
                  <Label className="text-muted-foreground text-xs font-semibold">{t('coreEditor.sockopt.hbMaxConcurrentTry')}</Label>
                  <Input
                    dir="ltr"
                    className="h-9 font-mono text-xs"
                    inputMode="numeric"
                    defaultValue={typeof hb.maxConcurrentTry === 'number' ? String(hb.maxConcurrentTry) : '4'}
                    key={`hbm-${String(hb.maxConcurrentTry)}`}
                    onBlur={e => {
                      const n = numOrUndef(e.target.value)
                      patch({ happyEyeballs: { ...hb, maxConcurrentTry: n ?? 4 } })
                    }}
                  />
                </div>
              </div>
            ) : null}
          </div>
        </div>
      ) : null}
    </div>
  )
}

export interface XrayStreamSockoptInboundAccordionProps {
  accordionItemClassName: string
  value: SockoptJson | undefined
  onChange: (next: SockoptJson | undefined) => void
  t: (key: string, opts?: Record<string, unknown>) => string
  dialerProxyTags?: readonly string[]
}

export function XrayStreamSockoptInboundAccordion({ accordionItemClassName, value, onChange, t }: XrayStreamSockoptInboundAccordionProps) {
  return (
    <Accordion type="single" collapsible className="mt-0! sm:col-span-2">
      <AccordionItem value="sockopt" className={accordionItemClassName}>
        <AccordionTrigger>
          <div className="flex items-center gap-2">
            <SlidersHorizontal className="text-muted-foreground h-3.5 w-3.5 shrink-0" aria-hidden />
            <span>{t('coreEditor.sockopt.section')}</span>
          </div>
        </AccordionTrigger>
        <AccordionContent className="pt-0 pb-3">
          <XrayStreamSockoptFields variant="inbound" value={value} onChange={onChange} t={t} />
        </AccordionContent>
      </AccordionItem>
    </Accordion>
  )
}
