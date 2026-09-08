import { useMemo, useState, type ReactNode } from 'react'
import { useTranslation } from 'react-i18next'
import { Link, useSearchParams } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { PieChart, Pie, Cell, Tooltip, ResponsiveContainer } from 'recharts'
import { ChevronDown, ChevronUp, X } from 'lucide-react'
import { BalanceDetails } from '@/components/balance-details'
import { Badge } from '@/components/ui/badge'
import { assets as assetsApi } from '@/lib/api'
import { assetTypeI18nKey, getTypeConfig } from '@/lib/asset-types'
import { formatCurrency, formatExactDecimal } from '@/lib/format'
import {
  buildPortfolio,
  filterPortfolio,
  shareOfTotal,
  CASH_EQUIVALENT_TYPE,
  type AllocationDim,
  type AllocationFilter,
  type AllocationSlice,
  type Position,
} from '@/lib/positions'
import type { Asset, AssetGroup, AssetIncome } from '@/types'

interface PositionsTabProps {
  workspaceId?: string
  holdingsError?: boolean
  canOpenAccounts?: boolean
  holdings: Asset[]
  wallets: AssetGroup[]
  currency: string
  locale: string
  dateLocale: string
  mask: (value: string) => string
  canWrite: boolean
  /** Reclassify one Holding — the user's verdict on what counts as cash. */
  onClassify: (assetId: string, type: string) => void
  onOpenHolding: (assetId: string) => void
  children?: ReactNode
  walletContent?: ReactNode
}

const ACCOUNT_TYPE_KEYS: Record<string, string> = {
  checking: 'accounts.typeChecking',
  savings: 'accounts.typeSavings',
  credit_card: 'accounts.typeCreditCard',
  investment: 'accounts.typeInvestment',
  cash: 'accounts.typeCash',
}

const SLICE_COLORS = [
  '#6366F1',
  '#F59E0B',
  '#10B981',
  '#EC4899',
  '#0EA5E9',
  '#8B5CF6',
  '#F97316',
  '#14B8A6',
  '#84CC16',
  '#D946EF',
  '#F43F5E',
  '#06B6D4',
]

const POSITIONS_GRID = 'minmax(0,2.2fr) 0.8fr 1fr 1.1fr 1.2fr 1.3fr 1.2fr 0.7fr 2rem'
const LEGS_GRID = 'minmax(0,2.2fr) 1fr 0.8fr 1.1fr 1.2fr 1.3fr 1.2fr'

const TOOLTIP_STYLE: React.CSSProperties = {
  background: 'var(--card)',
  color: 'var(--foreground)',
  border: '1px solid var(--border)',
  borderRadius: '0.75rem',
  boxShadow: '0 4px 12px rgba(0,0,0,0.08)',
  fontSize: '12px',
  padding: '8px 12px',
}

const DASH = '—'

interface Donut {
  dim: AllocationDim
  title: string
  data: DonutDatum[]
}

interface DonutDatum {
  key: string
  label: string
  value: number
  weight: number
  color: string
}

function formatPercent(weight: number): string {
  return `${(weight * 100).toFixed(1)}%`
}

function gainClass(gain: number): string {
  return gain >= 0 ? 'text-emerald-600' : 'text-rose-500'
}

function PositionIcon({ logoUrl, type }: { logoUrl: string | null; type: string }) {
  const [errored, setErrored] = useState(false)
  const config = getTypeConfig(type)
  const Icon = config.icon
  const showImage = !!logoUrl && !errored
  return (
    <div
      className={`w-8 h-8 rounded-lg flex items-center justify-center overflow-hidden shrink-0 ${
        showImage ? 'bg-white border border-border' : config.bg
      }`}
    >
      {showImage ? (
        <img src={logoUrl!} alt="" className="w-full h-full object-contain" onError={() => setErrored(true)} />
      ) : (
        <Icon size={16} className={config.color} />
      )}
    </div>
  )
}

const LOTS_GRID = 'minmax(0,1.4fr) 0.9fr 1fr 1.1fr 1.6fr'

/**
 * Warns before a candidate sale forfeits a deduction. Silent unless the sale
 * would be at a loss and the same instrument was bought inside the window —
 * a warning that fires on a non-risk teaches the user to ignore it.
 */
function WashSaleWarning({ assetId, dateLocale }: { assetId: string; dateLocale: string }) {
  const { t } = useTranslation()
  const { data } = useQuery({
    queryKey: ['asset-wash-sale', assetId],
    queryFn: () => assetsApi.washSale(assetId),
  })

  if (!data?.warning) return null

  const day = (iso: string) => new Date(`${iso}T00:00:00`).toLocaleDateString(dateLocale)

  return (
    <div className="px-3 py-2 bg-warning/10 border-t border-warning/30">
      <p className="text-[11px] font-semibold text-warning-foreground">
        {t('assets.washSaleTitle')}
      </p>
      <p className="text-[11px] text-warning-foreground">
        {t('assets.washSaleBody', { start: day(data.window_start), end: day(data.window_end) })}
      </p>
      <div className="flex flex-wrap gap-1 pt-1">
        {data.wallets.map((wallet) => (
          <Badge
            key={wallet.wallet_id}
            variant="outline"
            className={`text-[9px] px-1 py-0 ${
              wallet.unrecoverable ? 'text-rose-600 border-rose-300' : 'text-warning-foreground border-warning/40'
            }`}
          >
            {wallet.wallet ?? t('assets.noWallet')}
            {wallet.unrecoverable ? ` · ${t('assets.washSaleUnrecoverable')}` : ''}
          </Badge>
        ))}
      </div>
    </div>
  )
}

