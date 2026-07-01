export type TransactionOrderDateInput = {
  date: string
  effective_date?: string | null
  effective_bill_date?: string | null
}

export function transactionOrderDate(tx: TransactionOrderDateInput, isAccrual: boolean) {
  return tx.effective_bill_date ?? (isAccrual ? tx.effective_date ?? tx.date : tx.date)
}
