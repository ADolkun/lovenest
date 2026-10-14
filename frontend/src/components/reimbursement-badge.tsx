import { useTranslation } from 'react-i18next'
import { HandCoins } from 'lucide-react'

import { formatCurrency } from '@/lib/format'
import { linkedTotal } from '@/lib/reimbursements'
import { useDisplayLocale } from '@/hooks/use-display-locale'
import { usePrivacyMode } from '@/hooks/use-privacy-mode'
import type { Transaction } from '@/types'

export function ReimbursementBadge({ transaction }: { transaction: Transaction }) {
  const { t } = useTranslation()
  const locale = useDisplayLocale()
  const { mask } = usePrivacyMode()
  const links = transaction.reimbursement_links
  if (links.length === 0) return null

  const isCredit = transaction.type === 'credit'
  return (
    <span
      className="inline-flex items-center gap-1 text-[10px] font-semibold uppercase tracking-wide text-teal-700 bg-teal-50 border border-teal-200 dark:bg-teal-950/40 dark:text-teal-300 dark:border-teal-900 px-1.5 py-0.5 rounded-full"
      title={isCredit ? t('transactions.appliedToExpenses', { count: links.length }) : undefined}
    >
      <HandCoins className="h-3 w-3" />
      {isCredit
        ? t('transactions.reimbursementBadge')
        : t('transactions.reimbursedAmount', {
            amount: mask(formatCurrency(linkedTotal(links), transaction.currency, locale)),
          })}
    </span>
  )
}
