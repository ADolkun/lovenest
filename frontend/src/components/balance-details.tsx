import { useState, type ReactNode } from 'react'
import { Link, useLocation } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import { Info } from 'lucide-react'
import { useDisplayLocale } from '@/hooks/use-display-locale'
import { usePrivacyMode } from '@/hooks/use-privacy-mode'
import { formatCurrency } from '@/lib/format'
import { accountBalance, accountBasis, holdingComponents, knownAmount, scopedExplanation } from '@/lib/balance-explanation'
import { getAccountName } from '@/lib/account-utils'
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle, DialogTrigger } from '@/components/ui/dialog'
import type { Account, Asset, AssetGroup } from '@/types'
import type { BalanceExplanation, BalanceHolding } from '@/types/balance-explanation'

interface Props {
  canWrite?: boolean
  workspaceId?: string
  canOpenAccounts?: boolean
  canOpenAssets?: boolean
  account?: Account
  wallets?: AssetGroup[]
  holdings?: Asset[]
  /** A class slice has holdings only: account-wide residuals do not belong to it. */
  holdingIds?: string[]
  holdingsError?: boolean
  children?: ReactNode
  autoOpen?: boolean
}

export function AccountBalanceBasis({ account, workspaceId }: { account: Account; workspaceId?: string }) {
  const { t } = useTranslation()
  const detail = scopedExplanation(account, workspaceId)
  const incomplete = account.type === 'investment' && (!detail || detail.coverage !== 'complete' || detail.refresh_status !== 'active')
  return <>{t(`balanceExplanation.basis.${accountBasis(account, workspaceId)}`)}{incomplete && <span className="block text-xs">{t('balanceExplanation.coverageIncomplete')}</span>}</>
}

export function AccountBalanceAmount({ account, workspaceId }: { account: Account; workspaceId?: string }) {
  const { t } = useTranslation()
  const locale = useDisplayLocale()
  const { mask } = usePrivacyMode()
  const amount = accountBalance(account, workspaceId)
  return amount === null ? t('balanceExplanation.unavailable') : mask(formatCurrency(amount, account.currency, locale))
}

