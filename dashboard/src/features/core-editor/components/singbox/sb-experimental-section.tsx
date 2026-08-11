import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Switch } from '@/components/ui/switch'
import { SbSelectField, SbTextField, useSbDraft, type WireRow } from '@/features/core-editor/components/singbox/sb-section-kit'
import { useCallback } from 'react'
import { useTranslation } from 'react-i18next'

/** experimental.clash_api + experimental.cache_file — two toggle cards, mirroring the spec. */
export function SbExperimentalSection() {
  const { t } = useTranslation()
  const { draft, updateSbDraft } = useSbDraft()
  const experimental = (draft?.experimental ?? {}) as WireRow
  const clash = (experimental.clash_api ?? null) as WireRow | null
  const cache = (experimental.cache_file ?? null) as WireRow | null

  const setBlock = useCallback(
    (key: 'clash_api' | 'cache_file', value: WireRow | undefined) =>
      updateSbDraft(d => {
        const exp = { ...(d.experimental as WireRow) }
        if (value === undefined) delete exp[key]
        else exp[key] = value
        return { ...d, experimental: exp as never }
      }),
    [updateSbDraft],
  )
  const setClash = (k: string, v: unknown) => setBlock('clash_api', { ...(clash ?? {}), [k]: v })
  const setCache = (k: string, v: unknown) => setBlock('cache_file', { ...(cache ?? {}), [k]: v })

  return (
    <div className="space-y-6">
      <Card>
        <CardHeader className="flex-row items-center justify-between space-y-0 pb-3">
          <CardTitle className="text-sm">{t('coreEditor.singbox.clashApi', { defaultValue: 'Clash API' })}</CardTitle>
          <Switch checked={clash !== null} onCheckedChange={v => setBlock('clash_api', v ? { external_controller: '127.0.0.1:9090' } : undefined)} />
        </CardHeader>
        {clash !== null && (
          <CardContent className="grid grid-cols-1 gap-4 sm:grid-cols-2">
            <SbTextField label={t('coreEditor.singbox.externalController', { defaultValue: 'External controller' })} value={clash.external_controller} onChange={v => setClash('external_controller', v)} placeholder="127.0.0.1:9090" />
            <SbTextField label={t('coreEditor.singbox.externalUi', { defaultValue: 'External UI' })} value={clash.external_ui} onChange={v => setClash('external_ui', v || undefined)} placeholder="ui" />
            <SbTextField label={t('coreEditor.singbox.secret', { defaultValue: 'Secret' })} value={clash.secret} onChange={v => setClash('secret', v || undefined)} />
            <SbSelectField label={t('coreEditor.singbox.defaultMode', { defaultValue: 'Default mode' })} value={clash.default_mode} onChange={v => setClash('default_mode', v || undefined)} options={[{ value: '' }, { value: 'rule' }, { value: 'global' }, { value: 'direct' }]} />
          </CardContent>
        )}
      </Card>

      <Card>
        <CardHeader className="flex-row items-center justify-between space-y-0 pb-3">
          <CardTitle className="text-sm">{t('coreEditor.singbox.cacheFile', { defaultValue: 'Cache file' })}</CardTitle>
          <Switch checked={cache !== null} onCheckedChange={v => setBlock('cache_file', v ? { enabled: true } : undefined)} />
        </CardHeader>
        {cache !== null && (
          <CardContent className="grid grid-cols-1 gap-4 sm:grid-cols-2">
            <SbTextField label={t('coreEditor.singbox.cachePath', { defaultValue: 'Path' })} value={cache.path} onChange={v => setCache('path', v || undefined)} placeholder="cache.db" />
            <SbTextField label={t('coreEditor.singbox.cacheId', { defaultValue: 'Cache ID' })} value={cache.cache_id} onChange={v => setCache('cache_id', v || undefined)} />
          </CardContent>
        )}
      </Card>
    </div>
  )
}
