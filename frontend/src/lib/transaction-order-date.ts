export type TransactionOrderDateInput = {
  date: string
  effective_date?: string | null
  effective_bill_date?: string | null
  reporting_date_override?: string | null
}

// Mirrors `reporting_date_col` in backend/app/services/_query_filters.py, the
// date the server buckets and sorts transactions by.
export function transactionOrderDate(tx: TransactionOrderDateInput, isAccrual: boolean) {
  return tx.reporting_date_override
    ?? tx.effective_bill_date
    ?? (isAccrual ? tx.effective_date ?? tx.date : tx.date)
}
