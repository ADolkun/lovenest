import { useState, useMemo } from 'react'
import { useParams, Link } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { format } from 'date-fns'
import { accounts, transactions, categories as categoriesApi } from '@/lib/api'
import { toast } from 'sonner'
import type { Transaction } from '@/types'
import { Skeleton } from '@/components/ui/skeleton'
import { Button } from '@/components/ui/button'
import { ArrowLeft, ArrowLeftRight, Clock, Paperclip, X } from 'lucide-react'
import { CategoryIcon } from '@/components/category-icon'
import { TransactionDialog, extractApiError } from '@/components/transaction-dialog'
import { DatePickerInput } from '@/components/ui/date-picker-input'
import { usePrivacyMode } from '@/hooks/use-privacy-mode'
import { useAuth } from '@/contexts/auth-context'
import {
  AreaChart,
  Area,
  XAxis,
  YAxis,
  Tooltip,
  ResponsiveContainer,
} from 'recharts'

function defaultFrom() {
  const now = new Date()
  return format(new Date(now.getFullYear(), now.getMonth(), 1), 'yyyy-MM-dd')
}

function defaultTo() {
  return format(new Date(), 'yyyy-MM-dd')
}

function formatCurrency(value: number, currency = 'USD', locale = 'en-US') {
  return new Intl.NumberFormat(locale, { style: 'currency', currency }).format(value)
}

function formatDateStr(dateStr: string, locale = 'pt-BR') {
  return new Date(dateStr + 'T00:00:00').toLocaleDateString(locale)
}

type TxWithBalance = Transaction & { runningBalance: number }

