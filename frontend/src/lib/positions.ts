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

// Allocation keys off the wallet id, so a wallet-less holding needs a key of
// its own. Wallet ids are UUIDs, so this cannot collide with a real one.
export const NO_WALLET_KEY = 'none'

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

/** Which allocation a slice came from, and so what narrowing to it means. */
export type AllocationDim = 'class' | 'accountType' | 'wallet'

export interface AllocationFilter {
  dim: AllocationDim
  key: string
}

/**
 * Settled uninvested cash in one Wallet. Kept per wallet, not just summed, so
 * a view narrowed to one slice can carry the cash of the wallets that slice
 * covers and no others.
 */
export interface WalletCash {
  walletId: string
  accountType: string | null
  amount: number
}

export interface Portfolio {
  /** Ranked by value, every position — read `isDust` to drop them from a ranking. */
  positions: Position[]
  /** Everything: positions, dust, cash equivalents and liquid cash. */
  total: number
  /** The weight denominator: excludes dust, cash equivalents and liquid cash. */
  investedTotal: number
  cashEquivalentTotal: number
  /** Settled uninvested cash, per wallet whose account reported a balance. */
  liquidCash: WalletCash[]
  liquidCashTotal: number
  dustTotal: number
  byAssetClass: AllocationSlice[]
  byAccountType: AllocationSlice[]
  /** Keyed by wallet id, or `NO_WALLET_KEY` for holdings in no wallet. */
  byWallet: AllocationSlice[]
}

/**
 * Cash carries no `weight` — allocation excludes it by design — so its
 * percentage has to come off the totals instead of the weight column.
 */
export function shareOfTotal(value: number, total: number): number {
  return total > 0 ? value / total : 0
}

function primaryValue(asset: Asset): number {
  return Number(asset.current_value_primary ?? asset.current_value ?? 0)
}

function hasValue(asset: Asset): boolean {
  return (asset.current_value_primary ?? asset.current_value) !== null
}

