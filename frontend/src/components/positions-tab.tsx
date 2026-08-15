import { useMemo, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { useQuery } from '@tanstack/react-query'
import { PieChart, Pie, Cell, Tooltip, ResponsiveContainer } from 'recharts'
import { ChevronDown, ChevronUp } from 'lucide-react'
import { Badge } from '@/components/ui/badge'
import { assets as assetsApi } from '@/lib/api'
import { assetTypeI18nKey, getTypeConfig } from '@/lib/asset-types'
import { formatCurrency } from '@/lib/format'
import {
  buildPortfolio,
  CASH_EQUIVALENT_TYPE,
  type AllocationSlice,
  type Position,
} from '@/lib/positions'
import type { Asset, AssetGroup } from '@/types'

interface PositionsTabProps {
  holdings: Asset[]
  wallets: AssetGroup[]
  currency: string
  locale: string
  dateLocale: string
  mask: (value: string) => string
  canWrite: boolean
  /** Reclassify one Holding — the user's verdict on what counts as cash. */
  onClassify: (assetId: string, type: string) => void
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

const POSITIONS_GRID = 'minmax(0,2.2fr) 0.8fr 1fr 1.1fr 1.2fr 1.3fr 0.7fr 2rem'
const LEGS_GRID = 'minmax(0,2.2fr) 1fr 0.8fr 1.1fr 1.2fr 1.3fr'

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

interface DonutDatum {
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
  const { data, isError } = useQuery({
    queryKey: ['asset-tax-lots', assetId],
    queryFn: () => assetsApi.taxLots(assetId),
  })

  const money = (value: number) => mask(formatCurrency(value, currency, locale))
  const day = (iso: string) => new Date(`${iso}T00:00:00`).toLocaleDateString(dateLocale)
  const hint = (text: string) => (
    <div className="px-3 py-2 bg-background/60 border-t border-border">
      <p className="text-[11px] text-muted-foreground italic">{text}</p>
    </div>
  )

  if (!data) return hint(isError ? t('common.error') : t('common.loading'))
  // A gain in a Tax-Advantaged wallet is never Reportable, so it has no
  // long-versus-short answer to give.
  if (!data.tax_character) return hint(t('assets.lotsNoTaxCharacter'))
  if (data.snapshot) return hint(t('assets.lotsSnapshot'))
  if (data.lots.length === 0 && data.sales.length === 0) return hint(t('assets.lotsNone'))

  const character = (long: boolean) => (long ? t('assets.lotsLong') : t('assets.lotsShort'))

  return (
    <div className="px-3 py-2 bg-background/60 border-t border-border">
      <p className="text-[10px] font-semibold text-muted-foreground uppercase tracking-wider">
        {t('assets.lotsTitle')}
      </p>
      {data.lots.length > 0 && (
        <>
          <div
            className="grid items-center gap-2 py-1.5 text-[10px] font-semibold text-muted-foreground uppercase tracking-wider"
            style={{ gridTemplateColumns: LOTS_GRID }}
          >
            <div>{t('assets.lotsColAcquired')}</div>
            <div className="text-right">{t('assets.posColQuantity')}</div>
            <div className="text-right">{t('assets.lotsColUnitPrice')}</div>
            <div className="text-right">{t('assets.posColCostBasis')}</div>
            <div className="text-right">{t('assets.lotsColHoldingPeriod')}</div>
          </div>
          {data.lots.map((lot, i) => (
            <div
              key={`${lot.acquired}-${i}`}
              className="grid items-center gap-2 py-1.5 text-xs border-t border-border/50"
              style={{ gridTemplateColumns: LOTS_GRID }}
            >
              <div className="text-foreground">{day(lot.acquired)}</div>
              <div className="text-right tabular-nums text-muted-foreground">{mask(`${lot.quantity}`)}</div>
              <div className="text-right tabular-nums text-muted-foreground">{money(lot.unit_price)}</div>
              <div className="text-right tabular-nums text-muted-foreground">{money(lot.cost)}</div>
              <div className="text-right">
                <Badge
                  variant="outline"
                  className={`text-[9px] px-1 py-0 ${lot.long_term ? 'text-emerald-600' : 'text-amber-600'}`}
                >
                  {character(lot.long_term)}
                </Badge>
                <span className="block text-[10px] text-muted-foreground tabular-nums">
                  {lot.long_term
                    ? t('assets.lotsHeldDays', { count: lot.holding_days })
                    : t('assets.lotsLongIn', { count: lot.days_until_long_term })}
                </span>
              </div>
            </div>
          ))}
          <div className="flex flex-wrap gap-x-4 gap-y-1 pt-2 text-[11px] text-muted-foreground">
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
          <span className={gainClass(data.realised_long)}>
            {t('assets.lotsLong')} {money(data.realised_long)}
          </span>
          <span className={gainClass(data.realised_short)}>
            {t('assets.lotsShort')} {money(data.realised_short)}
          </span>
        </div>
      )}
    </div>
  )
}

export default function PositionsTab({
  holdings,
  wallets,
  currency,
  locale,
  dateLocale,
  mask,
  canWrite,
  onClassify,
}: PositionsTabProps) {
  const { t } = useTranslation()
  const [expandedTicker, setExpandedTicker] = useState<string | null>(null)

  const portfolio = useMemo(() => buildPortfolio(holdings, wallets), [holdings, wallets])

  const assetClassLabel = (type: string) => t(assetTypeI18nKey(type))
  const accountTypeLabel = (type: string | null) =>
    type && ACCOUNT_TYPE_KEYS[type] ? t(ACCOUNT_TYPE_KEYS[type]) : t('assets.posAccountUnknown')

  const money = (value: number | null) =>
    value === null ? DASH : mask(formatCurrency(value, currency, locale))

  const toDonutData = (slices: AllocationSlice[], label: (key: string) => string): DonutDatum[] =>
    slices.map((slice, i) => ({
      label: label(slice.key),
      value: slice.value,
      weight: slice.weight,
      color: SLICE_COLORS[i % SLICE_COLORS.length],
    }))

  const byClassData = toDonutData(portfolio.byAssetClass, assetClassLabel)
  const byAccountData = toDonutData(portfolio.byAccountType, accountTypeLabel)

  // The ranking answers "where is my concentration risk", so what allocation
  // leaves out stays out here too — a 49k money-market row heading a table
  // ranked by a weight it has none of reads as the biggest position there is.
  const rankedPositions = portfolio.positions.filter((p) => !p.isDust && !p.isCashEquivalent)
  // Still listed, just under their own heading — this is the only place the
  // user can see what was classified as cash and put it back.
  const cashEquivalents = portfolio.positions.filter((p) => p.isCashEquivalent && !p.isDust)

  if (portfolio.positions.length === 0) {
    return (
      <div className="rounded-xl border border-border bg-card shadow-sm px-4 py-10 text-center">
        <p className="text-sm text-muted-foreground">{t('assets.posNoPositions')}</p>
      </div>
    )
  }

  function renderDonut(title: string, data: DonutDatum[]) {
    return (
      <div className="rounded-xl border border-border bg-card shadow-sm p-4">
        <p className="text-sm font-semibold text-foreground mb-2">{title}</p>
        {data.length === 0 ? (
          <p className="text-xs text-muted-foreground italic py-8 text-center">{t('assets.posNoPositions')}</p>
        ) : (
          <div className="flex flex-col items-center">
            <div className="relative w-full" style={{ height: 200 }}>
              <ResponsiveContainer width="100%" height={200}>
                <PieChart>
                  <Pie
                    data={data}
                    cx="50%"
                    cy="50%"
                    innerRadius={55}
                    outerRadius={85}
                    paddingAngle={3}
                    dataKey="value"
                    stroke="var(--card)"
                    strokeWidth={0}
                  >
                    {data.map((entry, idx) => (
                      <Cell key={idx} fill={entry.color} />
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
              <div className="absolute inset-0 flex flex-col items-center justify-center pointer-events-none">
                <span className="text-[10px] text-muted-foreground">{t('assets.posInvestedTotal')}</span>
                <span className="text-base font-bold text-foreground tabular-nums">
                  {money(portfolio.investedTotal)}
                </span>
              </div>
            </div>
            <div className="flex flex-wrap justify-center gap-x-3 gap-y-1 mt-3">
              {data.map((d, i) => (
                <div key={`${i}-${d.label}`} className="flex items-center gap-1.5">
                  <div className="w-2 h-2 rounded-full shrink-0" style={{ backgroundColor: d.color }} />
                  <span className="text-[11px] text-muted-foreground whitespace-nowrap">
                    {d.label} · {formatPercent(d.weight)}
                  </span>
                </div>
              ))}
            </div>
          </div>
        )}
      </div>
    )
  }

  function renderLegs(position: Position) {
    return (
      <div className="bg-muted/20 border-t border-border px-3 py-2">
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
        </div>
        {position.legs.map((leg) => (
          <div key={leg.assetId}>
            <div
              className="grid items-center gap-2 py-1.5 text-xs border-t border-border/50"
              style={{ gridTemplateColumns: LEGS_GRID }}
            >
              <div className="min-w-0">
                <span className="font-medium text-foreground truncate block">
                  {leg.walletName ?? t('assets.noWallet')}
                </span>
                <span className="text-[10px] text-muted-foreground">
                  {accountTypeLabel(leg.accountType)}
                </span>
              </div>
              <div className="text-right tabular-nums text-muted-foreground">{mask(`${leg.quantity}`)}</div>
              <div className="text-right text-[10px] text-muted-foreground">
                {leg.taxTreatment ? t(`assets.taxTreatment.${leg.taxTreatment}`) : DASH}
              </div>
              <div className="text-right tabular-nums text-muted-foreground">{money(leg.costBasis)}</div>
              <div className="text-right tabular-nums text-foreground">{money(leg.value)}</div>
              <div className="text-right tabular-nums">
                {leg.gain === null ? (
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
            </div>
            {/* Per wallet, not per ticker: tax character attaches to the wallet,
                so a split blending a taxable leg with a Roth one would be a
                figure no tax return could use. */}
            <TaxLotsPanel
              assetId={leg.assetId}
              currency={currency}
              locale={locale}
              dateLocale={dateLocale}
              mask={mask}
            />
          </div>
        ))}
      </div>
    )
  }

  function renderPositionRow(position: Position) {
    const isExpanded = expandedTicker === position.ticker
    return (
      <div key={position.ticker} className="border-b border-border last:border-b-0">
        <div
          className="grid items-center gap-2 px-3 py-3 cursor-pointer hover:bg-muted/20 transition-colors text-sm"
          style={{ gridTemplateColumns: POSITIONS_GRID }}
          onClick={() => setExpandedTicker(isExpanded ? null : position.ticker)}
        >
          <div className="flex items-center gap-2.5 min-w-0">
            <PositionIcon logoUrl={position.logoUrl} type={position.assetType} />
            <div className="min-w-0">
              <div className="flex items-center gap-1.5">
                <span className="font-semibold text-foreground truncate">{position.ticker}</span>
                {position.isCashEquivalent && (
                  <Badge variant="outline" className="text-[9px] px-1 py-0 text-muted-foreground shrink-0">
                    {t('assets.posExcluded')}
                  </Badge>
                )}
              </div>
              <span className="text-[11px] text-muted-foreground truncate block">{position.name}</span>
            </div>
          </div>
          <div className="text-right tabular-nums text-muted-foreground">{mask(`${position.quantity}`)}</div>
          <div className="text-right tabular-nums text-muted-foreground">{money(position.averageCost)}</div>
          <div className="text-right tabular-nums text-muted-foreground">{money(position.costBasis)}</div>
          <div className="text-right tabular-nums font-semibold text-foreground">{money(position.value)}</div>
          <div className="text-right tabular-nums">
            {position.gain === null ? (
              <span className="text-muted-foreground">{DASH}</span>
            ) : (
              <span className={gainClass(position.gain)}>
                {money(position.gain)}
                {position.gainPct !== null && (
                  <span className="block text-[10px]">
                    {position.gainPct >= 0 ? '+' : ''}
                    {(position.gainPct * 100).toFixed(1)}%
                  </span>
                )}
              </span>
            )}
          </div>
          <div className="text-right tabular-nums text-muted-foreground">
            {position.weight === null ? DASH : formatPercent(position.weight)}
          </div>
          <div className="flex items-center justify-end">
            {isExpanded ? (
              <ChevronUp size={15} className="text-muted-foreground" />
            ) : (
              <ChevronDown size={15} className="text-muted-foreground" />
            )}
          </div>
        </div>
        {isExpanded && renderLegs(position)}
      </div>
    )
  }

  function renderTotalRow(label: string, value: number, hint?: string, emphasis = false) {
    return (
      <div className="flex items-baseline justify-between gap-4 px-3 py-2 border-t border-border">
        <div className="min-w-0">
          <span className={`text-xs ${emphasis ? 'font-semibold text-foreground' : 'text-muted-foreground'}`}>
            {label}
          </span>
          {hint && <span className="block text-[10px] text-muted-foreground">{hint}</span>}
        </div>
        <span
          className={`tabular-nums shrink-0 ${emphasis ? 'text-sm font-bold text-foreground' : 'text-xs text-muted-foreground'}`}
        >
          {money(value)}
        </span>
      </div>
    )
  }

  return (
    <div className="flex flex-col gap-4">
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        {renderDonut(t('assets.posAllocationByClass'), byClassData)}
        {renderDonut(t('assets.posAllocationByAccountType'), byAccountData)}
      </div>

      <div className="rounded-xl border border-border bg-card shadow-sm overflow-x-auto">
        <div className="min-w-[860px]">
          <p className="text-sm font-semibold text-foreground px-3 pt-3 pb-2">{t('assets.posRanking')}</p>
          <div
            className="grid items-center gap-2 px-3 py-2 text-[10px] font-semibold text-muted-foreground uppercase tracking-wider border-b border-border"
            style={{ gridTemplateColumns: POSITIONS_GRID }}
          >
            <div>{t('assets.posColTicker')}</div>
            <div className="text-right">{t('assets.posColQuantity')}</div>
            <div className="text-right">{t('assets.posColAvgCost')}</div>
            <div className="text-right">{t('assets.posColCostBasis')}</div>
            <div className="text-right">{t('assets.posColValue')}</div>
            <div className="text-right">{t('assets.posColGain')}</div>
            <div className="text-right">{t('assets.posColWeight')}</div>
            <div />
          </div>
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

      <div className="rounded-xl border border-border bg-card shadow-sm py-1">
        {renderTotalRow(t('assets.posInvestedTotal'), portfolio.investedTotal)}
        {portfolio.cashEquivalentTotal > 0 &&
          renderTotalRow(
            t('assets.posCashEquivalents'),
            portfolio.cashEquivalentTotal,
            t('assets.posCashEquivalentHint'),
          )}
        {portfolio.dustTotal > 0 &&
          renderTotalRow(t('assets.posDust'), portfolio.dustTotal, t('assets.posDustHint'))}
        {renderTotalRow(t('assets.posGrandTotal'), portfolio.total, undefined, true)}
      </div>
    </div>
  )
}
