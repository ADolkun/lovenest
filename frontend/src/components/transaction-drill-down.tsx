import { useEffect, useRef, useMemo } from 'react'
import { useTranslation } from 'react-i18next'
import { useQuery } from '@tanstack/react-query'
import { transactions as transactionsApi, dashboard } from '@/lib/api'
import { AlertTriangle, Paperclip, X } from 'lucide-react'
import { CategoryIcon } from '@/components/category-icon'
import { useAuth } from '@/contexts/auth-context'
import { usePrivacyMode } from '@/hooks/use-privacy-mode'
import type { Transaction } from '@/types'

export type DrillDownFilter = {
  title: string
  category_id?: string
  uncategorized?: boolean
  account_id?: string
  type?: 'credit' | 'debit'
  from?: string
  to?: string
}

type DisplayItem = {
  key: string
  description: string
  date: string
  type: 'debit' | 'credit'
  amount: number
  amountPrimary: number | null
  currency: string
  categoryIcon: string | null
  categoryName: string | null
  categoryColor: string | null
  isProjected: boolean
  attachmentCount: number
  transaction: Transaction | null
}

function formatCurrency(value: number, currency = 'USD', locale = 'en-US') {
  return new Intl.NumberFormat(locale, { style: 'currency', currency }).format(value)
}

