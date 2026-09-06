import { useId, useState } from 'react'
import { Link } from 'react-router-dom'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { toast } from 'sonner'
import { MoreHorizontal, Unlink } from 'lucide-react'
import { assetGroups } from '@/lib/api'
import { extractApiError } from '@/lib/api-errors'
import { getAccountName } from '@/lib/account-utils'
import { formatCurrency } from '@/lib/format'
import { useDisplayLocale } from '@/hooks/use-display-locale'
import { usePrivacyMode } from '@/hooks/use-privacy-mode'
import { useAuth } from '@/contexts/auth-context'
import { useWorkspace } from '@/contexts/workspace-context'
import { Button } from '@/components/ui/button'
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuTrigger } from '@/components/ui/dropdown-menu'
import type { Account, AssetGroup } from '@/types'

function WalletValue({ wallet }: { wallet: AssetGroup }) {
  const { t } = useTranslation()
  const { user } = useAuth()
  const { mask } = usePrivacyMode()
  const locale = useDisplayLocale()
  const currency = user?.preferences?.currency_display ?? 'USD'
  const missing = wallet.unvalued_count ?? 0
  return (
    <span className="tabular-nums">
      {missing >= wallet.asset_count && missing > 0
        ? t('accountHoldings.unpriced')
        : mask(formatCurrency(wallet.current_value_primary, currency, locale))}
      {missing > 0 && <span className="mt-1 block text-xs font-normal text-warning-foreground">{t('accountHoldings.partial', { count: missing })}</span>}
    </span>
  )
}

/** Holdings remain distinct from the cash ledger and provider account total. */
export function AccountHoldingsSummary({ account, wallets, size = 'default' }: { account: Account; wallets: AssetGroup[]; size?: 'default' | 'large' }) {
  const { t } = useTranslation()
  const { canWrite } = useWorkspace()
  const queryClient = useQueryClient()
  const unlink = useMutation({
    mutationFn: (id: string) => assetGroups.update(id, { account_id: null }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['asset-groups'] }),
    onError: (error) => toast.error(extractApiError(error, t('common.error'))),
  })
  const linked = wallets.filter((wallet) => wallet.account_id === account.id)
  return linked.map((wallet) => (
    <div key={wallet.id} className="flex items-start gap-2">
      <Link to={`/assets?wallet=${encodeURIComponent(wallet.id)}`} className="group/holdings min-w-0 rounded-sm focus-visible:outline-2 focus-visible:outline-offset-4 focus-visible:outline-ring">
        <span className="block text-xs font-medium text-muted-foreground group-hover/holdings:text-primary">{t('accountHoldings.holdings')}<span className="sr-only">:</span></span>{' '}
        <span className={`mt-1 block font-semibold text-foreground group-hover/holdings:text-primary ${size === 'large' ? 'text-2xl' : wallet.source === 'manual' ? 'text-lg' : 'text-sm'}`}><WalletValue wallet={wallet} /></span>
      </Link>
      {wallet.source === 'manual' && canWrite && (
        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <Button type="button" variant="ghost" size="icon" className="-mt-1 h-8 w-8 shrink-0 text-muted-foreground" aria-label={`${t('common.more')}: ${t('accountHoldings.holdings')} · ${wallet.name}`}>
              <MoreHorizontal size={16} />
            </Button>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="end">
            <DropdownMenuItem disabled={unlink.isPending} onSelect={() => unlink.mutate(wallet.id)}>
              <Unlink size={14} />
              {t('accountHoldings.unlink')}
            </DropdownMenuItem>
          </DropdownMenuContent>
        </DropdownMenu>
      )}
    </div>
  ))
}

function WalletLinkRow({ wallet, accounts }: { wallet: AssetGroup; accounts: Account[] }) {
  const { t } = useTranslation()
  const { canWrite } = useWorkspace()
  const queryClient = useQueryClient()
  const selectId = useId()
  const [accountId, setAccountId] = useState('')
  const link = useMutation({
    mutationFn: () => assetGroups.update(wallet.id, { account_id: accountId }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['asset-groups'] })
      toast.success(t('accountHoldings.linked'))
    },
    onError: (error) => toast.error(extractApiError(error, t('common.error'))),
  })
  return (
    <li className="flex flex-col gap-3 py-4 lg:flex-row lg:items-center lg:justify-between">
      <div className="flex min-w-0 flex-wrap items-baseline justify-between gap-x-4 gap-y-1 lg:flex-1">
        <Link to={`/assets?wallet=${encodeURIComponent(wallet.id)}`} className="min-w-0 break-words text-sm font-medium text-primary hover:underline">{wallet.name}</Link>
        <div className="text-base font-semibold"><WalletValue wallet={wallet} /></div>
      </div>
      {canWrite && accounts.length > 0 && (
        <form className="flex min-w-0 items-center gap-2 lg:ml-4 lg:w-80" onSubmit={(event) => { event.preventDefault(); if (accountId) link.mutate() }}>
          <label htmlFor={selectId} className="sr-only">{t('accountHoldings.chooseAccountFor', { wallet: wallet.name })}</label>
          <select id={selectId} value={accountId} onChange={(event) => setAccountId(event.target.value)} disabled={link.isPending} className="h-9 min-w-0 flex-1 rounded-md border border-input bg-background px-2 text-sm focus-visible:outline-2 focus-visible:outline-ring">
            <option value="">{t('accountHoldings.chooseAccount')}</option>
            {accounts.map((account) => <option key={account.id} value={account.id}>{getAccountName(account)}</option>)}
          </select>
          <Button type="submit" variant="outline" size="sm" disabled={!accountId || link.isPending}>{t('accountHoldings.link')}</Button>
        </form>
      )}
    </li>
  )
}

/** Show unlinked portfolio values before asking the user to establish identity. */
export function UnlinkedWallets({ wallets, accounts }: { wallets: AssetGroup[]; accounts: Account[] }) {
  const { t } = useTranslation()
  const { current, canWrite } = useWorkspace()
  const unlinked = wallets.filter((wallet) => wallet.source === 'manual' && !wallet.account_id)
  if (unlinked.length === 0) return null
  const available = accounts.filter((account) => account.connection_id === null && account.type === 'investment' && !account.is_closed && !wallets.some((wallet) => wallet.account_id === account.id))
  return (
    <section id="unlinked-wallets" aria-labelledby="unlinked-wallets-heading" className="scroll-mt-6 rounded-xl border border-border bg-card px-4 py-4 sm:px-5">
      <h2 id="unlinked-wallets-heading" className="text-sm font-semibold">{t('accountHoldings.unlinkedTitle')}</h2>
      <p className="mt-1 max-w-prose text-sm text-muted-foreground">{t('accountHoldings.unlinkedHint')}</p>
      {canWrite && <p className="mt-1 max-w-prose text-sm text-muted-foreground">{t(available.length > 0 ? 'accountHoldings.linkHint' : 'accountHoldings.noEligibleAccounts')}</p>}
      <ul className="mt-2 divide-y divide-border">
        {unlinked.map((wallet) => <WalletLinkRow key={`${current?.id}:${wallet.id}`} wallet={wallet} accounts={available} />)}
      </ul>
    </section>
  )
}
