import { describe, expect, it } from 'bun:test'

import { canResetAdminUsage, currentAdminUsername, resetEligibleAdmins } from './reset-usage-eligibility'

const self = { username: 'alice', isOwner: false }
const ownerSelf = { username: 'alice', isOwner: true }
const other = { username: 'bob', isOwner: false }

describe('canResetAdminUsage', () => {
  it('hides the reset on the own row of a non-owner admin', () => {
    expect(canResetAdminUsage(true, self, 'alice')).toBe(false)
  })

  it('shows the reset on the own row of an owner admin', () => {
    expect(canResetAdminUsage(true, ownerSelf, 'alice')).toBe(true)
  })

  it('shows the reset on another admin', () => {
    expect(canResetAdminUsage(true, other, 'alice')).toBe(true)
  })

  it('hides the reset when there is no handler', () => {
    expect(canResetAdminUsage(false, other, 'alice')).toBe(false)
  })
})

describe('resetEligibleAdmins', () => {
  it('drops the own row of a non-owner admin from a mixed bulk selection', () => {
    expect(resetEligibleAdmins([self, other], admin => admin, 'alice').map(admin => admin.username)).toEqual(['bob'])
  })

  it('keeps the own row of an owner admin in a bulk selection', () => {
    expect(resetEligibleAdmins([ownerSelf, other], admin => admin, 'alice').map(admin => admin.username)).toEqual(['alice', 'bob'])
  })

  it('drops the own row when the username comes from the current admin accessor', () => {
    expect(resetEligibleAdmins([self, other], admin => admin, currentAdminUsername({ username: 'alice', is_sudo: false })).map(admin => admin.username)).toEqual(['bob'])
  })
})

describe('currentAdminUsername', () => {
  it('reads the username of the loaded current admin', () => {
    expect(currentAdminUsername({ username: 'alice', is_sudo: false })).toBe('alice')
  })

  it('returns undefined before the current admin has loaded', () => {
    expect(currentAdminUsername(null)).toBeUndefined()
    expect(currentAdminUsername(undefined)).toBeUndefined()
  })

  it('returns undefined when the username is missing or not a string', () => {
    expect(currentAdminUsername({ status: 401 })).toBeUndefined()
    expect(currentAdminUsername({ username: 42 })).toBeUndefined()
  })
})
