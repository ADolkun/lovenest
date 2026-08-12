import { describe, expect, it } from 'vitest'
import {
  buildAllowlist,
  initialSelection,
  pendingAccountCount,
  shouldSaveAllowlist,
} from './account-allowlist'
import type { ProviderAccount } from '../types'

const account = (
  external_id: string,
  status: ProviderAccount['status'],
  has_holdings = false,
): ProviderAccount => ({
  external_id,
  name: `Account ${external_id}`,
  balance: '10.00',
  currency: 'USD',
  has_holdings,
  status,
})

describe('pendingAccountCount', () => {
  it('is zero without an allowlist — every account syncs, nothing awaits review', () => {
    expect(pendingAccountCount({ seen_account_ids: ['a', 'b'] })).toBe(0)
    expect(pendingAccountCount(null)).toBe(0)
  })

  it('counts seen accounts that are neither allowlisted nor already reviewed', () => {
    expect(
      pendingAccountCount({
        account_allowlist: ['a'],
        seen_account_ids: ['a', 'b', 'c'],
        reviewed_account_ids: ['a', 'b'],
      }),
    ).toBe(1)
  })

  it('is zero when the reviewed set was never pinned', () => {
    expect(
      pendingAccountCount({ account_allowlist: ['a'], seen_account_ids: ['a', 'b'] }),
    ).toBe(0)
  })

  it('treats an empty allowlist as a real selection, not as absent', () => {
    expect(
      pendingAccountCount({
        account_allowlist: [],
        seen_account_ids: ['a'],
        reviewed_account_ids: [],
      }),
    ).toBe(1)
  })
})

describe('initialSelection', () => {
  it('ticks the included accounts only', () => {
    const selected = initialSelection([
      account('a', 'included'),
      account('b', 'excluded'),
      account('c', 'pending'),
    ])
    expect([...selected]).toEqual(['a'])
  })

  it('does not tick a pending account because it reports holdings', () => {
    const selected = initialSelection([account('c', 'pending', true)])
    expect(selected.size).toBe(0)
  })
})

describe('shouldSaveAllowlist', () => {
  const legacy = [account('a', 'included'), account('b', 'included')]

  it('leaves a connection that never configured one on legacy behaviour', () => {
    expect(shouldSaveAllowlist(initialSelection(legacy), legacy, null)).toBe(false)
  })

  it('opts in once the user unticks something', () => {
    expect(shouldSaveAllowlist(new Set(['a']), legacy, null)).toBe(true)
  })

  it('keeps writing for a connection that already has one, untouched or not', () => {
    const configured = [account('a', 'included'), account('b', 'excluded')]
    expect(shouldSaveAllowlist(initialSelection(configured), configured, ['a'])).toBe(true)
    expect(shouldSaveAllowlist(new Set(), configured, [])).toBe(true)
  })

  it('ignores a tick that was undone', () => {
    const configured = [account('a', 'included'), account('b', 'excluded')]
    expect(shouldSaveAllowlist(new Set(['a']), configured, null)).toBe(false)
  })
})

describe('buildAllowlist', () => {
  const shown = [account('a', 'included'), account('b', 'excluded')]

  it('saves the ticked accounts', () => {
    expect(buildAllowlist(new Set(['a']), shown, null)).toEqual(['a'])
  })

  it('saves an empty list when nothing is ticked', () => {
    expect(buildAllowlist(new Set(), shown, ['a'])).toEqual([])
  })

  it('keeps stored ids the provider no longer exposes', () => {
    expect(buildAllowlist(new Set(['a']), shown, ['a', 'gone'])).toEqual(['a', 'gone'])
  })

  it('ignores ticks for accounts that are not on screen', () => {
    expect(buildAllowlist(new Set(['a', 'ghost']), shown, null)).toEqual(['a'])
  })
})