/** One read-only explanation across account, wallet and portfolio entrypoints. */
export function BalanceDetails({ account, wallets = [], holdings = [], holdingIds, holdingsError = false, children, autoOpen = false, workspaceId, canOpenAccounts = true, canOpenAssets = true, canWrite = false }: Props) {
  const { t } = useTranslation()
  const locale = useDisplayLocale()
  const { mask } = usePrivacyMode()
  const location = useLocation()
  const scope = `${workspaceId ?? ''}:${location.pathname}${location.search}:${account?.id ?? ''}:${wallets.map((wallet) => wallet.id).join(',')}:${holdingIds?.join(',') ?? ''}`
  const [openScope, setOpenScope] = useState<string | null>(autoOpen ? scope : null)
  const money = (value: number | null | undefined, currency: string | null | undefined) => knownAmount(value) === null || !currency ? t('balanceExplanation.unavailable') : mask(formatCurrency(value!, currency, locale))
  const age = (value: string | null | undefined) => value && Number.isFinite(Date.parse(value)) ? new Date(value).toLocaleString(locale) : t('balanceExplanation.ageUnknown')
  const rows: { id: string; name: string; detail: BalanceExplanation | null; invalid?: boolean }[] = account
    ? [{ id: account.id, name: getAccountName(account), detail: scopedExplanation(account, workspaceId), invalid: !!account.balance_explanation && !scopedExplanation(account, workspaceId) }]
    : wallets.map((wallet) => ({ id: wallet.id, name: wallet.name, detail: scopedExplanation(wallet, workspaceId), invalid: !!wallet.balance_explanation && !scopedExplanation(wallet, workspaceId) }))
  if (rows.length === 0 && holdings.length > 0) rows.push({ id: 'selected-holdings', name: t('balanceExplanation.selectedHoldings'), detail: null })
  const fallbackHoldings = (id: string): BalanceHolding[] => holdings.filter((holding) => !holding.sell_date && !holding.is_archived && (id === 'selected-holdings' || holding.group_id === id)).map((holding) => ({ asset_id: holding.id, wallet_id: holding.group_id, name: holding.name, ticker: holding.ticker, type: holding.type, quantity: holding.units, value: holding.current_value, currency: holding.currency, observed_at: holding.last_price_at ?? holding.value_updated_at, observation_basis: holding.last_price_at ? 'quote' : holding.value_updated_at ? 'recorded' : 'unknown' }))

  return (
    <Dialog open={openScope === scope} onOpenChange={(open) => setOpenScope(open ? scope : null)}>
      <DialogTrigger asChild>
        <button type="button" className="inline-flex min-h-9 items-center gap-1.5 rounded-sm text-left text-xs font-medium text-muted-foreground underline-offset-4 hover:text-foreground hover:underline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring" aria-label={t('balanceExplanation.open', { name: account ? getAccountName(account) : wallets.length === 1 ? wallets[0].name : t('balanceExplanation.currentView') })}>
          {children ?? <><Info size={14} aria-hidden="true" />{t('balanceExplanation.title')}</>}
        </button>
      </DialogTrigger>
      <DialogContent className="max-w-2xl">
        <DialogHeader className="pr-6 text-left">
          <DialogTitle>{t('balanceExplanation.title')}</DialogTitle>
          <DialogDescription>{t('balanceExplanation.description')}</DialogDescription>
        </DialogHeader>
        {holdingsError && <p role="alert" className="text-sm text-warning-foreground">{t('balanceExplanation.holdingsRequestFailed')}</p>}
        {holdingIds && <p className="text-sm text-muted-foreground">{t('balanceExplanation.filteredHoldings')}</p>}
        {rows.every((row) => row.invalid) && <p className="text-sm text-muted-foreground">{t('balanceExplanation.noWallets')}</p>}
        <div className="divide-y divide-border">
          {rows.filter((row) => !row.invalid).map(({ id, name, detail }) => {
            const items = (detail?.holdings ?? fallbackHoldings(id)).filter((holding) => !holdingIds || holdingIds.includes(holding.asset_id))
            const components = holdingComponents(items)
            return <section key={id} aria-label={name} className="space-y-4 py-5 first:pt-0 last:pb-0">
              <h3 className="break-words text-base font-semibold">{name}</h3>
              {!detail && !holdingIds && <p className="text-sm text-muted-foreground">{t('balanceExplanation.metadataUnknown')}</p>}
              {detail && !holdingIds && <>
                <dl className="space-y-3 text-sm">
                  <div className="flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1"><dt className="text-muted-foreground">{t(`balanceExplanation.basis.${detail.basis}`)}</dt><dd className="font-medium tabular-nums">{money(detail.amount, detail.currency)}{detail.currency && <span className="ml-1 text-xs text-muted-foreground">{detail.currency}</span>}</dd></div>
                  <div><dt className="text-muted-foreground">{t(detail.observation_basis === 'collection' ? 'balanceExplanation.collectedAt' : 'balanceExplanation.observedAt')}</dt><dd>{age(detail.observed_at)}</dd></div>
                  <div className="flex flex-wrap gap-x-8 gap-y-3"><div><dt className="text-muted-foreground">{t('balanceExplanation.refresh')}</dt><dd>{t(`balanceExplanation.refreshState.${detail.refresh_status}`, { defaultValue: t('balanceExplanation.refreshState.unknown') })}</dd><dd className="mt-1 text-xs text-muted-foreground">{age(detail.refresh_observed_at)}</dd></div><div><dt className="text-muted-foreground">{t('balanceExplanation.coverage')}</dt><dd>{t(`balanceExplanation.coverageState.${detail.coverage}`)}</dd></div></div>
                  {detail.last_successful_sync_at && <div><dt className="text-muted-foreground">{t('balanceExplanation.lastSync')}</dt><dd>{age(detail.last_successful_sync_at)}</dd></div>}
                </dl>
                <p className="text-sm text-muted-foreground">{t(`balanceExplanation.reconciliation.${detail.reconciliation}`)}</p>
                {detail.reconciliation === 'difference' && <p className="text-sm font-medium">{t('balanceExplanation.difference')}: <span className="tabular-nums">{money(detail.difference, detail.currency)}</span></p>}
                {detail.reason_codes.length > 0 && <ul className="space-y-2 text-sm text-muted-foreground">{detail.reason_codes.map((reason) => <li key={reason}>{t(`balanceExplanation.reason.${reason}`, { defaultValue: t('balanceExplanation.reason.unknown') })}</li>)}</ul>}
                {Math.max(detail.unreadable_count ?? 0, detail.omitted_count ?? 0) > 0 && <p className="text-sm text-warning-foreground">{t('balanceExplanation.omitted', { count: Math.max(detail.unreadable_count ?? 0, detail.omitted_count ?? 0) })}</p>}
              </>}
              {components.length > 0 && <div>
                <h4 className="text-sm font-medium">{t('balanceExplanation.components')}</h4>
                <dl className="mt-2 divide-y divide-border text-sm">{components.map((component) => <div key={`${component.kind}:${component.currency}`} className="flex flex-wrap justify-between gap-x-4 gap-y-1 py-2"><dt className="text-muted-foreground">{t(`balanceExplanation.component.${component.kind}`)}{component.kind === 'non_ticker_assets' && <span className="block text-xs">{t('balanceExplanation.excluded')}</span>}{component.partial && <span className="block text-xs">{t('assets.knownSubtotal')}</span>}</dt><dd className="tabular-nums">{money(component.amount, component.currency)} {component.currency}</dd></div>)}</dl>
              </div>}
              {detail && !holdingIds && <dl className="text-sm"><div className="flex flex-wrap justify-between gap-2"><dt className="text-muted-foreground">{t('balanceExplanation.component.liquid_cash')}</dt><dd className="tabular-nums">{money(holdingsError ? null : detail.residual_cash, detail.holdings_currency)} {detail.holdings_currency}</dd></div></dl>}
              {items.length > 0 && <div>
                <h4 className="text-sm font-medium">{t('balanceExplanation.holdings')}</h4>
                <ul className="mt-2 divide-y divide-border">{items.map((holding) => <li key={holding.asset_id} className="flex flex-wrap items-start justify-between gap-x-4 gap-y-2 py-3 text-sm">
                  <div className="min-w-0 flex-1 basis-48"><p className="break-words font-medium">{holding.name}</p><p className="mt-1 text-xs text-muted-foreground">{t('balanceExplanation.quantity')}: {knownAmount(holding.quantity) === null ? t('balanceExplanation.unavailable') : mask(String(holding.quantity))}</p><p className="mt-1 text-xs text-muted-foreground">{t(holding.observation_basis === 'recorded' ? 'balanceExplanation.recordedAt' : holding.observation_basis === 'quote' ? 'balanceExplanation.quoteAt' : 'balanceExplanation.observedAt')}: {age(holding.observed_at)}</p>{holding.value_date && <p className="mt-1 text-xs text-muted-foreground">{t('balanceExplanation.valueDate')}: {holding.value_date}</p>}</div>
                  <div className="text-right"><p className="tabular-nums">{money(holding.value, holding.currency)} {holding.currency}</p>{knownAmount(holding.value) === null && <p className="mt-1 text-xs text-warning-foreground">{t('balanceExplanation.missingValue')}</p>}{canOpenAssets && <Link className="mt-1 inline-flex min-h-9 items-center text-xs font-medium text-primary hover:underline focus-visible:outline-2 focus-visible:outline-ring" onClick={() => setOpenScope(null)} to={`/assets?tab=portfolio&view=wallets${holding.wallet_id ? `&wallet=${encodeURIComponent(holding.wallet_id)}` : ''}&holding=${encodeURIComponent(holding.asset_id)}`}>{t('balanceExplanation.openHolding')}</Link>}</div>
                </li>)}</ul>
              </div>}
              {canOpenAccounts && <div className="flex flex-wrap gap-2">
                {detail?.account_id && <Button asChild variant="outline" size="sm"><Link onClick={() => setOpenScope(null)} to={`/accounts/${encodeURIComponent(detail.account_id)}`}>{t('balanceExplanation.openAccount')}</Link></Button>}
                {detail?.connection_id && <Button asChild variant="outline" size="sm"><Link onClick={() => setOpenScope(null)} to={canWrite ? `/accounts?review=${encodeURIComponent(detail.connection_id)}` : `/accounts#connection-${encodeURIComponent(detail.connection_id)}`}>{t('balanceExplanation.openConnection')}</Link></Button>}
                {!holdingIds && (!detail || detail.association !== 'resolved') && <Button asChild variant="outline" size="sm"><Link onClick={() => setOpenScope(null)} to="/accounts#unlinked-wallets">{t('balanceExplanation.reviewAssociation')}</Link></Button>}
              </div>}
            </section>
          })}
        </div>
      </DialogContent>
    </Dialog>
  )
}
