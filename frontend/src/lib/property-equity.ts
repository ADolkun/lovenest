import type { Account, Asset } from '@/types'
import { getAssetValuePrimary } from '@/lib/asset-portfolio-share'

/** Asset types a loan can be secured against (a mortgage, a car financing). */
export const LOAN_COLLATERAL_TYPES: readonly string[] = ['real_estate', 'vehicle']

type ValuedProperty = Pick<Asset, 'id' | 'currency' | 'current_value' | 'current_value_primary'>
type SecuredLoan = Pick<Account, 'type' | 'secured_asset_id' | 'currency' | 'current_balance' | 'balance_primary'>

/**
 * Loan debt in the display currency. An overpaid loan owes nothing. A foreign
 * loan with no FX rate owes an unknown amount here, not its native figure.
 */
export function getLoanDebtPrimary(
  loan: Pick<Account, 'currency' | 'current_balance' | 'balance_primary'>,
  displayCurrency: string,
): number | null {
  const balance = loan.balance_primary ?? (loan.currency === displayCurrency ? loan.current_balance : null)
  return balance == null ? null : Math.max(0, -Number(balance))
}

/** Value, linked debt and equity of a property, in the user's primary currency. */
export function getPropertyEquity(
  property: ValuedProperty,
  accounts: SecuredLoan[],
  displayCurrency: string,
): { value: number; debt: number; equity: number } | null {
  const value = getAssetValuePrimary(property, displayCurrency)
  if (value == null) return null
  const debts = accounts
    .filter(account => account.type === 'loan' && account.secured_asset_id === property.id)
    .map(loan => getLoanDebtPrimary(loan, displayCurrency))
  if (debts.some(debt => debt == null)) return null
  const debt = debts.reduce<number>((total, loanDebt) => total + (loanDebt ?? 0), 0)
  return { value, debt, equity: value - debt }
}
