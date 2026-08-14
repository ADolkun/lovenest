import type { Asset, AssetGroup, TaxTreatment } from '@/types'

// CONTEXT.md: a Cash Equivalent behaves as Liquid Cash, so it is never an
// invested position. The classification is the user's, stored on `Asset.type`;
// the backend only seeds the guess for well-known tickers.
export const CASH_EQUIVALENT_TYPE = 'cash_equivalent'

// CONTEXT.md: Dust is a Holding worth under one dollar. An absolute test, not
// a share of the portfolio, so a position does not reclassify itself every
// time the market moves.
export const DUST_THRESHOLD = 1

export const UNKNOWN_ACCOUNT_TYPE = 'unknown'

/** One Holding — a ticker in a single Wallet, i.e. a single account. */
export interface PositionLeg {
  assetId: string
  walletId: string | null
  walletName: string | null
  accountType: string | null
  taxTreatment: TaxTreatment | null
  /** Classification of this Holding, which the user owns and can change. */
  assetType: string
  quantity: number
  value: number
  /** Null when the provider reported no basis (a Snapshot Holding). */
  costBasis: number | null
  gain: number | null
}

/** One ticker, consolidated across every account that holds it. */
export interface Position {
  ticker: string
  name: string
  logoUrl: string | null
  assetType: string
  quantity: number
  value: number
  costBasis: number | null
  /** Weighted-average cost per unit, across accounts. */
  averageCost: number | null
  gain: number | null
  /** Unrealised gain as a fraction of cost basis; null when basis is unknown or zero. */
  gainPct: number | null
  /** Share of the invested total. Null for what allocation excludes. */
  weight: number | null
  isDust: boolean
  isCashEquivalent: boolean
  legs: PositionLeg[]
}

export interface AllocationSlice {
  key: string
  value: number
  weight: number
}

export interface Portfolio {
  /** Ranked by value, every position — read `isDust` to drop them from a ranking. */
  positions: Position[]
  /** Everything, dust and cash equivalents included. */
  total: number
  /** The weight denominator: excludes dust and cash equivalents. */
  investedTotal: number
  cashEquivalentTotal: number
  dustTotal: number
  byAssetClass: AllocationSlice[]
  byAccountType: AllocationSlice[]
}

function primaryValue(asset: Asset): number {
  return Number(asset.current_value_primary ?? asset.current_value ?? 0)
}

function hasValue(asset: Asset): boolean {
  return (asset.current_value_primary ?? asset.current_value) !== null
}

/**
 * Cost basis in the primary currency. `total_invested` is in the holding's own
 * currency and has no converted twin, but value and gain are both converted at
 * one rate — so their difference is the basis at that same rate.
 */
function primaryCostBasis(asset: Asset): number | null {
  const gain = asset.gain_loss_primary ?? asset.gain_loss
  if (gain === null || gain === undefined) return null
  return primaryValue(asset) - Number(gain)
}

function sortByValueDesc<T extends { value: number }>(items: T[]): T[] {
  return [...items].sort((a, b) => b.value - a.value)
}

function allocate(totals: Map<string, number>, denominator: number): AllocationSlice[] {
  return sortByValueDesc(
    [...totals].map(([key, value]) => ({
      key,
      value,
      weight: denominator > 0 ? value / denominator : 0,
    })),
  )
}

/**
 * Consolidate holdings into one position per ticker, with each position broken
 * down per account, plus the allocation and weight figures derived from them.
 *
 * Sold and archived holdings are out; so is anything without a ticker, which is
 * an Asset (property, a vehicle) rather than a Holding.
 *
 * Dust is tested on the consolidated position rather than each leg: a ticker
 * worth under a dollar in total is what buries a real position in a ranking,
 * and by definition every leg of it is dust too.
 */
