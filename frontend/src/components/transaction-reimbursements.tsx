import { useState } from 'react'
import { useTranslation } from 'react-i18next'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { HandCoins, Link2, Unlink } from 'lucide-react'
import { toast } from 'sonner'

import { Button } from '@/components/ui/button'
import { LinkReimbursementDialog } from '@/components/link-reimbursement-dialog'
import { transactions as transactionsApi } from '@/lib/api'
import { extractApiError } from '@/lib/api-errors'
import { formatCurrency } from '@/lib/format'
import { invalidateFinancialQueries } from '@/lib/invalidate-queries'
import { linkedTotal, roundCents } from '@/lib/reimbursements'
import { useDateLocale, useDisplayLocale } from '@/hooks/use-display-locale'
import { usePrivacyMode } from '@/hooks/use-privacy-mode'
import type { Transaction } from '@/types'

/**
 * A credit lists the expenses it repays and can link more; a debit lists the
 * credits that repaid it, and only once there is one. Loads and saves on its
 * own, outside the transaction form's payload.
 */
export function TransactionReimbursements({ transaction }: { transaction: Transaction }) {
  const { t } = useTranslation()
  const queryClient = useQueryClient()
  const locale = useDisplayLocale()
  const dateLocale = useDateLocale()
  const { mask } = usePrivacyMode()
  const [linkOpen, setLinkOpen] = useState(false)
  const isCredit = transaction.type === 'credit'

  const { data } = useQuery({
    queryKey: ['transactions', transaction.id, 'reimbursements'],
    queryFn: () => transactionsApi.reimbursements.list(transaction.id),
  })
  const links = data ?? transaction.reimbursement_links

  const unlinkMutation = useMutation({
    mutationFn: (linkId: string) => transactionsApi.reimbursements.unlink(transaction.id, linkId),
    onSuccess: () => {
      invalidateFinancialQueries(queryClient)
      toast.success(t('transactions.reimbursementUnlinked'))
    },
    onError: (error) => {
      toast.error(extractApiError(error, t('common.error')))
    },
  })

  if (!isCredit && links.length === 0) return null

  const balance = roundCents(Math.abs(Number(transaction.amount)) - linkedTotal(links))

  return (
    <div className="space-y-3" data-testid="transaction-reimbursements">
      <div className="flex items-center justify-between gap-2">
        <div className="flex items-center gap-2 text-sm font-medium">
          <HandCoins size={14} />
          {isCredit ? t('transactions.reimburses') : t('transactions.reimbursedBy')}
        </div>
        {isCredit && (
          <Button type="button" variant="outline" size="xs" onClick={() => setLinkOpen(true)}>
            <Link2 />
            {t('transactions.linkExpenses')}
          </Button>
        )}
      </div>

      {links.length === 0 ? (
        <p className="text-xs text-muted-foreground">{t('transactions.reimbursesEmpty')}</p>
      ) : (
        <>
          <ul className="divide-y divide-border rounded-lg border border-border">
            {links.map((link) => (
              <li key={link.id} className="flex items-center gap-3 px-3 py-2">
                <div className="min-w-0 flex-1">
                  <p className="truncate text-sm" title={link.counterpart_description}>
                    {link.counterpart_description}
                  </p>
                  <p className="text-xs text-muted-foreground">
                    {new Date(link.counterpart_date + 'T00:00:00').toLocaleDateString(dateLocale)}
                    {link.counterpart_category_name && ` · ${link.counterpart_category_name}`}
                  </p>
                </div>
                <span className="shrink-0 text-sm tabular-nums">
                  {mask(formatCurrency(Number(link.amount), link.currency, locale))}
                </span>
                <Button
                  type="button"
                  variant="ghost"
                  size="icon-xs"
                  onClick={() => unlinkMutation.mutate(link.id)}
                  disabled={unlinkMutation.isPending}
                  aria-label={t('transactions.unlinkReimbursement', { description: link.counterpart_description })}
                  title={t('transactions.unlinkReimbursement', { description: link.counterpart_description })}
                >
                  <Unlink />
                </Button>
              </li>
            ))}
          </ul>
          <div className="flex items-center justify-between text-xs text-muted-foreground">
            <span>{isCredit ? t('transactions.reimbursementUnallocated') : t('transactions.reimbursementNetCost')}</span>
            <span className="tabular-nums text-foreground" data-testid="reimbursement-balance">
              {mask(formatCurrency(balance, transaction.currency, locale))}
            </span>
          </div>
        </>
      )}

      {isCredit && (
        <LinkReimbursementDialog
          open={linkOpen}
          onOpenChange={setLinkOpen}
          credit={transaction}
          links={links}
        />
      )}
    </div>
  )
}
