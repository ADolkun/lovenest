import { useEffect, useMemo, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Search } from 'lucide-react'
import { toast } from 'sonner'

import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Skeleton } from '@/components/ui/skeleton'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { transactions as transactionsApi } from '@/lib/api'
import { extractApiError } from '@/lib/api-errors'
import { formatAmountInput, formatCurrency, parseAmountInput } from '@/lib/format'
import { invalidateFinancialQueries } from '@/lib/invalidate-queries'
import { roundCents } from '@/lib/reimbursements'
import { cn } from '@/lib/utils'
import { useDateLocale, useDisplayLocale } from '@/hooks/use-display-locale'
import { usePrivacyMode } from '@/hooks/use-privacy-mode'
import type { ReimbursementCandidate, ReimbursementLink, Transaction } from '@/types'

const CANDIDATE_LIMIT = 50
const CENT = 0.005

type ExpenseRow = {
  id: string
  description: string
  date: string
  categoryName: string | null
  /** Unknown for a linked expense the candidate search has not returned. */
  amount: number | null
  currency: string
  /** The most this credit may put on the expense; null when unknown. */
  capacity: number | null
}

type Pick = { row: ExpenseRow; input: string }

/** A prefilled amount always shows its cents, so 10.6 reads as 10.60. */
function centsInput(value: number, locale: string): string {
  return formatAmountInput(value, locale, 2, 2)
}

function candidateRow(candidate: ReimbursementCandidate): ExpenseRow {
  const tx = candidate.transaction
  return {
    id: tx.id,
    description: tx.description,
    date: tx.date,
    categoryName: tx.category?.name ?? null,
    amount: Math.abs(Number(tx.amount)),
    currency: tx.currency,
    // The server leaves this credit's own link out of `remaining`, so the
    // whole of it is this credit's to allocate.
    capacity: roundCents(Number(candidate.remaining)),
  }
}

function linkRow(link: ReimbursementLink): ExpenseRow {
  return {
    id: link.counterpart_id,
    description: link.counterpart_description,
    date: link.counterpart_date,
    categoryName: link.counterpart_category_name,
    amount: null,
    currency: link.currency,
    capacity: null,
  }
}

export function LinkReimbursementDialog({
  open,
  onOpenChange,
  credit,
  links,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
  credit: Transaction
  /** The credit's current links. The save replaces the whole set, so these
   *  seed the selection even when the search does not return them. */
  links: ReimbursementLink[]
}) {
  const { t } = useTranslation()
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-2xl flex flex-col">
        <DialogHeader>
          <DialogTitle>{t('transactions.linkExpenses')}</DialogTitle>
          <DialogDescription>{t('transactions.linkExpensesDescription')}</DialogDescription>
        </DialogHeader>
        <LinkReimbursementBody credit={credit} links={links} onClose={() => onOpenChange(false)} />
      </DialogContent>
    </Dialog>
  )
}