export function buildPortfolio(assets: Asset[], wallets: AssetGroup[]): Portfolio {
  const walletsById = new Map(wallets.map((w) => [w.id, w]))
  const byTicker = new Map<string, Asset[]>()

  for (const asset of assets) {
    if (asset.is_archived || asset.sell_date) continue
    const ticker = asset.ticker?.trim().toUpperCase()
    if (!ticker) continue
    const group = byTicker.get(ticker)
    if (group) group.push(asset)
    else byTicker.set(ticker, [asset])
  }

  const positions: Position[] = []
  for (const [ticker, holdings] of byTicker) {
    const ranked = [...holdings].sort((a, b) => primaryValue(b) - primaryValue(a))
    const legs: PositionLeg[] = ranked.map((asset) => {
      const wallet = asset.group_id ? walletsById.get(asset.group_id) : undefined
      const costBasis = primaryCostBasis(asset)
      return {
        assetId: asset.id,
        walletId: asset.group_id,
        walletName: wallet?.name ?? null,
        accountType: wallet?.account_type ?? null,
        taxTreatment: wallet?.tax_treatment ?? null,
        assetType: asset.type,
        quantity: Number(asset.units ?? 0),
        value: primaryValue(asset),
        costBasis,
        gain: costBasis === null ? null : primaryValue(asset) - costBasis,
      }
    })

    const value = legs.reduce((sum, leg) => sum + leg.value, 0)
    const quantity = legs.reduce((sum, leg) => sum + leg.quantity, 0)
    // A position's basis is only meaningful when every account reported one —
    // summing the known half would overstate the gain by the unknown half.
    const basisKnown = legs.every((leg) => leg.costBasis !== null)
    const costBasis = basisKnown ? legs.reduce((sum, leg) => sum + (leg.costBasis ?? 0), 0) : null
    const gain = costBasis === null ? null : value - costBasis
    // The heaviest leg names the position: the same ticker classified two ways
    // in two accounts resolves to whichever holds more of it.
    const dominant = ranked[0]

    positions.push({
      ticker,
      name: dominant.name,
      logoUrl: dominant.logo_url,
      assetType: dominant.type,
      quantity,
      value,
      costBasis,
      averageCost: costBasis !== null && quantity > 0 ? costBasis / quantity : null,
      gain,
      gainPct: costBasis !== null && costBasis > 0 && gain !== null ? gain / costBasis : null,
      weight: null,
      // A holding nobody has priced yet is worth an unknown amount, not zero,
      // so it is never Dust — hiding it would be a guess dressed as a fact.
      isDust: ranked.some(hasValue) && value < DUST_THRESHOLD,
      isCashEquivalent: dominant.type === CASH_EQUIVALENT_TYPE,
      legs,
    })
  }

  const included = positions.filter((p) => !p.isDust && !p.isCashEquivalent)
  const investedTotal = included.reduce((sum, p) => sum + p.value, 0)
  for (const position of included) {
    position.weight = investedTotal > 0 ? position.value / investedTotal : 0
  }

  const byAssetClass = new Map<string, number>()
  const byAccountType = new Map<string, number>()
  for (const position of included) {
    byAssetClass.set(position.assetType, (byAssetClass.get(position.assetType) ?? 0) + position.value)
    for (const leg of position.legs) {
      const key = leg.accountType ?? UNKNOWN_ACCOUNT_TYPE
      byAccountType.set(key, (byAccountType.get(key) ?? 0) + leg.value)
    }
  }

  return {
    positions: sortByValueDesc(positions),
    total: positions.reduce((sum, p) => sum + p.value, 0),
    investedTotal,
    cashEquivalentTotal: positions
      .filter((p) => p.isCashEquivalent && !p.isDust)
      .reduce((sum, p) => sum + p.value, 0),
    dustTotal: positions.filter((p) => p.isDust).reduce((sum, p) => sum + p.value, 0),
    byAssetClass: allocate(byAssetClass, investedTotal),
    byAccountType: allocate(byAccountType, investedTotal),
  }
}