export function TransactionDrillDown({
  filter,
  onClose,
  onTransactionClick,
}: {
  filter: DrillDownFilter | null
  onClose: () => void
  onTransactionClick?: (tx: Transaction) => void
}) {
  const { t, i18n } = useTranslation()
  const { user } = useAuth()
  const { mask } = usePrivacyMode()
  const userCurrency = user?.preferences?.currency_display ?? 'USD'
  const locale = i18n.language === 'en' ? 'en-US' : i18n.language
  const panelRef = useRef<HTMLDivElement>(null)

  const { data, isLoading } = useQuery({
    queryKey: ['drill-down', filter],
    queryFn: () =>
      transactionsApi.list({
        category_id: filter?.category_id,
        uncategorized: filter?.uncategorized,
        account_id: filter?.account_id,
        type: filter?.type,
        from: filter?.from,
        to: filter?.to,
        limit: 200,
      }),
    enabled: !!filter,
  })

  // Derive month param from filter.from for projected transactions
  const monthParam = filter?.from ? filter.from.slice(0, 7) + '-01' : undefined

  const { data: projectedTxs } = useQuery({
    queryKey: ['dashboard', 'projected-transactions', monthParam],
    queryFn: () => dashboard.projectedTransactions(monthParam),
    enabled: !!filter && !!monthParam,
  })

  // Merge real + projected transactions, filtering projected by drill-down criteria
  const displayItems = useMemo((): DisplayItem[] => {
    const items: DisplayItem[] = []

    for (const tx of data?.items ?? []) {
      items.push({
        key: tx.id,
        description: tx.description,
        date: tx.date,
        type: tx.type as 'debit' | 'credit',
        amount: Number(tx.amount),
        amountPrimary: tx.amount_primary != null ? Number(tx.amount_primary) : null,
        currency: tx.currency,
        categoryIcon: tx.category?.icon ?? null,
        categoryName: tx.category?.name ?? null,
        categoryColor: tx.category?.color ?? null,
        isProjected: false,
        attachmentCount: tx.attachment_count ?? 0,
        transaction: tx,
      })
    }

    for (const pt of projectedTxs ?? []) {
      // Filter projected txs by drill-down criteria
      if (filter?.type && pt.type !== filter.type) continue
      if (filter?.category_id && String(pt.category_id) !== filter.category_id) continue
      if (filter?.uncategorized && pt.category_id != null) continue
      if (filter?.from && pt.date < filter.from) continue
      if (filter?.to && pt.date > filter.to) continue

      items.push({
        key: `proj-${pt.recurring_id}-${pt.date}`,
        description: pt.description,
        date: pt.date,
        type: pt.type,
        amount: pt.amount,
        amountPrimary: pt.amount_primary ?? null,
        currency: pt.currency,
        categoryIcon: pt.category_icon,
        categoryName: pt.category_name,
        categoryColor: pt.category_color ?? null,
        isProjected: true,
        attachmentCount: 0,
        transaction: null,
      })
    }

    items.sort((a, b) => a.date.localeCompare(b.date))
    return items
  }, [data, projectedTxs, filter])

  // Close on Escape
  useEffect(() => {
    if (!filter) return
    const handleKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose()
    }
    document.addEventListener('keydown', handleKey)
    return () => document.removeEventListener('keydown', handleKey)
  }, [filter, onClose])

  // Close on click outside
  useEffect(() => {
    if (!filter) return
    const handleClick = (e: MouseEvent) => {
      if (panelRef.current && !panelRef.current.contains(e.target as Node)) {
        onClose()
      }
    }
    // Delay to avoid closing immediately from the click that opened it
    const timer = setTimeout(() => {
      document.addEventListener('mousedown', handleClick)
    }, 100)
    return () => {
      clearTimeout(timer)
      document.removeEventListener('mousedown', handleClick)
    }
  }, [filter, onClose])

  const absTotal = displayItems.reduce((sum, item) => {
    // Use amount_primary for foreign currency transactions, fall back to amount
    const effectiveAmount = (item.currency !== userCurrency && item.amountPrimary != null)
      ? item.amountPrimary
      : item.amount
    return sum + Math.abs(effectiveAmount)
  }, 0)

  return (
    <>
      {/* Backdrop */}
      <div
        className={`fixed inset-0 bg-black/20 z-40 transition-opacity duration-200 ${
          filter ? 'opacity-100' : 'opacity-0 pointer-events-none'
        }`}
      />

      {/* Panel */}
      <div
        ref={panelRef}
        className={`fixed top-0 right-0 h-full w-full max-w-md bg-card shadow-2xl z-50 transform transition-transform duration-200 ease-out flex flex-col ${
          filter ? 'translate-x-0' : 'translate-x-full'
        }`}
      >
        {/* Header */}
        <div className="flex items-center justify-between px-5 py-4 border-b border-border shrink-0">
          <h2 className="text-sm font-semibold text-foreground truncate pr-4">
            {filter?.title}
          </h2>
          <button
            onClick={onClose}
            className="w-8 h-8 flex items-center justify-center rounded-lg hover:bg-muted text-muted-foreground hover:text-foreground transition-colors shrink-0"
          >
            <X size={16} />
          </button>
        </div>

        {/* Transaction list */}
        <div className="flex-1 overflow-auto">
          {isLoading ? (
            <div className="p-5 space-y-3">
              {Array.from({ length: 8 }).map((_, i) => (
                <div key={i} className="h-12 bg-muted rounded-lg animate-pulse" />
              ))}
            </div>
          ) : displayItems.length === 0 ? (
            <p className="text-muted-foreground text-sm text-center py-12">
              {t('dashboard.drillDownEmpty')}
            </p>
          ) : (
            <div className="divide-y divide-border">
              {displayItems.map((item) => (
                <div
                  key={item.key}
                  className={`flex items-center gap-3 px-5 py-3 hover:bg-muted transition-colors ${!item.isProjected ? 'cursor-pointer' : ''}`}
                  onClick={() => {
                    if (!item.isProjected && item.transaction) {
                      onTransactionClick?.(item.transaction)
                    }
                  }}
                >
                  <CategoryIcon
                    icon={item.categoryIcon}
                    color={item.categoryColor}
                    size="lg"
                  />
                  <div className="flex-1 min-w-0">
                    <div className="flex items-center gap-2">
                      <p className="text-sm font-medium text-foreground truncate">{item.description}</p>
                      {item.isProjected && (
                        <span className="inline-flex items-center px-1.5 py-0.5 rounded text-[10px] font-semibold bg-violet-100 text-violet-600 shrink-0">
                          {t('transactions.recurringBadge')}
                        </span>
                      )}
                      {item.attachmentCount > 0 && (
                        <Paperclip size={12} className="text-muted-foreground shrink-0" />
                      )}
                    </div>
                    <p className="text-xs text-muted-foreground">
                      {new Date(item.date + 'T00:00:00').toLocaleDateString(locale)}
                      {item.categoryName && ` · ${item.categoryName}`}
                    </p>
                  </div>
                  <div className="text-right shrink-0">
                    <span
                      className={`text-sm font-semibold tabular-nums ${
                        item.type === 'credit' ? 'text-emerald-600' : 'text-rose-500'
                      }`}
                    >
                      {item.type === 'credit' ? '+' : '-'}
                      {mask(formatCurrency(Math.abs(item.amount), item.currency ?? userCurrency, locale))}
                    </span>
                    {item.currency !== userCurrency && item.amountPrimary != null && (
                      <div className="flex items-center justify-end gap-1">
                        {item.transaction?.fx_fallback && (
                          <span title={t('transactions.fxFallbackTooltip')}><AlertTriangle size={11} className="text-amber-500 shrink-0" /></span>
                        )}
                        <span className="text-[10px] text-muted-foreground tabular-nums">
                          {mask(formatCurrency(Math.abs(item.amountPrimary), userCurrency, locale))}
                        </span>
                      </div>
                    )}
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>

        {/* Footer */}
        {displayItems.length > 0 && (
          <div className="px-5 py-3 border-t border-border bg-muted/50 shrink-0">
            <div className="flex items-center justify-between">
              <span className="text-xs text-muted-foreground">
                {t('dashboard.drillDownTotal', {
                  count: displayItems.length,
                  total: mask(formatCurrency(absTotal, userCurrency, locale)),
                })}
              </span>
              <span className="text-sm font-bold tabular-nums text-foreground">
                {mask(formatCurrency(absTotal, userCurrency, locale))}
              </span>
            </div>
          </div>
        )}
      </div>
    </>
  )
}
