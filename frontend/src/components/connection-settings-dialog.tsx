import { useState, useEffect } from 'react'
import { useTranslation } from 'react-i18next'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { connections } from '@/lib/api'
import { buildAllowlist, initialSelection, shouldSaveAllowlist } from '@/lib/account-allowlist'
import { formatCurrency } from '@/lib/format'
import { useDisplayLocale } from '@/hooks/use-display-locale'
import { toast } from 'sonner'
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogFooter,
} from '@/components/ui/dialog'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Badge } from '@/components/ui/badge'
import type { BankConnection, ConnectionSettings } from '@/types'

type PayeeSource = NonNullable<ConnectionSettings['payee_source']>

interface ConnectionSettingsDialogProps {
  open: boolean
  onClose: () => void
  onReconnect: () => void
  connection: BankConnection | null
  supportsAssetSync?: boolean
}

export function ConnectionSettingsDialog({
  open,
  onClose,
  onReconnect,
  connection,
  supportsAssetSync = false,
}: ConnectionSettingsDialogProps) {
  const { t } = useTranslation()
  const queryClient = useQueryClient()
  const locale = useDisplayLocale()

  const [displayName, setDisplayName] = useState('')
  const [payeeSource, setPayeeSource] = useState<PayeeSource>('auto')
  const [importPending, setImportPending] = useState(true)
  const [syncAssets, setSyncAssets] = useState(true)
  const [selected, setSelected] = useState<Set<string>>(new Set())

  useEffect(() => {
    if (connection) {
      setDisplayName(connection.display_name ?? '')
      setPayeeSource(connection.settings?.payee_source ?? 'auto')
      setImportPending(connection.settings?.import_pending ?? true)
      setSyncAssets(connection.settings?.sync_assets ?? true)
    }
  }, [connection])

  // Only while the dialog is open, and exactly once per opening: this one costs
  // a provider request against a daily budget, and a refetch behind the user's
  // back would also reset the ticks they have not saved yet. gcTime 0 is what
  // makes reopening fetch fresh rather than replay a cached list.
  const providerAccounts = useQuery({
    // Deliberately not under the 'connections' key: invalidating that list
    // after a save or a sync would spend another provider request.
    queryKey: ['provider-accounts', connection?.id],
    queryFn: () => connections.listProviderAccounts(connection!.id),
    enabled: open && !!connection,
    staleTime: Infinity,
    gcTime: 0,
    refetchOnWindowFocus: false,
    refetchOnReconnect: false,
    retry: false,
  })
  const accountList = providerAccounts.data

  useEffect(() => {
    if (accountList) setSelected(initialSelection(accountList))
  }, [accountList])

  const allSelected = !!accountList?.length && accountList.every((a) => selected.has(a.external_id))

  const toggle = (externalId: string) =>
    setSelected((prev) => {
      const next = new Set(prev)
      if (!next.delete(externalId)) next.add(externalId)
      return next
    })

  const mutation = useMutation({
    mutationFn: () =>
      connections.updateSettings(connection!.id, {
        display_name: displayName.trim() || null,
        payee_source: payeeSource,
        import_pending: importPending,
        // Only persist asset-sync for connectors that actually import holdings.
        ...(supportsAssetSync ? { sync_assets: syncAssets } : {}),
        // Omitted when the listing failed: an allowlist rebuilt from accounts
        // we could not read would exclude everything the provider didn't return.
        ...(accountList &&
        shouldSaveAllowlist(selected, accountList, connection!.settings?.account_allowlist)
          ? {
              account_allowlist: buildAllowlist(
                selected,
                accountList,
                connection!.settings?.account_allowlist,
              ),
            }
          : {}),
      }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['connections'] })
      toast.success(t('accounts.updated'))
      onClose()
    },
    onError: () => toast.error(t('common.error')),
  })

  return (
    <Dialog open={open} onOpenChange={onClose}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>{t('connections.settings')}</DialogTitle>
        </DialogHeader>
        <div className="space-y-4">
          <div className="space-y-2">
            <Label htmlFor="connection-display-name">{t('connections.displayName')}</Label>
            <Input
              id="connection-display-name"
              value={displayName}
              onChange={(e) => setDisplayName(e.target.value)}
              placeholder={connection?.institution_name ?? t('connections.displayNamePlaceholder')}
              maxLength={255}
            />
            <p className="text-[11px] text-muted-foreground">{t('connections.displayNameHint')}</p>
          </div>
          <div className="space-y-2">
            <Label>{t('connections.payeeSource')}</Label>
            <select
              className="w-full border border-border rounded-lg px-3 py-2 text-sm bg-card text-foreground focus:outline-none focus:ring-2 focus:ring-primary"
              value={payeeSource}
              onChange={(e) => setPayeeSource(e.target.value as PayeeSource)}
            >
              <option value="auto">{t('connections.payeeAuto')}</option>
              <option value="merchant">{t('connections.payeeMerchant')}</option>
              <option value="payment_data">{t('connections.payeePaymentData')}</option>
              <option value="description">{t('connections.payeeDescription')}</option>
              <option value="none">{t('connections.payeeNone')}</option>
            </select>
          </div>
          <div className="flex items-center justify-between">
            <Label htmlFor="import-pending">{t('connections.importPending')}</Label>
            <input
              id="import-pending"
              type="checkbox"
              checked={importPending}
              onChange={(e) => setImportPending(e.target.checked)}
              className="h-4 w-4 rounded border-border text-primary focus:ring-primary"
            />
          </div>
          {supportsAssetSync && (
            <div className="flex items-start justify-between gap-4">
              <div className="space-y-1">
                <Label htmlFor="sync-assets">{t('connections.syncAssets')}</Label>
                <p className="text-[11px] text-muted-foreground">{t('connections.syncAssetsHint')}</p>
              </div>
              <input
                id="sync-assets"
                type="checkbox"
                checked={syncAssets}
                onChange={(e) => setSyncAssets(e.target.checked)}
                className="mt-1 h-4 w-4 rounded border-border text-primary focus:ring-primary"
              />
            </div>
          )}
          <div className="space-y-2 border-t border-border pt-4">
            <div className="flex items-center justify-between gap-4">
              <Label>{t('connections.syncedAccounts')}</Label>
              {!!accountList?.length && (
                <Button
                  variant="ghost"
                  size="sm"
                  className="h-7 px-2 text-xs"
                  onClick={() =>
                    setSelected(
                      allSelected ? new Set() : new Set(accountList.map((a) => a.external_id)),
                    )
                  }
                >
                  {allSelected ? t('connections.deselectAll') : t('connections.selectAll')}
                </Button>
              )}
            </div>
            <p className="text-[11px] text-muted-foreground">{t('connections.syncedAccountsHint')}</p>
            {providerAccounts.isLoading ? (
              <p className="text-xs text-muted-foreground">{t('common.loading')}</p>
            ) : providerAccounts.isError ? (
              <div className="flex items-start justify-between gap-3 rounded-lg border border-amber-500/30 bg-amber-500/10 px-3 py-2 text-xs text-amber-700 dark:text-amber-300">
                <p>{t('connections.accountsLoadError')}</p>
                <button
                  type="button"
                  className="shrink-0 underline underline-offset-2"
                  onClick={() => void providerAccounts.refetch()}
                >
                  {t('common.retry')}
                </button>
              </div>
            ) : accountList?.length ? (
              <div className="max-h-56 space-y-1 overflow-y-auto">
                {accountList.map((account) => (
                  <label
                    key={account.external_id}
                    htmlFor={`provider-account-${account.external_id}`}
                    className={`flex cursor-pointer items-center gap-3 rounded-lg border px-3 py-2 ${
                      account.status === 'pending'
                        ? 'border-amber-500/30 bg-amber-500/5'
                        : 'border-border'
                    }`}
                  >
                    <input
                      id={`provider-account-${account.external_id}`}
                      type="checkbox"
                      checked={selected.has(account.external_id)}
                      onChange={() => toggle(account.external_id)}
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
        </div>
        <DialogFooter>
          <Button variant="outline" onClick={onReconnect}>
            {t('accounts.reconnect')}
          </Button>
          <Button variant="outline" onClick={onClose}>
            {t('common.cancel')}
          </Button>
          <Button onClick={() => mutation.mutate()} disabled={mutation.isPending}>
            {mutation.isPending ? t('common.loading') : t('common.save')}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
