import type { Transaction } from '@/types'

/**
 * Whether a transaction reads as a transfer in row-level indicators.
 *
 * Row indicators must match the totals, so a row the dashboard, the reports
 * and the list summary keep out of income and expense reads as a transfer on
 * every screen that lists it (issue #993), and a row they count never does.
 * The server's `is_transfer` is that rule. A pair id or a category alone
 * cannot tell: a pair whose other leg is gone still counts, and so does a
 * card refund filed under a transfer category.
 *
 * Without the flag (rows an endpoint did not classify, projected recurring
 * rows) the row is a transfer when it is part of a linked pair or its
 * category is flagged `treat_as_transfer`.
 */
export function isTransferTransaction(
  tx: Pick<Transaction, 'transfer_pair_id' | 'category' | 'is_transfer'>,
): boolean {
  if (tx.is_transfer != null) return tx.is_transfer
  return !!tx.transfer_pair_id || !!tx.category?.treat_as_transfer
}
