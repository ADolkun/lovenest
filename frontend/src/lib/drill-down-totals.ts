/**
 * A drill-down row, reduced to what the footer totals need. Kept structural
 * rather than importing the panel's DisplayItem so the arithmetic can be
 * tested without standing up the panel.
 */
export type DrillDownTotalsItem = {
  type: 'debit' | 'credit'
  amount: number
  /** Reimbursement links on the row, in its own currency. */
  linkedAmount: number
  amountPrimary: number | null
  currency: string
  isPending: boolean
  isProjected: boolean
}

export type DrillDownTotals = {
  total: number
  postedTotal: number
  pendingTotal: number
  projectedTotal: number
}

/**
 * Split the rows the panel is showing into the buckets the footer reports.
 *
 * Every row lands in exactly one of posted, pending or projected, so the
 * three always add back up to `total`. That invariant is the point: the
 * footer labels its bottom line "total shown", and a row the user can see in
 * the list has to be inside it. Bucketing on the row's own `isPending` and
 * `isProjected` flags, rather than on a persisted status, is what keeps
 * projections from falling through the gaps: they have no transaction behind
 * them, so a status test can never place them.
 *
 * With `netOf`, rows of the other type count against the total instead of
 * toward it. An expenses panel lists refunds and reimbursements beside the
 * debits they offset, and its total has to come out net, as the dashboard
 * figure it opened from does. Without it every row adds its magnitude.
 *
 * A row with reimbursement links counts only its unlinked share: an expense
 * net of what repaid it, a credit net of what it repaid. The share scales
 * `amountPrimary` too, so the figure stays the one the dashboard computed.
 *
 * Foreign-currency rows are converted through `amountPrimary`; when that is
 * missing the row is skipped rather than added as if its raw amount were
 * already in the user's currency. This matches how `get_summary` computes
 * `monthly_*_primary` on the backend.
 */
export function sumDrillDownTotals(
  items: DrillDownTotalsItem[],
  userCurrency: string,
  netOf?: 'debit' | 'credit',
): DrillDownTotals {
  return items.reduce<DrillDownTotals>(
    (totals, item) => {
      const unlinkedShare = 1 - linkedFraction(item.amount, item.linkedAmount)
      const magnitude = unlinkedShare * (item.currency === userCurrency
        ? Math.abs(item.amount)
        : item.amountPrimary != null
          ? Math.abs(item.amountPrimary)
          : 0)
      const amount = netOf && item.type !== netOf ? -magnitude : magnitude

      totals.total += amount
      if (item.isProjected) totals.projectedTotal += amount
      else if (item.isPending) totals.pendingTotal += amount
      else totals.postedTotal += amount
      return totals
    },
    { total: 0, postedTotal: 0, pendingTotal: 0, projectedTotal: 0 },
  )
}

export function linkedFraction(amount: number, linkedAmount: number): number {
  const magnitude = Math.abs(amount)
  return magnitude > 0 ? Math.min(1, linkedAmount / magnitude) : 0
}