function LinkReimbursementBody({
  credit,
  links,
  onClose,
}: {
  credit: Transaction
  links: ReimbursementLink[]
  onClose: () => void
}) {
  const { t } = useTranslation()
  const queryClient = useQueryClient()
  const locale = useDisplayLocale()
  const dateLocale = useDateLocale()
  const { mask } = usePrivacyMode()
  const creditAmount = Math.abs(Number(credit.amount))
  const money = (value: number, currency = credit.currency) =>
    mask(formatCurrency(value, currency, locale))

  const [search, setSearch] = useState('')
  const [query, setQuery] = useState('')
  useEffect(() => {
    const id = setTimeout(() => setQuery(search.trim()), 300)
    return () => clearTimeout(id)
  }, [search])

  const [picks, setPicks] = useState<Map<string, Pick>>(() => new Map(
    links.map((link) => [
      link.counterpart_id,
      { row: linkRow(link), input: centsInput(Number(link.amount), locale) },
    ]),
  ))

  const { data: candidates, isLoading } = useQuery({
    queryKey: ['transactions', credit.id, 'reimbursement-candidates', query],
    queryFn: () => transactionsApi.reimbursements.candidates(credit.id, {
      q: query || undefined,
      limit: CANDIDATE_LIMIT,
    }),
  })

  const candidateRows = useMemo(() => (candidates ?? []).map(candidateRow), [candidates])
  const candidateById = useMemo(
    () => new Map(candidateRows.map((row) => [row.id, row])),
    [candidateRows],
  )
  // Checked expenses the current search did not return stay listed on top,
  // so a search never hides part of what the save will send.
  const rows = [
    ...[...picks.values()]
      .filter((pick) => !candidateById.has(pick.row.id))
      .map((pick) => pick.row),
    ...candidateRows,
  ]

  const parsed = (pick: Pick) => parseAmountInput(pick.input, locale)
  const capacityOf = (id: string) =>
    candidateById.get(id)?.capacity ?? picks.get(id)?.row.capacity ?? null
  const allocatedIn = (selection: Map<string, Pick>) =>
    roundCents([...selection.values()].reduce((sum, pick) => sum + (parsed(pick) ?? 0), 0))

  const allocated = allocatedIn(picks)
  const unallocated = roundCents(creditAmount - allocated)
  const invalidIds = new Set(
    [...picks.values()]
      .filter((pick) => {
        const value = parsed(pick)
        return value == null || value <= 0
      })
      .map((pick) => pick.row.id),
  )
  const overRowIds = new Set(
    [...picks.values()]
      .filter((pick) => {
        const value = parsed(pick)
        const capacity = capacityOf(pick.row.id)
        return value != null && capacity != null && value > capacity + CENT
      })
      .map((pick) => pick.row.id),
  )
  const problem = invalidIds.size > 0
    ? t('transactions.reimbursementAmountRequired')
    : overRowIds.size > 0
      ? t('transactions.reimbursementOverExpense')
      : unallocated < -CENT
        ? t('transactions.reimbursementOverCredit', { amount: money(-unallocated) })
        : null

  const toggle = (row: ExpenseRow) => {
    setPicks((prev) => {
      const next = new Map(prev)
      if (next.has(row.id)) {
        next.delete(row.id)
        return next
      }
      const left = roundCents(creditAmount - allocatedIn(prev))
      const amount = Math.max(0, Math.min(row.capacity ?? left, left))
      next.set(row.id, { row, input: amount > 0 ? centsInput(amount, locale) : '' })
      return next
    })
  }

  const setInput = (id: string, input: string) => {
    setPicks((prev) => {
      const pick = prev.get(id)
      if (!pick) return prev
      return new Map(prev).set(id, { ...pick, input })
    })
  }

  const mutation = useMutation({
    mutationFn: () => transactionsApi.reimbursements.replace(
      credit.id,
      [...picks.values()].map((pick) => ({
        expense_id: pick.row.id,
        amount: (parsed(pick) ?? 0).toFixed(2),
      })),
    ),
    onSuccess: () => {
      invalidateFinancialQueries(queryClient)
      toast.success(t('transactions.reimbursementsSaved'))
      onClose()
    },
    onError: (error) => {
      toast.error(extractApiError(error, t('common.error')))
    },
  })

  return (
    <>
      <div className="flex items-center justify-between gap-3 rounded-lg border border-border bg-muted/30 px-3 py-2">
        <div className="min-w-0">
          <p className="truncate text-sm font-medium" title={credit.description}>{credit.description}</p>
          <p className="text-xs text-muted-foreground tabular-nums">{money(creditAmount)}</p>
        </div>
        <div className="text-right shrink-0">
          <p className="text-xs text-muted-foreground">{t('transactions.reimbursementLeftToAllocate')}</p>
          <p
            className={cn('text-sm font-semibold tabular-nums', unallocated < -CENT && 'text-destructive')}
            data-testid="reimbursement-unallocated"
          >
            {money(unallocated)}
          </p>
        </div>
      </div>

      <div className="relative">
        <Search className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
        <Input
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          placeholder={t('transactions.reimbursementSearchPlaceholder')}
          aria-label={t('transactions.reimbursementSearchPlaceholder')}
          className="pl-9"
        />
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto rounded-lg border border-border">
        {isLoading && rows.length === 0 ? (
          <div className="space-y-2 p-3">
            <Skeleton className="h-10 w-full" />
            <Skeleton className="h-10 w-full" />
            <Skeleton className="h-10 w-full" />
          </div>
        ) : rows.length === 0 ? (
          <p className="px-3 py-6 text-center text-sm text-muted-foreground">
            {t('transactions.reimbursementNoCandidates')}
          </p>
        ) : (
          <ul className="divide-y divide-border">
            {rows.map((row) => {
              const pick = picks.get(row.id)
              const capacity = capacityOf(row.id)
              const value = pick ? parsed(pick) ?? 0 : 0
              const flagged = invalidIds.has(row.id) || overRowIds.has(row.id)
              return (
                <li
                  key={row.id}
                  className={cn('flex items-center gap-3 px-3 py-2', pick && 'bg-primary/5')}
                  data-testid="reimbursement-candidate"
                >
                  <input
                    type="checkbox"
                    className="h-4 w-4 shrink-0 rounded border-border accent-primary"
                    checked={!!pick}
                    onChange={() => toggle(row)}
                    aria-label={row.description}
                  />
                  <div className="min-w-0 flex-1">
                    <p className="truncate text-sm" title={row.description}>{row.description}</p>
                    <p className="text-xs text-muted-foreground tabular-nums">
                      {new Date(row.date + 'T00:00:00').toLocaleDateString(dateLocale)}
                      {row.categoryName && ` · ${row.categoryName}`}
                      {row.amount != null && ` · ${money(row.amount, row.currency)}`}
                    </p>
                    {capacity != null && (
                      <p className="text-[11px] text-muted-foreground tabular-nums">
                        {t('transactions.reimbursementExpenseLeft', {
                          amount: money(Math.max(0, roundCents(capacity - value)), row.currency),
                        })}
                      </p>
                    )}
                  </div>
                  <Input
                    className="h-8 w-28 shrink-0 text-right tabular-nums"
                    inputMode="decimal"
                    value={pick?.input ?? ''}
                    disabled={!pick}
                    onChange={(e) => setInput(row.id, e.target.value)}
                    aria-label={t('transactions.reimbursementAmountFor', { description: row.description })}
                    aria-invalid={flagged || undefined}
                  />
                </li>
              )
            })}
          </ul>
        )}
      </div>

      {problem && (
        <p className="text-xs text-destructive" role="alert">{problem}</p>
      )}

      <DialogFooter>
        <Button type="button" variant="outline" onClick={onClose}>
          {t('common.cancel')}
        </Button>
        <Button
          type="button"
          onClick={() => mutation.mutate()}
          disabled={!!problem || mutation.isPending}
        >
          {mutation.isPending ? t('common.saving') : t('common.save')}
        </Button>
      </DialogFooter>
    </>
  )
}