// Quantities are stored at six decimals, so summing legs in binary floating
// point can surface digits the holding never had (24183.95 + 25252.29 reading
// as 49436.240000000005). Round back to the precision the column actually has.
function roundQuantity(quantity: number): number {
  return Math.round(quantity * 1e6) / 1e6
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

// Balances and holding values are both stored at two decimals, so their
// difference is too. Rounding back stops a residue like 4e-11 surfacing as a
// cash row on an account whose holdings exactly match its balance.
function roundCents(amount: number): number {
  return Math.round(amount * 100) / 100
}

/**
 * Settled uninvested cash per wallet: the balance the provider reports for the
 * account, less everything the wallet holds.
 *
 * The floor at zero is load-bearing. A provider can report a zero balance
 * against real holdings — Coinbase prices its own positions and leaves the
 * account balance at 0 — and a negative figure would subtract those holdings
 * from the portfolio a second time.
 *
 * Cash is what is *left* of the balance, so the subtraction has to be complete
 * or the remainder is not cash. Two things make it incomplete, and both mean
 * the wallet derives nothing rather than a wrong figure:
 *
 *   - the account reported no balance, so there is nothing to subtract from;
 *   - a holding is unpriced, so its share of the balance is an unknown amount
 *     rather than zero, and the remainder would be that holding misread as cash.
 *
 * Every asset in the wallet counts against the balance, not only the ticker'd
 * ones a Position is built from — the provider's balance covers dust, cash
 * equivalents and anything else parked in the account.
 */
function liquidCashPerWallet(assets: Asset[], wallets: AssetGroup[]): Map<string, number> {
  const heldByWallet = new Map<string, number>()
  const unpriced = new Set<string>()
  for (const asset of assets) {
    if (asset.is_archived || asset.sell_date || !asset.group_id) continue
    if (hasValue(asset)) {
      heldByWallet.set(asset.group_id, (heldByWallet.get(asset.group_id) ?? 0) + primaryValue(asset))
    } else {
      unpriced.add(asset.group_id)
    }
  }

  const cash = new Map<string, number>()
  for (const wallet of wallets) {
    const balance = wallet.account_balance
    if (balance === null || balance === undefined) continue
    if (unpriced.has(wallet.id)) continue
    cash.set(wallet.id, Math.max(0, roundCents(balance - (heldByWallet.get(wallet.id) ?? 0))))
  }
  return cash
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
 * The figures a position derives from its legs. Shared so that consolidating a
 * ticker and narrowing it to one account compute them the same way.
 */
function aggregateLegs(legs: PositionLeg[]) {
  const value = legs.reduce((sum, leg) => sum + leg.value, 0)
  const quantity = roundQuantity(legs.reduce((sum, leg) => sum + leg.quantity, 0))
  // A position's basis is only meaningful when every account reported one —
  // summing the known half would overstate the gain by the unknown half.
  const costBasis = legs.every((leg) => leg.costBasis !== null)
    ? legs.reduce((sum, leg) => sum + (leg.costBasis ?? 0), 0)
    : null
  const gain = costBasis === null ? null : value - costBasis
  return {
    quantity,
    value,
    costBasis,
    averageCost: costBasis !== null && quantity > 0 ? costBasis / quantity : null,
    gain,
    gainPct: costBasis !== null && costBasis > 0 && gain !== null ? gain / costBasis : null,
  }
}

type PortfolioTotals = Pick<
  Portfolio,
  'total' | 'investedTotal' | 'cashEquivalentTotal' | 'liquidCashTotal' | 'dustTotal'
>

/**
 * The totals a set of positions and its cash add up to, writing each
 * position's `weight` as a side effect — the weight denominator is one of the
 * totals, so it cannot be computed before them.
 */
function summarise(positions: Position[], liquidCash: WalletCash[]): PortfolioTotals {
  const investedTotal = positions
    .filter((p) => !p.isDust && !p.isCashEquivalent)
    .reduce((sum, p) => sum + p.value, 0)
  for (const position of positions) {
    position.weight =
      position.isDust || position.isCashEquivalent
        ? null
        : investedTotal > 0
          ? position.value / investedTotal
          : 0
  }
  const liquidCashTotal = liquidCash.reduce((sum, cash) => sum + cash.amount, 0)
  return {
    // Liquid Cash is part of the account's total but part of no Holding
    // (CONTEXT.md), so the grand total has to carry it or the rows above it
    // will not add up to it.
    total: positions.reduce((sum, p) => sum + p.value, 0) + liquidCashTotal,
    investedTotal,
    cashEquivalentTotal: positions
      .filter((p) => p.isCashEquivalent && !p.isDust)
      .reduce((sum, p) => sum + p.value, 0),
    liquidCashTotal,
    dustTotal: positions.filter((p) => p.isDust).reduce((sum, p) => sum + p.value, 0),
  }
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
  const occupiedWallets = new Set<string>()

  for (const asset of assets) {
    if (asset.is_archived || asset.sell_date) continue
    if (asset.group_id) occupiedWallets.add(asset.group_id)
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

    const aggregate = aggregateLegs(legs)
    // The heaviest leg names the position: the same ticker classified two ways
    // in two accounts resolves to whichever holds more of it.
    const dominant = ranked[0]

    positions.push({
      ticker,
      name: dominant.name,
      logoUrl: dominant.logo_url,
      assetType: dominant.type,
      ...aggregate,
      weight: null,
      // A holding nobody has priced yet is worth an unknown amount, not zero,
      // so it is never Dust — hiding it would be a guess dressed as a fact.
      isDust: ranked.some(hasValue) && aggregate.value < DUST_THRESHOLD,
      isCashEquivalent: dominant.type === CASH_EQUIVALENT_TYPE,
      legs,
    })
  }

  const liquidCash: WalletCash[] = [...liquidCashPerWallet(assets, wallets)].map(
    ([walletId, amount]) => ({
      walletId,
      accountType: walletsById.get(walletId)?.account_type ?? null,
      amount,
    }),
  )
  for (const cash of liquidCash) {
    if (cash.amount > 0) occupiedWallets.add(cash.walletId)
  }

  const totals = summarise(positions, liquidCash)

  const byAssetClass = new Map<string, number>()
  const byAccountType = new Map<string, number>()
  const byWallet = new Map<string, number>()
  for (const position of positions) {
    if (position.isDust || position.isCashEquivalent) continue
    byAssetClass.set(position.assetType, (byAssetClass.get(position.assetType) ?? 0) + position.value)
    for (const leg of position.legs) {
      const typeKey = leg.accountType ?? UNKNOWN_ACCOUNT_TYPE
      byAccountType.set(typeKey, (byAccountType.get(typeKey) ?? 0) + leg.value)
      const walletKey = leg.walletId ?? NO_WALLET_KEY
      byWallet.set(walletKey, (byWallet.get(walletKey) ?? 0) + leg.value)
    }
  }
  // A wallet holding nothing but cash has no invested value and so draws no
  // wedge — and a wedge is what a slice is clicked on. Listing it at zero is
  // what keeps every account reachable as a filter.
  for (const wallet of wallets) {
    if (!occupiedWallets.has(wallet.id)) continue
    if (!byWallet.has(wallet.id)) byWallet.set(wallet.id, 0)
    const typeKey = wallet.account_type ?? UNKNOWN_ACCOUNT_TYPE
    if (!byAccountType.has(typeKey)) byAccountType.set(typeKey, 0)
  }

  return {
    positions: sortByValueDesc(positions),
    ...totals,
    liquidCash,
    byAssetClass: allocate(byAssetClass, totals.investedTotal),
    byAccountType: allocate(byAccountType, totals.investedTotal),
    byWallet: allocate(byWallet, totals.investedTotal),
  }
}

/**
 * The same Portfolio narrowed to one allocation slice, so the table can drill
 * into a wedge while its subtotals still add up.
 *
 * Two things make the narrowed `investedTotal` equal the value of the wedge
 * clicked, and both are why this reads the built Portfolio rather than
 * rebuilding from a filtered asset list:
 *
 *   - An asset class is a property of the *consolidated* position — the same
 *     key the wedge was bucketed under — so a class slice keeps whole
 *     positions. A wallet and an account type are properties of a leg, so
 *     those slices keep matching legs and re-derive the position from them.
 *   - The Dust and Cash Equivalent verdicts are carried over, not taken again.
 *     Both describe the whole Holding, and re-testing a position against one
 *     account's share of it would drop rows the wedge had counted.
 *
 * The allocations are the unfiltered ones: the donuts keep showing the whole
 * portfolio, or picking a second slice would mean clicking a wedge that is no
 * longer drawn.
 */
export function filterPortfolio(portfolio: Portfolio, filter: AllocationFilter): Portfolio {
  const matchesWallet = (walletId: string | null, accountType: string | null) =>
    filter.dim === 'wallet'
      ? (walletId ?? NO_WALLET_KEY) === filter.key
      : (accountType ?? UNKNOWN_ACCOUNT_TYPE) === filter.key

  const positions =
    filter.dim === 'class'
      ? portfolio.positions.filter((p) => p.assetType === filter.key).map((p) => ({ ...p }))
      : portfolio.positions.flatMap((position) => {
          const legs = position.legs.filter((leg) => matchesWallet(leg.walletId, leg.accountType))
          return legs.length === 0 ? [] : [{ ...position, ...aggregateLegs(legs), legs }]
        })

  // Liquid Cash belongs to a Wallet, not to an asset class, so a class slice
  // has none to report — carrying it over would add the whole portfolio's cash
  // to a subtotal covering one class of the holdings.
  const liquidCash =
    filter.dim === 'class'
      ? []
      : portfolio.liquidCash.filter((cash) => matchesWallet(cash.walletId, cash.accountType))

  return { ...portfolio, positions, ...summarise(positions, liquidCash), liquidCash }
}
