import { expect, test } from 'bun:test'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'

const dashboard = join(import.meta.dir, '../..')
const hostModal = readFileSync(join(dashboard, 'features/hosts/dialogs/host-modal.tsx'), 'utf8')
const hostsPage = readFileSync(join(dashboard, 'pages/_dashboard.hosts.tsx'), 'utf8')
const formFields = ['mihomo_ech_config', 'mihomo_ech_query_server_name', 'sing_box_ech_config', 'sing_box_ech_query_server_name']

test('the hosts page still sends the per-client ECH fields', () => {
  for (const field of formFields) expect(hostsPage).toContain(field)
})

test('the host dialog renders an input for every per-client ECH field it sends', () => {
  const rendered = formFields.filter(field => hostModal.includes(`name="${field}"`))
  expect(rendered).toEqual(formFields)
})