/**
 * The Tax Lots of one Holding, fetched on demand — they are derived by
 * replaying its ledger, so they are not part of the holdings list.
 */
function TaxLotsPanel({
  assetId,
  currency,
  locale,
  dateLocale,
  mask,
}: {
  assetId: string
  currency: string
  locale: string
  dateLocale: string
  mask: (value: string) => string
}) {
  const { t } = useTranslation()
  const { data, isError, refetch } = useQuery({
    queryKey: ['asset-tax-lots', assetId],
    queryFn: () => assetsApi.taxLots(assetId),
  })

  const money = (value: number | string | null) => value == null ? t('evidence.unknown', 'Unknown') : mask(typeof value === 'string' ? `${formatExactDecimal(value)} ${currency}` : formatCurrency(value, currency, locale))
  const day = (iso: string) => new Date(`${iso}T00:00:00`).toLocaleDateString(dateLocale)
  const hint = (text: string) => (
    <div className="px-3 py-2 bg-background/60 border-t border-border">
      <p className="text-[11px] text-muted-foreground italic">{text}</p>
    </div>
  )

  if (!data) return <div>{hint(isError ? t('common.error') : t('common.loading'))}{isError && <button className="min-h-11 px-3 text-sm underline underline-offset-4" onClick={() => { void refetch() }}>{t('common.retry', 'Retry')}</button>}</div>
  // Checked before tax character, which is also false here: no wallet means no
  // treatment to read, so the answer is missing rather than "not reportable".
  if (data.no_wallet) return hint(t('assets.lotsNoWallet'))
  // A gain in a Tax-Advantaged wallet is never Reportable, so it has no
  // long-versus-short answer to give.
  if (!data.tax_character) return hint(t('assets.lotsNoTaxCharacter'))
  if (data.snapshot) return hint(t('assets.lotsSnapshot'))
  if (data.lots.length === 0 && data.sales.length === 0) return hint(t('assets.lotsNone'))

  const character = (long: boolean | null) => long == null ? t('ownedTransfers.periodUnknown', 'Holding period unknown') : long ? t('assets.lotsLong') : t('assets.lotsShort')

  return (
    <div className="px-3 py-2 bg-background/60 border-t border-border">
      <p className="text-[10px] font-semibold text-muted-foreground uppercase tracking-wider">
        {t('assets.lotsTitle')}
      </p>
      {data.basis_complete === false && <p role="status" className="mt-2 text-sm text-muted-foreground">{t('ownedTransfers.partialLots', 'Acquisition basis is incomplete. Known cost subtotal')}: {money(data.known_acquisition_cost ?? null)} · {t('ownedTransfers.unknownUnits', 'Units with unknown basis')}: {data.unknown_basis_quantity == null ? t('evidence.unknown', 'Unknown') : mask(data.unknown_basis_quantity)}</p>}
      {data.settlement_complete === false && <p role="status" className="mt-2 text-sm text-warning-foreground">{t('ownedTransfers.principalUnknown', 'Movement settlement unresolved')}</p>}
      {!!data.missing_links?.length && <ul className="mt-2 list-inside list-disc text-sm text-warning-foreground">{data.missing_links.map((reason) => <li key={reason}>{t(`ownedTransfers.reasons.${reason}`, reason.replaceAll('_', ' '))}</li>)}</ul>}
      {data.lots.length > 0 && (
        <>
          <div
            className="grid items-center gap-2 py-1.5 text-[10px] font-semibold text-muted-foreground uppercase tracking-wider"
            style={{ gridTemplateColumns: LOTS_GRID }}
          >
            <div>{t('assets.lotsColAcquired')}</div>
            <div className="text-right">{t('assets.posColQuantity')}</div>
            <div className="text-right">{t('assets.lotsColUnitPrice')}</div>
            {/* Not "Cost Basis": a lot is carried at what it actually cost,
                while the position's Cost Basis is the blended average, so the
                two legitimately differ after a partial sale (ADR 0003). */}
            <div className="text-right">{t('assets.lotsColCost')}</div>
            <div className="text-right">{t('assets.lotsColHoldingPeriod')}</div>
          </div>
          {data.lots.map((lot, i) => (
            <div
              key={lot.lot_id ?? `${lot.acquired}-${i}`}
              className="grid items-center gap-2 py-1.5 text-xs border-t border-border/50"
              style={{ gridTemplateColumns: LOTS_GRID }}
            >
              <div className="min-w-0 text-foreground">{lot.acquired == null ? t('ownedTransfers.unknownDate', 'Acquisition date unknown') : day(lot.acquired)}{lot.lineage?.map((id) => <Link key={id} className="mt-1 block break-all py-1 text-xs underline underline-offset-4" to={`/assets?${new URLSearchParams({ tab: 'activity', activity: 'transfers', transfer: id })}`}>{t('ownedTransfers.openHop', 'Open transfer')}</Link>)}{!!lot.missing_links?.length && <span className="mt-1 block text-xs text-warning-foreground">{lot.missing_links.map((reason) => t(`ownedTransfers.reasons.${reason}`, reason.replaceAll('_', ' '))).join(' · ')}</span>}</div>
              <div className="text-right tabular-nums text-muted-foreground">{mask(`${lot.quantity}`)}</div>
              <div className="text-right tabular-nums text-muted-foreground">{money(lot.unit_price)}</div>
              <div className="text-right tabular-nums text-muted-foreground">{money(lot.cost)}</div>
              <div className="text-right">
                <Badge
                  variant="outline"
                  className={`text-[9px] px-1 py-0 ${lot.long_term == null ? 'text-muted-foreground' : lot.long_term ? 'text-emerald-600' : 'text-warning-foreground'}`}
                >
                  {character(lot.long_term)}
                </Badge>
                <span className="block text-[10px] text-muted-foreground tabular-nums">
                  {lot.holding_days == null || lot.days_until_long_term == null ? null : lot.long_term
                    ? t('assets.lotsHeldDays', { count: lot.holding_days })
                    : t('assets.lotsLongIn', { count: lot.days_until_long_term })}
                </span>
              </div>
            </div>
          ))}
          <div className="flex flex-wrap gap-x-4 gap-y-1 pt-2 text-[11px] text-muted-foreground">
            {data.basis_complete === false && <span>{t('ownedTransfers.knownPeriodSubtotals', 'Known holding-period subtotals')}</span>}
            <span>
              {t('assets.lotsLong')}: <span className="tabular-nums">{mask(`${data.long_quantity}`)}</span> ·{' '}
              {money(data.long_cost)}
            </span>
            <span>
              {t('assets.lotsShort')}: <span className="tabular-nums">{mask(`${data.short_quantity}`)}</span> ·{' '}
              {money(data.short_cost)}
            </span>
          </div>
        </>
      )}
      {data.sales.length > 0 && (
        <div className="mt-2 pt-2 border-t border-border/50 flex flex-wrap gap-x-4 gap-y-1 text-[11px]">
          <span className="text-muted-foreground">{t('assets.lotsRealised')}:</span>
          <span className={data.realised_long == null ? 'text-muted-foreground' : String(data.realised_long).startsWith('-') ? 'text-rose-600' : 'text-emerald-600'}>
            {t('assets.lotsLong')} {money(data.realised_long)}
          </span>
          <span className={data.realised_short == null ? 'text-muted-foreground' : String(data.realised_short).startsWith('-') ? 'text-rose-600' : 'text-emerald-600'}>
            {t('assets.lotsShort')} {money(data.realised_short)}
          </span>
          {data.unknown_disposition_quantity != null && <span>{t('ownedTransfers.unknownDisposed', 'Disposition quantity with unknown gain')}: {mask(data.unknown_disposition_quantity)}</span>}
        </div>
      )}
    </div>
  )
}

