import { describe, expect, it } from 'bun:test'

import { DEFAULT_TEMPLATE_CONTENT } from '@/features/templates/forms/client-template-form'
import { ClientTemplateType } from '@/service/api'

describe('default Xray client template', () => {
  it('opens its local proxies on loopback only', () => {
    const template = JSON.parse(DEFAULT_TEMPLATE_CONTENT[ClientTemplateType.xray_subscription]) as { inbounds: Array<{ listen?: string }> }
    const listens = template.inbounds.map(inbound => inbound.listen)
    expect(listens.length).toBeGreaterThan(0)
    expect(listens.every(listen => listen === '127.0.0.1')).toBe(true)
  })
})
