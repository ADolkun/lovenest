import { useTranslation } from 'react-i18next'
import { formatCurrency } from '@/lib/format'
import { useDisplayLocale } from '@/hooks/use-display-locale'
import { Button } from '@/components/ui/button'
import { Label } from '@/components/ui/label'
import { Badge } from '@/components/ui/badge'
import type { ProviderAccount } from '@/types'

interface ProviderAccountPickerProps {
  accounts: ProviderAccount[] | undefined
  selected: Set<string>
  allSelected: boolean
  isLoading: boolean
  isError: boolean
  onToggle: (externalId: string) => void
  onToggleAll: () => void
  onRetry: () => void
}

export function ProviderAccountPicker({
  accounts,
  selected,
  allSelected,
  isLoading,
  isError,
  onToggle,
  onToggleAll,
  onRetry,
}: ProviderAccountPickerProps) {
  const { t } = useTranslation()
  const locale = useDisplayLocale()

  return (
    <div className="space-y-2 border-t border-border pt-4">
      <div className="flex items-center justify-between gap-4">
        <Label>{t('connections.syncedAccounts')}</Label>
        {!!accounts?.length && (
          <Button variant="ghost" size="sm" className="h-7 px-2 text-xs" onClick={onToggleAll}>
            {allSelected ? t('connections.deselectAll') : t('connections.selectAll')}
          </Button>
        )}
      </div>
      <p className="text-[11px] text-muted-foreground">{t('connections.syncedAccountsHint')}</p>
      {isLoading ? (
        <p className="text-xs text-muted-foreground">{t('common.loading')}</p>
      ) : isError ? (
        <div className="flex items-start justify-between gap-3 rounded-lg border border-amber-500/30 bg-amber-500/10 px-3 py-2 text-xs text-amber-700 dark:text-amber-300">
          <p>{t('connections.accountsLoadError')}</p>
          <button type="button" className="shrink-0 underline underline-offset-2" onClick={onRetry}>
            {t('common.retry')}
          </button>
        </div>
      ) : accounts?.length ? (
        <div className="max-h-56 space-y-1 overflow-y-auto">
          {accounts.map((account) => (
            <label
              key={account.external_id}
              htmlFor={`provider-account-${account.external_id}`}
              className={`flex cursor-pointer items-center gap-3 rounded-lg border px-3 py-2 ${
                account.status === 'pending' ? 'border-amber-500/30 bg-amber-500/5' : 'border-border'
              }`}
            >
              <input
                id={`provider-account-${account.external_id}`}
                type="checkbox"
                checked={selected.has(account.external_id)}
                onChange={() => onToggle(account.external_id)}
                className="h-4 w-4 rounded border-border text-primary focus:ring-primary"
              />
              <div className="min-w-0 flex-1">
                <div className="flex items-center gap-2">
                  <p className="truncate text-sm text-foreground">{account.name}</p>
                  {account.status === 'pending' && (
                    <Badge className="h-4 border border-amber-500/30 bg-amber-500/10 px-1.5 py-0 text-[10px] text-amber-700 dark:text-amber-300">
                      {t('connections.accountPending')}
                    </Badge>
                  )}
                </div>
                <p className="text-[11px] text-muted-foreground">
                  {formatCurrency(Number(account.balance), account.currency, locale)}
                  {account.has_holdings && ` · ${t('connections.accountHasHoldings')}`}
                </p>
              </div>
            </label>
          ))}
        </div>
      ) : (
        <p className="text-xs text-muted-foreground">{t('connections.accountsEmpty')}</p>
      )}
    </div>
  )
}