export default function PositionsTab({
  holdings,
  wallets,
  workspaceId,
  holdingsError = false,
  canOpenAccounts = true,
  currency,
  locale,
  dateLocale,
  mask,
  canWrite,
  onClassify,
  onOpenHolding,
  children,
  walletContent,
}: PositionsTabProps) {
  const { t } = useTranslation()
  const [expandedTicker, setExpandedTicker] = useState<string | null>(null)
  const [searchParams, setSearchParams] = useSearchParams()
  const allocation = searchParams.get('allocation')
  const allocationKey = searchParams.get('allocationKey')
  const filter = useMemo<AllocationFilter | null>(() => allocationKey && (allocation === 'class' || allocation === 'wallet' || allocation === 'accountType')
    ? { dim: allocation, key: allocationKey } : null, [allocation, allocationKey])
  const setFilter = (nextFilter: AllocationFilter | null) => {
    const next = new URLSearchParams(searchParams)
    next.set('tab', 'portfolio')
    next.set('view', 'assets')
    if (nextFilter) {
      next.set('allocation', nextFilter.dim)
      next.set('allocationKey', nextFilter.key)
    } else {
      next.delete('allocation')
      next.delete('allocationKey')
    }
    setSearchParams(next, { replace: true })
  }

  const walletsById = useMemo(() => new Map(wallets.map((w) => [w.id, w])), [wallets])
  const portfolio = useMemo(() => buildPortfolio(holdings, wallets.map((wallet) => holdingsError || (workspaceId && wallet.balance_explanation?.workspace_id !== workspaceId) ? { ...wallet, balance_explanation: null } : wallet), currency), [holdings, wallets, currency, holdingsError, workspaceId])

  // One call for the whole workspace: a per-asset route would be one request
  // per row. A holding that received nothing is absent, not zero.
  const { data: income } = useQuery({
    queryKey: ['asset-income'],
    queryFn: () => assetsApi.income(),
  })

  /** A position's income is its legs': the same ticker pays in every account.
   *  Only what a description attributed on evidence reaches here — a reward
   *  paid into a holding that did not earn it is the wallet's, below. */
  const incomeOf = (assetIds: string[]) => {
    const rows = assetIds.map((id) => income?.holdings[id]).filter((r): r is AssetIncome => !!r)
    if (rows.length === 0) return null
    const runRates = rows.map((r) => r.run_rate)
    return {
      total: rows.reduce((sum, r) => sum + r.total, 0),
      // The legs of one ticker pay on the same schedule, so the heaviest one
      // names it; a disagreement means one leg is too young to have a pattern.
      cadence: [...rows].sort((a, b) => b.total - a.total)[0].cadence,
      // Null unless every leg projects, or the sum would be a partial year
      // read as a whole one.
      runRate: runRates.every((r) => r !== null)
        ? runRates.reduce((sum, r) => sum + (r ?? 0), 0)
        : null,
    }
  }

  // Rebuilding the figures from the slice rather than hiding rows is what
  // keeps the subtotals under the table adding up to the wedge that was
  // clicked.
  const view = useMemo(
    () => (filter && !walletContent ? filterPortfolio(portfolio, filter) : portfolio),
    [filter, portfolio, walletContent],
  )
  const selectedHoldingIds = view.positions.flatMap((position) => position.legs.map((leg) => leg.assetId))
  const detailHoldings = filter && !walletContent
    ? holdings.filter((holding) => view.wallets.some((wallet) => wallet.id === holding.group_id) || selectedHoldingIds.includes(holding.id))
    : holdings
  // Match buildPortfolio's scope: a separately listed manual asset cannot
  // make a fully priced ticker allocation incomplete.
  const unpricedIds = new Set(holdings.filter((holding) =>
    holding.ticker?.trim() && !holding.sell_date && !holding.is_archived &&
    holding.current_value_primary == null && holding.current_value == null,
  ).map((holding) => holding.id))
  const incompletePositions = view.positions.filter((position) => position.legs.some((leg) => unpricedIds.has(leg.assetId)))
  const hasUnpricedPositions = incompletePositions.length > 0
  const hasUnknownCash = view.unknownCashWalletIds.length > 0
  const hasIncompleteBalance = hasUnpricedPositions || hasUnknownCash || holdingsError
  const hasKnownValue = view.positions.some((position) => position.legs.some((leg) => !unpricedIds.has(leg.assetId))) || view.liquidCash.length > 0
  const summaryValue = hasIncompleteBalance && !hasKnownValue ? null : view.total

  // Income the wallets in view received, however it was paid. Kept apart from
  // the per-holding column because it is the answer to a different question:
  // not what a position yields, but whether the account is still being paid.
  const walletIncome = useMemo(() => {
    const rows = view.wallets.map((w) => income?.wallets[w.id]).filter((r): r is AssetIncome => !!r)
    if (rows.length === 0) return null
    const runRates = rows.map((r) => r.run_rate)
    return {
      total: rows.reduce((sum, r) => sum + r.total, 0),
      cadence: [...rows].sort((a, b) => b.total - a.total)[0].cadence,
      runRate: runRates.every((r) => r !== null)
        ? runRates.reduce((sum, r) => sum + (r ?? 0), 0)
        : null,
    }
  }, [view, income])

  const accountTypeLabel = (type: string | null) =>
    type && ACCOUNT_TYPE_KEYS[type] ? t(ACCOUNT_TYPE_KEYS[type]) : t('assets.posAccountUnknown')

  // One label function per dimension, so the donut, its legend and the active
  // filter chip all read the same name for a slice.
  const labelFor: Record<AllocationDim, (key: string) => string> = {
    class: (key) => t(assetTypeI18nKey(key)),
    accountType: accountTypeLabel,
    wallet: (key) => walletsById.get(key)?.name ?? t('assets.noWallet'),
  }

  const money = (value: number | null) =>
    value === null ? DASH : mask(formatCurrency(value, currency, locale))

  const toDonutData = (slices: AllocationSlice[], label: (key: string) => string): DonutDatum[] =>
    slices.filter((slice) => slice.value > 0).map((slice, i) => ({
      key: slice.key,
      label: label(slice.key),
      value: slice.value,
      weight: slice.weight,
      color: SLICE_COLORS[i % SLICE_COLORS.length],
    }))

  const donuts: Donut[] = [
    {
      dim: 'class',
      title: t('assets.posAllocationByClass'),
      data: toDonutData(portfolio.byAssetClass, labelFor.class),
    },
    {
      dim: 'wallet',
      title: t('assets.posAllocationByAccount'),
      data: toDonutData(portfolio.byWallet, labelFor.wallet),
    },
  ]

  const activeFilterLabel = filter && !walletContent ? labelFor[filter.dim](filter.key) : null

  const toggleFilter = (dim: AllocationDim, key: string) => {
    setExpandedTicker(null)
    setFilter(!walletContent && filter?.dim === dim && filter.key === key ? null : { dim, key })
  }

  // The ranking answers "where is my concentration risk", so what allocation
  // leaves out stays out here too — a 49k money-market row heading a table
  // ranked by a weight it has none of reads as the biggest position there is.
  const rankedPositions = view.positions.filter((p) => !p.isCashEquivalent && (!p.isDust || incompletePositions.includes(p)))
  // Still listed, just under their own heading — this is the only place the
  // user can see what was classified as cash and put it back.
  const cashEquivalents = view.positions.filter((p) => p.isCashEquivalent && (!p.isDust || incompletePositions.includes(p)))

  function renderDonut({ dim, title, data }: Donut) {
    const selectedKey = !walletContent && filter?.dim === dim ? filter.key : null
    const dimmed = (key: string) => selectedKey !== null && selectedKey !== key
    return (
      <section className="h-full min-w-0 rounded-xl border border-border bg-card p-4 sm:p-5" aria-label={title}>
        <h3 className="text-sm font-semibold text-foreground mb-4">{title}</h3>
        {data.length === 0 ? (
          <p className="text-xs text-muted-foreground italic py-8 text-center">{t('assets.posNoPositions')}</p>
        ) : (
          <div className="grid grid-cols-[7rem_minmax(0,1fr)] items-center gap-4 sm:grid-cols-[10rem_minmax(0,1fr)] xl:grid-cols-[11rem_minmax(0,1fr)]">
            <div className="relative h-28 w-full sm:h-40 xl:h-44">
              <ResponsiveContainer width="100%" height="100%">
                <PieChart>
                  <Pie
                    data={data}
                    cx="50%"
                    cy="50%"
                    innerRadius="62%"
                    outerRadius="90%"
                    paddingAngle={3}
                    dataKey="value"
                    stroke="var(--card)"
                    strokeWidth={0}
                  >
                    {data.map((entry, idx) => (
                      <Cell
                        key={idx}
                        fill={entry.color}
                        fillOpacity={dimmed(entry.key) ? 0.25 : 1}
                        className="cursor-pointer focus:outline-none"
                        onClick={() => toggleFilter(dim, entry.key)}
                      />
                    ))}
                  </Pie>
                  <Tooltip
                    offset={20}
                    wrapperStyle={{ zIndex: 10 }}
                    content={({ active, payload }) => {
                      if (!active || !payload?.length) return null
                      const datum = payload[0].payload as DonutDatum
                      return (
                        <div style={TOOLTIP_STYLE}>
                          <p className="text-xs font-semibold mb-1">{datum.label}</p>
                          <p className="text-xs">
                            {money(datum.value)} ({formatPercent(datum.weight)})
                          </p>
                        </div>
                      )
                    }}
                  />
                </PieChart>
              </ResponsiveContainer>
              <div className="absolute inset-0 hidden sm:flex flex-col items-center justify-center pointer-events-none">
                <span className="text-[10px] text-muted-foreground">{t('assets.posInvestedTotal')}</span>
                <span className="text-sm font-semibold text-foreground tabular-nums">
                  {money(portfolio.investedTotal)}
                </span>
              </div>
            </div>
            <div className="max-h-56 min-w-0 overflow-y-auto overscroll-contain">
              {data.map((d, i) => (
                <button
                  key={`${i}-${d.key}`}
                  type="button"
                  onClick={() => toggleFilter(dim, d.key)}
                  aria-pressed={selectedKey === d.key}
                  className={`flex min-h-11 w-full items-center gap-2 rounded-md px-1.5 py-1.5 text-left hover:bg-muted/40 focus-visible:outline-2 focus-visible:outline-ring transition-colors ${
                    dimmed(d.key) ? 'opacity-40' : ''
                  }`}
                >
                  <div className="w-2 h-2 rounded-full shrink-0" style={{ backgroundColor: d.color }} />
                  <span
                    className={`min-w-0 flex-1 break-words text-xs ${
                      selectedKey === d.key ? 'font-semibold text-foreground' : 'text-muted-foreground'
                    }`}
                  >
                    {d.label}
                  </span>
                  <span className="shrink-0 text-right text-xs tabular-nums"><span className="block font-medium text-foreground">{money(d.value)}</span><span className="block text-muted-foreground">{formatPercent(d.weight)}</span></span>
                </button>
              ))}
            </div>
          </div>
        )}
      </section>
    )
  }

  /** Income over the trailing year, and what the recent payouts annualise to
   *  against the value they were earned on — the figure that says whether a
   *  cash position is still being paid, which the twelve-month total does not. */
  function renderIncomeCell(assetIds: string[], value: number) {
    const summary = incomeOf(assetIds)
    if (!summary || summary.total === 0) return <span className="text-muted-foreground">{DASH}</span>
    const yieldNow = summary.runRate !== null && value > 0 ? summary.runRate / value : null
    return (
      <>
        <span className="text-foreground">{money(summary.total)}</span>
        <span className="block text-[10px] text-muted-foreground">
          {summary.cadence ? t(`assets.incomeCadence.${summary.cadence}`) : t('assets.incomeIrregular')}
          {yieldNow !== null && ` · ${formatPercent(yieldNow)}`}
        </span>
      </>
    )
  }

  function renderLegs(position: Position) {
    return (
      <div className="overflow-x-auto bg-muted/20 border-t border-border">
      <div className="min-w-[780px] px-3 py-2">
        <div
          className="grid items-center gap-2 py-1.5 text-[10px] font-semibold text-muted-foreground uppercase tracking-wider"
          style={{ gridTemplateColumns: LEGS_GRID }}
        >
          <div>{t('assets.posColAccount')}</div>
          <div className="text-right">{t('assets.posColQuantity')}</div>
          <div className="text-right" />
          <div className="text-right">{t('assets.posColCostBasis')}</div>
          <div className="text-right">{t('assets.posColValue')}</div>
          <div className="text-right">{t('assets.posColGain')}</div>
          <div className="text-right">{t('assets.posColIncome')}</div>
        </div>
        {position.legs.map((leg) => (
          <div key={leg.assetId}>
            <div
              className="grid items-center gap-2 py-1.5 text-xs border-t border-border/50"
              style={{ gridTemplateColumns: LEGS_GRID }}
            >
              <div className="min-w-0">
                <button type="button" onClick={() => onOpenHolding(leg.assetId)} className="font-medium text-primary text-left hover:underline truncate block max-w-full">
                  {leg.walletName ?? t('assets.noWallet')}
                  <span className="sr-only"> · {t('assets.openHolding')}</span>
                </button>
                <span className="text-[10px] text-muted-foreground">
                  {accountTypeLabel(leg.accountType)}
                </span>
              </div>
              <div className="text-right tabular-nums text-muted-foreground">{mask(`${leg.quantity}`)}</div>
              <div className="text-right text-[10px] text-muted-foreground">
                {leg.taxTreatment ? t(`assets.taxTreatment.${leg.taxTreatment}`) : DASH}
              </div>
              <div className="text-right tabular-nums text-muted-foreground">{money(unpricedIds.has(leg.assetId) ? null : leg.costBasis)}</div>
              <div className="text-right tabular-nums text-foreground">{money(unpricedIds.has(leg.assetId) ? null : leg.value)}</div>
              <div className="text-right tabular-nums">
                {leg.gain === null || unpricedIds.has(leg.assetId) ? (
                  <span className="text-muted-foreground">{DASH}</span>
                ) : (
                  <span className={gainClass(leg.gain)}>{money(leg.gain)}</span>
                )}
                {canWrite && (
                  <button
                    onClick={() =>
                      onClassify(
                        leg.assetId,
                        leg.assetType === CASH_EQUIVALENT_TYPE ? 'investment' : CASH_EQUIVALENT_TYPE,
                      )
                    }
                    className="block ml-auto text-[10px] font-medium text-primary hover:underline"
                  >
                    {leg.assetType === CASH_EQUIVALENT_TYPE
                      ? t('assets.posMarkInvestment')
                      : t('assets.posMarkCashEquivalent')}
                  </button>
                )}
              </div>
              <div className="text-right tabular-nums">
                {renderIncomeCell([leg.assetId], leg.value)}
              </div>
            </div>
            {/* Per wallet, not per ticker: tax character attaches to the wallet,
                so a split blending a taxable leg with a Roth one would be a
                figure no tax return could use. */}
            <WashSaleWarning assetId={leg.assetId} dateLocale={dateLocale} />
            <TaxLotsPanel
              assetId={leg.assetId}
              currency={holdings.find((holding) => holding.id === leg.assetId)?.currency ?? currency}
              locale={locale}
              dateLocale={dateLocale}
              mask={mask}
            />
          </div>
        ))}
      </div>
      </div>
    )
  }

  function renderPositionRow(position: Position) {
    const isExpanded = expandedTicker === position.ticker
    const unpriced = position.legs.some((leg) => unpricedIds.has(leg.assetId))
    const value = unpriced ? null : position.value
    const gain = unpriced ? null : position.gain
    const weight = hasUnpricedPositions ? null : position.weight
    const averageCost = unpriced ? null : position.averageCost
    const costBasis = unpriced ? null : position.costBasis
    return (
      <div key={position.ticker} className="border-b border-border last:border-b-0">
        <button
          type="button"
          aria-expanded={isExpanded}
          className="grid w-full grid-cols-[minmax(0,1fr)_auto_1rem] lg:grid-cols-[var(--position-columns)] text-left items-center gap-2 px-3 py-3 cursor-pointer hover:bg-muted/20 transition-colors text-sm"
          style={{ '--position-columns': POSITIONS_GRID } as React.CSSProperties}
          onClick={() => setExpandedTicker(isExpanded ? null : position.ticker)}
        >
          <div className="flex items-center gap-2.5 min-w-0">
            <PositionIcon logoUrl={position.logoUrl} type={position.assetType} />
            <div className="min-w-0">
              <div className="flex items-center gap-1.5">
                <span className="font-semibold text-foreground truncate">{position.ticker}</span>
                {position.isCashEquivalent && (
                  <Badge variant="outline" className="hidden lg:inline-flex text-[9px] px-1 py-0 text-muted-foreground shrink-0">
                    {t('assets.posExcluded')}
                  </Badge>
                )}
              </div>
              <span className="text-[11px] text-muted-foreground truncate block">{position.name}</span>
            </div>
          </div>
          <div className="hidden lg:block text-right tabular-nums text-muted-foreground">{mask(`${position.quantity}`)}</div>
          <div className="hidden lg:block text-right tabular-nums text-muted-foreground">{money(averageCost)}</div>
          <div className="hidden lg:block text-right tabular-nums text-muted-foreground">{money(costBasis)}</div>
          <div className="text-right tabular-nums font-semibold text-foreground">
            {money(value)}
            <span className={`mt-0.5 block text-xs font-normal lg:hidden ${gain === null ? 'text-muted-foreground' : gainClass(gain)}`}>
              {money(gain)}
            </span>
          </div>
          <div className="hidden lg:block text-right tabular-nums">
            {gain === null ? (
              <span className="text-muted-foreground">{DASH}</span>
            ) : (
              <span className={gainClass(gain)}>
                {money(gain)}
                {position.gainPct !== null && (
                  <span className="block text-[10px]">
                    {position.gainPct >= 0 ? '+' : ''}
                    {(position.gainPct * 100).toFixed(1)}%
                  </span>
                )}
              </span>
            )}
          </div>
          <div className="hidden lg:block text-right tabular-nums">
            {renderIncomeCell(position.legs.map((l) => l.assetId), value ?? 0)}
          </div>
          <div className="hidden lg:block text-right tabular-nums text-muted-foreground">
            {weight === null ? DASH : formatPercent(weight)}
          </div>
          <div className="flex items-center justify-end">
            {isExpanded ? (
              <ChevronUp size={15} className="text-muted-foreground" />
            ) : (
              <ChevronDown size={15} className="text-muted-foreground" />
            )}
          </div>
        </button>
        {isExpanded && (
          <>
            <dl className="grid grid-cols-2 gap-x-4 gap-y-3 border-t border-border px-3 py-3 text-xs lg:hidden">
              <div><dt className="text-muted-foreground">{t('assets.posColQuantity')}</dt><dd className="mt-1 tabular-nums">{mask(`${position.quantity}`)}</dd></div>
              <div><dt className="text-muted-foreground">{t('assets.posColAvgCost')}</dt><dd className="mt-1 tabular-nums">{money(averageCost)}</dd></div>
              <div><dt className="text-muted-foreground">{t('assets.posColCostBasis')}</dt><dd className="mt-1 tabular-nums">{money(costBasis)}</dd></div>
              <div><dt className="text-muted-foreground">{t('assets.posColWeight')}</dt><dd className="mt-1 tabular-nums">{weight === null ? DASH : formatPercent(weight)}</dd></div>
              <div className="col-span-2"><dt className="text-muted-foreground">{t('assets.posColIncome')}</dt><dd className="mt-1 tabular-nums">{renderIncomeCell(position.legs.map((leg) => leg.assetId), value ?? 0)}</dd></div>
            </dl>
            {renderLegs(position)}
          </>
        )}
      </div>
    )
  }

  function renderTotalRow(
    label: string,
    value: number | null,
    hint?: string,
    share?: number,
  ) {
    return (
      <div className="flex items-baseline justify-between gap-4 py-3 border-t border-border first:border-t-0">
        <div className="min-w-0">
          <span className="text-sm text-muted-foreground">
            {label}
          </span>
          {hint && <span className="mt-1 block text-xs text-muted-foreground">{hint}</span>}
        </div>
        <span
          className="shrink-0 text-sm tabular-nums text-foreground"
        >
          {money(value)}
          {share !== undefined && (
            <span className="ml-2 text-[10px] text-muted-foreground">{formatPercent(share)}</span>
          )}
        </span>
      </div>
    )
  }

  return (
    <div className="space-y-6">
      <section aria-label={t('assets.balanceOverview')} className="rounded-xl border border-border bg-card p-4 sm:p-5">
        <dl className="grid grid-cols-2 gap-x-6 gap-y-4 sm:grid-cols-[1.4fr_1fr_1fr]">
          <div className="col-span-2 sm:col-span-1">
            <dt className="text-sm text-muted-foreground">{t('assets.positionsAndCash')}{activeFilterLabel && ` · ${activeFilterLabel}`}</dt>
            <dd className="mt-1 text-2xl font-semibold tabular-nums">{money(summaryValue)}</dd>
            {hasIncompleteBalance && <p className="mt-1 text-xs text-muted-foreground">{t('assets.knownSubtotal', 'Known subtotal')}</p>}
          </div>
          <div>
            <dt className="text-sm text-muted-foreground">{t('assets.posInvestedTotal')}</dt>
            <dd className="mt-1 text-lg font-semibold tabular-nums">{money(incompletePositions.some((position) => !position.isCashEquivalent) && view.investedTotal === 0 ? null : view.investedTotal)}</dd>
            {incompletePositions.some((position) => !position.isCashEquivalent) && <p className="mt-1 text-xs text-muted-foreground">{t('assets.knownHoldingsValue')}</p>}
          </div>
          <div>
            <dt className="text-sm text-muted-foreground">{t('assets.cashAndEquivalents')}</dt>
            <dd className="mt-1 text-lg font-semibold tabular-nums">{money(hasIncompleteBalance && view.cashEquivalentTotal + view.liquidCashTotal === 0 ? null : view.cashEquivalentTotal + view.liquidCashTotal)}</dd>
            {hasIncompleteBalance && <p className="mt-1 text-xs text-muted-foreground">{t('assets.knownSubtotal', 'Known subtotal')}</p>}
          </div>
        </dl>
        <BalanceDetails canWrite={canWrite} workspaceId={workspaceId} wallets={view.wallets.map((wallet) => walletsById.get(wallet.id) ?? wallet)} holdings={detailHoldings} holdingIds={filter?.dim === 'class' && !walletContent ? selectedHoldingIds : undefined} holdingsError={holdingsError} canOpenAccounts={canOpenAccounts} />
        {hasUnknownCash && <p role="status" className="mt-4 text-sm text-muted-foreground">{t('assets.unknownCashHint', 'Some wallet cash balances are unavailable. Balance and cash totals include known values only.')}</p>}
        {walletIncome && <p className="mt-4 text-sm text-muted-foreground">{t('assets.posWalletIncome')} <span className="ml-2 font-medium tabular-nums text-foreground">{money(walletIncome.total)}</span></p>}
        <details className="mt-4 border-t border-border pt-3">
          <summary className="w-fit cursor-pointer rounded-sm text-sm text-muted-foreground hover:text-foreground focus-visible:outline-2 focus-visible:outline-offset-4 focus-visible:outline-ring">{t('balanceExplanation.portfolioBreakdown')}</summary>
          <div className="mt-2">
            {view.cashEquivalentTotal > 0 && renderTotalRow(t('assets.posCashEquivalents'), view.cashEquivalentTotal, incompletePositions.some((position) => position.isCashEquivalent) ? t('assets.knownSubtotal', 'Known subtotal') : t('assets.posCashEquivalentHint'), hasIncompleteBalance ? undefined : shareOfTotal(view.cashEquivalentTotal, view.total))}
            {view.liquidCashTotal > 0 && renderTotalRow(t('assets.posLiquidCash'), view.liquidCashTotal, hasUnknownCash ? t('assets.knownSubtotal', 'Known subtotal') : t('assets.posLiquidCashHint'), hasIncompleteBalance ? undefined : shareOfTotal(view.liquidCashTotal, view.total))}
            {view.dustTotal > 0 && !hasUnpricedPositions && renderTotalRow(t('assets.posDust'), view.dustTotal, t('assets.posDustHint'))}
            {walletIncome && (
              <div className="flex items-baseline justify-between gap-4 py-3 border-t border-border">
                <div className="min-w-0">
                  <span className="text-xs text-muted-foreground">{t('assets.posWalletIncome')}</span>
                  <span className="block text-xs text-muted-foreground">
                    {t('assets.posWalletIncomeHint')}
                  </span>
                </div>
                <span className="tabular-nums shrink-0 text-xs text-muted-foreground">
                  {money(walletIncome.total)}
                  <span className="block text-xs text-right">
                    {walletIncome.cadence
                      ? t(`assets.incomeCadence.${walletIncome.cadence}`)
                      : t('assets.incomeIrregular')}
                    {walletIncome.runRate !== null &&
                      ` · ${t('assets.posRunRate', { amount: money(walletIncome.runRate) })}`}
                  </span>
                </span>
              </div>
            )}
            <p className="pt-3 text-xs leading-relaxed text-muted-foreground">{t('assets.balanceScopeHint')}</p>
          </div>
        </details>
      </section>
      {portfolio.positions.length > 0 && <section aria-label={t('assets.allocationBreakdown')} className="space-y-4">
        <h2 className="text-base font-semibold">{t('assets.allocationBreakdown')}</h2>
        <p className="text-sm text-muted-foreground">{t('assets.allocationScopeHint', 'Allocation covers invested ticker holdings. Cash and other assets are listed separately.')}</p>
        {unpricedIds.size > 0 && <p role="status" className="text-sm text-muted-foreground">{t('assets.unpricedPositionsHint')}</p>}
        {unpricedIds.size === 0 && (
          <div className="grid grid-cols-1 items-stretch gap-4 xl:grid-cols-2">
            {donuts.map((donut) => <div key={donut.dim} className="min-w-0">{renderDonut(donut)}</div>)}
          </div>
        )}
      </section>}
      {children}
      {walletContent ?? (portfolio.positions.length > 0 &&
        <section className="space-y-3" aria-label={t('assets.posRanking')}>
          <div className="flex flex-wrap items-center justify-between gap-3">
            <h2 className="text-sm font-semibold">{t('assets.posRanking')}</h2>
            {activeFilterLabel && (
              <button type="button" onClick={() => setFilter(null)} className="inline-flex min-h-8 items-center gap-2 rounded-md border border-border bg-muted/40 px-2.5 py-1 text-xs hover:bg-muted focus-visible:outline-2 focus-visible:outline-ring">
                {activeFilterLabel}<X size={13} /><span className="sr-only">{t('assets.posFilterClear')}</span>
              </button>
            )}
          </div>
          <div className="rounded-xl border border-border bg-card shadow-sm overflow-x-auto">
            <div className="lg:min-w-[1000px]">
              <div
                className="grid grid-cols-[minmax(0,1fr)_auto_1rem] lg:grid-cols-[var(--position-columns)] items-center gap-2 px-3 py-2 text-[10px] font-semibold text-muted-foreground uppercase tracking-wider border-b border-border"
                style={{ '--position-columns': POSITIONS_GRID } as React.CSSProperties}
              >
                <div>{t('assets.posColTicker')}</div>
                <div className="hidden lg:block text-right">{t('assets.posColQuantity')}</div>
                <div className="hidden lg:block text-right">{t('assets.posColAvgCost')}</div>
                <div className="hidden lg:block text-right">{t('assets.posColCostBasis')}</div>
                <div className="text-right">{t('assets.posColValue')}<span className="block lg:hidden">{t('assets.posColGain')}</span></div>
                <div className="hidden lg:block text-right">{t('assets.posColGain')}</div>
                <div className="hidden lg:block text-right">{t('assets.posColIncome')}</div>
                <div className="hidden lg:block text-right">{t('assets.posColWeight')}</div>
                <div />
              </div>
              {rankedPositions.length === 0 && cashEquivalents.length === 0 && (
                <p className="px-3 py-8 text-center text-sm text-muted-foreground">
                  {t(filter ? 'assets.posNoFilteredPositions' : 'assets.posNoPositions')}
                </p>
              )}
              {rankedPositions.map(renderPositionRow)}
              {cashEquivalents.length > 0 && (
                <>
                  <div className="px-3 py-2 bg-muted/30 border-y border-border">
                    <span className="text-[10px] font-semibold text-muted-foreground uppercase tracking-wider">
                      {t('assets.posCashEquivalents')}
                    </span>
                    <span className="block text-[10px] text-muted-foreground normal-case">
                      {t('assets.posCashEquivalentHint')}
                    </span>
                  </div>
                  {cashEquivalents.map(renderPositionRow)}
                </>
              )}
            </div>
          </div>


        </section>
      )}
    </div>
  )
}
