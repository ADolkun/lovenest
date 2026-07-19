type AmountSortableTransaction = {
  amount: number
  amountPrimary: number | null
  orderDate: string
}

export function compareTransactionAmountsDesc(
  a: AmountSortableTransaction,
  b: AmountSortableTransaction,
) {
  const amountDiff = Math.abs(b.amountPrimary ?? b.amount) - Math.abs(a.amountPrimary ?? a.amount)
  return amountDiff || b.orderDate.localeCompare(a.orderDate)
}