export default function AccountDetailPage() {
  const { id } = useParams<{ id: string }>()
  const { t, i18n } = useTranslation()
  const { mask, privacyMode, MASK } = usePrivacyMode()
  const { user } = useAuth()
  const userCurrency = user?.preferences?.currency_display ?? 'USD'
  const locale = i18n.language === 'en' ? 'en-US' : i18n.language
  const queryClient = useQueryClient()
  const [dialogOpen, setDialogOpen] = useState(false)
  const [editingTx, setEditingTx] = useState<Transaction | null>(null)
  const [filterFrom, setFilterFrom] = useState(defaultFrom)
  const [filterTo, setFilterTo] = useState(defaultTo)

  const { data: account, isLoading: accountLoading } = useQuery({
    queryKey: ['accounts', id],
    queryFn: () => accounts.get(id!),
    enabled: !!id,
  })

  const { data: summary, isLoading: summaryLoading } = useQuery({
    queryKey: ['accounts', id, 'summary', filterFrom, filterTo],
    queryFn: () => accounts.summary(id!, filterFrom || undefined, filterTo || undefined),
    enabled: !!id,
  })

  const { data: balanceHistory, isLoading: balanceHistoryLoading } = useQuery({
    queryKey: ['accounts', id, 'balance-history', filterFrom, filterTo],
    queryFn: () => accounts.balanceHistory(id!, filterFrom || undefined, filterTo || undefined),
    enabled: !!id,
  })

  const { data: txData, isLoading: txLoading } = useQuery({
    queryKey: ['transactions', { account_id: id, from: filterFrom, to: filterTo, limit: 500, include_opening_balance: true }],
    queryFn: () => transactions.list({
      account_id: id,
      from: filterFrom || undefined,
      to: filterTo || undefined,
      limit: 500,
      include_opening_balance: true,
    }),
    enabled: !!id,
  })

  const { data: categoriesList } = useQuery({
    queryKey: ['categories'],
    queryFn: categoriesApi.list,
  })

  const updateMutation = useMutation({
    mutationFn: ({ id: txId, ...data }: Partial<Transaction> & { id: string }) =>
      transactions.update(txId, data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['transactions'] })
      queryClient.invalidateQueries({ queryKey: ['accounts', id, 'summary'] })
      queryClient.invalidateQueries({ queryKey: ['accounts', id, 'balance-history'] })
      setDialogOpen(false)
      setEditingTx(null)
      toast.success(t('accounts.updated'))
    },
    onError: (error) => {
      toast.error(extractApiError(error))
    },
  })

  const deleteMutation = useMutation({
    mutationFn: (txId: string) => transactions.delete(txId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['transactions'] })
      queryClient.invalidateQueries({ queryKey: ['accounts', id, 'summary'] })
      queryClient.invalidateQueries({ queryKey: ['accounts', id, 'balance-history'] })
      setDialogOpen(false)
      setEditingTx(null)
      toast.success(t('transactions.deleted'))
    },
    onError: (error) => {
      toast.error(extractApiError(error))
    },
  })

  const reopenMutation = useMutation({
    mutationFn: () => accounts.reopen(id!),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['accounts'] })
      queryClient.invalidateQueries({ queryKey: ['accounts', id] })
      toast.success(t('accounts.accountReopened'))
    },
    onError: () => toast.error(t('common.error')),
  })

  // Chart data — simple daily balance series
  const chartData = useMemo(() => {
    if (!balanceHistory) return []
    return balanceHistory.map(p => ({
      label: formatDateStr(p.date, locale),
      date: p.date,
      balance: p.balance,
    }))
  }, [balanceHistory, locale])

  // Running balance computation for transaction table
  const isCreditCard = account?.type === 'credit_card'
  const txWithRunningBalance = useMemo((): TxWithBalance[] => {
    if (!txData?.items || summary === undefined) return []
    // Use the last chart point as reference if available, else current_balance
    const endBalance = balanceHistory?.length
      ? balanceHistory[balanceHistory.length - 1].balance
      : summary.current_balance
    const sorted = [...txData.items].sort(
      (a, b) => new Date(b.date).getTime() - new Date(a.date).getTime()
    )
    let running = endBalance
    return sorted.map((tx) => {
      const balanceAtPoint = running
      const amt = Number(tx.amount)
      if (isCreditCard) {
        running -= tx.type === 'debit' ? amt : -amt
      } else {
        running -= tx.type === 'credit' ? amt : -amt
      }
      return { ...tx, runningBalance: balanceAtPoint }
    })
  }, [txData, summary, isCreditCard, balanceHistory])

  const hasFilters = filterFrom !== defaultFrom() || filterTo !== defaultTo()

  const isLoading = accountLoading || summaryLoading

  if (isLoading) {
    return (
      <div className="space-y-6">
        <Skeleton className="h-8 w-48" />
        <Skeleton className="h-32" />
        <Skeleton className="h-64" />
      </div>
    )
  }

  if (!account) {
    return <p className="text-muted-foreground">{t('accounts.notFound')}</p>
  }

  const currency = account.currency || userCurrency

  return (
    <div>
      {/* Header bar with date range */}
      <div className="mb-6 space-y-3">
        <div className="flex items-center gap-3 flex-wrap">
          <Button variant="ghost" size="sm" asChild className="text-muted-foreground hover:text-foreground">
            <Link to="/accounts">
              <ArrowLeft className="h-4 w-4 mr-1" />
              <span className="hidden sm:inline">{t('accounts.backToAccounts')}</span>
            </Link>
          </Button>
          <h1 className="text-xl sm:text-2xl font-bold text-foreground">{account.name}</h1>
          <span className="text-xs font-medium bg-muted text-muted-foreground px-2.5 py-1 rounded-full capitalize">
            {account.type}
          </span>
        </div>
        <div className="flex items-center gap-2 sm:gap-3 flex-wrap">
          <div className="flex items-center gap-2">
            <label className="text-sm text-muted-foreground hidden md:inline">{t('transactions.from')}</label>
            <DatePickerInput
              value={filterFrom}
              onChange={setFilterFrom}
              placeholder={t('transactions.from')}
            />
          </div>
          <div className="flex items-center gap-2">
            <label className="text-sm text-muted-foreground hidden md:inline">{t('transactions.to')}</label>
            <DatePickerInput
              value={filterTo}
              onChange={setFilterTo}
              placeholder={t('transactions.to')}
            />
          </div>
          {hasFilters && (
            <Button
              variant="ghost"
              size="sm"
              className="text-muted-foreground hover:text-foreground"
              onClick={() => { setFilterFrom(defaultFrom()); setFilterTo(defaultTo()) }}
            >
              <X className="h-3.5 w-3.5 mr-1" />
              {t('transactions.clearFilters')}
            </Button>
          )}
        </div>
      </div>

      {account.is_closed && (
        <div className="flex items-center justify-between rounded-lg border border-border bg-muted px-4 py-3 mb-6">
          <span className="text-sm text-muted-foreground">{t('accounts.closedBanner')}</span>
          <Button
            variant="outline"
            size="sm"
            onClick={() => reopenMutation.mutate()}
            disabled={reopenMutation.isPending}
          >
            {t('accounts.reopen')}
          </Button>
        </div>
      )}

      {/* Compact stat bar */}
      <div className="grid grid-cols-3 gap-2 sm:gap-4 mb-6">
        <div className="bg-card rounded-xl border border-border shadow-sm p-3 sm:p-4">
          <p className="text-[10px] sm:text-xs font-medium text-muted-foreground mb-1">
            {t('accounts.currentBalance')}
          </p>
          <p className={`text-base sm:text-2xl font-bold tabular-nums ${(account.type === 'credit_card' ? (summary?.current_balance ?? 0) > 0 : (summary?.current_balance ?? 0) < 0) ? 'text-rose-500' : 'text-foreground'}`}>
            {mask(formatCurrency(summary?.current_balance ?? 0, currency, locale))}
          </p>
        </div>
        <div className="bg-card rounded-xl border border-border shadow-sm p-3 sm:p-4">
          <p className="text-[10px] sm:text-xs font-medium text-muted-foreground mb-1">
            {t('accounts.income')}
          </p>
          <p className="text-base sm:text-2xl font-bold tabular-nums text-emerald-600">
            {mask(formatCurrency(summary?.monthly_income ?? 0, currency, locale))}
          </p>
        </div>
        <div className="bg-card rounded-xl border border-border shadow-sm p-3 sm:p-4">
          <p className="text-[10px] sm:text-xs font-medium text-muted-foreground mb-1">
            {t('accounts.expenses')}
          </p>
          <p className="text-base sm:text-2xl font-bold tabular-nums text-rose-500">
            {mask(formatCurrency(summary?.monthly_expenses ?? 0, currency, locale))}
          </p>
        </div>
      </div>

      {/* Balance chart */}
      <div className="bg-card rounded-xl border border-border shadow-sm mb-6">
        <div className="px-5 pt-5 pb-3">
          <p className="text-base font-bold text-foreground">{t('dashboard.balanceFlow')}</p>
        </div>
        <div className="px-1 pb-4 h-[280px]">
          {balanceHistoryLoading ? (
            <Skeleton className="h-full w-full" />
          ) : chartData.length > 0 ? (
            <ResponsiveContainer width="100%" height="100%">
              <AreaChart
                data={chartData}
                margin={{ top: 4, right: 8, left: 0, bottom: 0 }}
              >
                <defs>
                  <linearGradient id="acctBalGrad" x1="0" y1="0" x2="0" y2="1">
                    <stop offset="5%" stopColor="#10B981" stopOpacity={0.18} />
                    <stop offset="95%" stopColor="#10B981" stopOpacity={0.02} />
                  </linearGradient>
                </defs>
                <XAxis
                  dataKey="label"
                  tick={{ fontSize: 10, fill: 'var(--muted-foreground)' }}
                  axisLine={false}
                  tickLine={false}
                  interval="preserveStartEnd"
                  minTickGap={40}
                />
                <YAxis
                  tickFormatter={(v) => {
                    if (privacyMode) return ''
                    if (v === 0) return '0'
                    return formatCurrency(v, currency, locale).replace(/,00$/, '').replace(/\.00$/, '')
                  }}
                  tick={{ fontSize: 10, fill: 'var(--muted-foreground)' }}
                  axisLine={false}
                  tickLine={false}
                  width={56}
                  tickCount={5}
                  domain={[
                    (dataMin: number) => dataMin < 0 ? Math.floor(dataMin / 100) * 100 : 0,
                    (dataMax: number) => Math.ceil(dataMax / 100) * 100,
                  ]}
                />
                <Tooltip
                  formatter={(value) => [
                    value !== null ? (privacyMode ? MASK : formatCurrency(Number(value), currency, locale)) : '\u2014',
                    t('accounts.currentBalance'),
                  ]}
                  labelFormatter={(label) => label}
                  contentStyle={{
                    background: 'var(--card)',
                    color: 'var(--foreground)',
                    border: '1px solid var(--border)',
                    borderRadius: '0.75rem',
                    boxShadow: '0 4px 12px rgba(0,0,0,0.08)',
                    fontSize: '12px',
                  }}
                />
                <Area
                  type="monotone"
                  dataKey="balance"
                  stroke="#10B981"
                  strokeWidth={2}
                  fill="url(#acctBalGrad)"
                  dot={false}
                  activeDot={{ r: 3, fill: '#10B981' }}
                />
              </AreaChart>
            </ResponsiveContainer>
          ) : (
            <p className="text-muted-foreground text-sm text-center py-12">{t('dashboard.noData')}</p>
          )}
        </div>
      </div>

      {/* Transaction table */}
      <div className="bg-card rounded-xl border border-border shadow-sm overflow-hidden">
        <div className="px-5 py-4 border-b border-border">
          <p className="font-semibold text-foreground">{t('transactions.title')}</p>
        </div>
        <div className="p-0">
          {txLoading ? (
            <div className="p-6 space-y-3">
              {Array.from({ length: 5 }).map((_, i) => <Skeleton key={i} className="h-10" />)}
            </div>
          ) : txWithRunningBalance.length === 0 ? (
            <p className="p-6 text-center text-muted-foreground">{t('accounts.noTransactions')}</p>
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead>
                  <tr className="border-b">
                    <th className="px-3 sm:px-4 py-3 text-left font-medium">{t('transactions.date')}</th>
                    <th className="px-3 sm:px-4 py-3 text-left font-medium">{t('transactions.description')}</th>
                    <th className="px-4 py-3 text-left font-medium hidden md:table-cell">{t('transactions.category')}</th>
                    <th className="px-3 sm:px-4 py-3 text-right font-medium">{t('transactions.amount')}</th>
                    <th className="px-4 py-3 text-right font-medium hidden sm:table-cell">{t('accounts.runningBalance')}</th>
                  </tr>
                </thead>
                <tbody>
                  {txWithRunningBalance.map((tx) => {
                    const isOpening = tx.source === 'opening_balance'
                    const isTransfer = !!tx.transfer_pair_id
                    const isPending = tx.status === 'pending'
                    return (
                      <tr
                        key={tx.id}
                        className={`border-b last:border-0 transition-colors ${isOpening ? 'bg-muted/60' : isPending ? 'opacity-60' : 'hover:bg-muted cursor-pointer'}`}
                        onClick={() => {
                          if (!isOpening) {
                            setEditingTx(tx)
                            setDialogOpen(true)
                          }
                        }}
                      >
                        <td className="px-3 sm:px-4 py-3 text-xs text-muted-foreground whitespace-nowrap">
                          {formatDateStr(tx.date, locale)}
                        </td>
                        <td className="px-3 sm:px-4 py-3">
                          <div>
                            <span className="font-semibold text-foreground text-sm">{tx.description}</span>
                            {isOpening && (
                              <span className="ml-2 text-xs text-muted-foreground font-normal border border-border rounded px-1.5 py-0.5">
                                {t('accounts.openingBalance')}
                              </span>
                            )}
                            {isTransfer && (
                              <span className="ml-2 inline-flex items-center gap-1 text-xs text-blue-600 font-normal bg-blue-50 border border-blue-200 rounded px-1.5 py-0.5">
                                <ArrowLeftRight className="h-3 w-3" />
                                {t('transactions.transfer')}
                              </span>
                            )}
                            {isPending && (
                              <span className="ml-2 inline-flex items-center gap-1 text-xs text-amber-600 font-normal bg-amber-50 border border-amber-200 rounded px-1.5 py-0.5">
                                <Clock className="h-3 w-3" />
                                {t('transactions.pending')}
                              </span>
                            )}
                            {(tx.attachment_count ?? 0) > 0 && (
                              <Paperclip size={12} className="ml-2 inline text-muted-foreground" />
                            )}
                          </div>
                          {tx.payee && tx.payee !== tx.description && (
                            <p className="text-xs text-muted-foreground mt-0.5">{tx.payee}</p>
                          )}
                        </td>
                        <td className="px-4 py-3 hidden md:table-cell">
                          {tx.category ? (
                            <span className="flex items-center gap-1.5">
                              <CategoryIcon icon={tx.category.icon} color={tx.category.color} size="sm" />
                              <span className="text-sm text-muted-foreground">{tx.category.name}</span>
                            </span>
                          ) : (
                            <span className="text-muted-foreground">—</span>
                          )}
                        </td>
                        <td className={`px-3 sm:px-4 py-3 text-right text-xs sm:text-sm font-semibold tabular-nums ${tx.type === 'credit' ? 'text-emerald-600' : 'text-rose-500'}`}>
                          {mask(`${tx.type === 'credit' ? '+' : '-'}${formatCurrency(Math.abs(Number(tx.amount)), currency, locale)}`)}
                        </td>
                        <td className={`px-4 py-3 text-right tabular-nums text-sm hidden sm:table-cell ${(account.type === 'credit_card' ? tx.runningBalance > 0 : tx.runningBalance < 0) ? 'text-rose-500' : 'text-muted-foreground'}`}>
                          {mask(formatCurrency(tx.runningBalance, currency, locale))}
                        </td>
                      </tr>
                    )
                  })}
                </tbody>
              </table>
            </div>
          )}
        </div>
      </div>

      <TransactionDialog
        open={dialogOpen}
        onClose={() => { setDialogOpen(false); setEditingTx(null) }}
        transaction={editingTx}
        categories={categoriesList ?? []}
        accounts={[]}
        onSave={(data) => {
          if (editingTx) {
            updateMutation.mutate({ id: editingTx.id, ...data })
          }
        }}
        onDelete={editingTx ? () => deleteMutation.mutate(editingTx.id) : undefined}
        loading={updateMutation.isPending || deleteMutation.isPending}
        error={updateMutation.error ? extractApiError(updateMutation.error) : null}
        isSynced={editingTx?.source === 'sync'}
      />
    </div>
  )
}
