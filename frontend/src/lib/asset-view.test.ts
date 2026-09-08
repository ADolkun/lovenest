import { describe, expect, it } from 'vitest'
import { readAssetView, transactionsForHoldings } from './asset-view'
import type { Asset, AssetTransaction } from '@/types'

describe('consolidated asset navigation', () => {
  it('keeps legacy shared links on their original content', () => {
    expect(readAssetView(new URLSearchParams('tab=positions'))).toEqual({ tab: 'portfolio', view: 'assets', activity: 'trades' })
    expect(readAssetView(new URLSearchParams('tab=holdings')).view).toBe('wallets')
    expect(readAssetView(new URLSearchParams('tab=transactions')).tab).toBe('activity')
    expect(readAssetView(new URLSearchParams('tab=contributions')).activity).toBe('contributions')
    expect(readAssetView(new URLSearchParams('wallet=account-wallet')).view).toBe('wallets')
    expect(readAssetView(new URLSearchParams('tab=activity&activity=wallets&wallet=account-wallet'))).toEqual({ tab: 'activity', view: 'wallets', activity: 'wallets' })
    expect(readAssetView(new URLSearchParams('tab=activity&activity=transfers&wallet=account-wallet'))).toEqual({ tab: 'activity', view: 'wallets', activity: 'transfers' })
    expect(readAssetView(new URLSearchParams('tab=activity&activity=recovery&wallet=account-wallet'))).toEqual({ tab: 'activity', view: 'wallets', activity: 'recovery' })
    expect(readAssetView(new URLSearchParams('tab=unknown&view=unknown&activity=unknown'))).toEqual({ tab: 'portfolio', view: 'assets', activity: 'trades' })
  })

  it('never shows trades outside the selected wallet or collection, including an empty slice', () => {
    const rows = [{ id: 'mine', asset_id: 'inside' }, { id: 'other', asset_id: 'outside' }] as AssetTransaction[]
    const holdings = [{ id: 'inside' }] as Asset[]
    expect(transactionsForHoldings(rows, holdings).map((row) => row.id)).toEqual(['mine'])
    expect(transactionsForHoldings(rows, [])).toEqual([])
  })
})
