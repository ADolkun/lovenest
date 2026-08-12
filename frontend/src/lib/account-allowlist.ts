import type { BankConnection, ProviderAccount } from '../types'

/** Whether a just-connected connection is waiting on its first account review.
 *
 * A review-first connect stores an empty allowlist, so it imported nothing and
 * every provider account is pending. Only ever asked of a connection the user
 * just created — an existing one deliberately set to "sync nothing" reads the
 * same, and re-opening its picker would be a nag, not a step in the flow.
 */
export function needsAccountReview(connection: BankConnection): boolean {
  return connection.settings?.account_allowlist?.length === 0
}

const REVIEW_ON_CONNECT_KEY = 'securo:reviewAccountsOnConnect'

/** Remember that the connect attempt now leaving for the bank asked to review
 * accounts first.
 *
 * A reauth comes back to the same callback route as a first connect, and the
 * connection it returns cannot tell them apart: one deliberately set to "sync
 * nothing" reads exactly like a review-first connect. Reconnecting must never
 * re-prompt for the allowlist (issue #53), so the intent travels with the
 * attempt rather than being inferred on the way back.
 */
export function markReviewOnConnect(): void {
  sessionStorage.setItem(REVIEW_ON_CONNECT_KEY, '1')
}

/** Read the flag and clear it, so a connect the user abandoned at the bank
 * cannot leak into a later reauth. */
export function takeReviewOnConnect(): boolean {
  const marked = sessionStorage.getItem(REVIEW_ON_CONNECT_KEY)
  sessionStorage.removeItem(REVIEW_ON_CONNECT_KEY)
  return marked === '1'
}

/** The checkbox state a freshly opened dialog starts from.
 *
 * Pending accounts start unticked: `has_holdings` is a hint for the user, never
 * an inclusion default — exchange cash and staking accounts report no holdings.
 */
export function initialSelection(accounts: ProviderAccount[]): Set<string> {
  return new Set(accounts.filter((a) => a.status === 'included').map((a) => a.external_id))
}

/** Whether saving the dialog should write an allowlist at all.
 *
 * A connection that never configured one syncs everything, and must keep doing
 * so until the user opts in (issue #46). Every account of such a connection
 * reads as included, so writing the ticked set on an untouched save — a rename,
 * say — would silently pin an allowlist and stop future accounts syncing.
 */
export function shouldSaveAllowlist(
  selected: Set<string>,
  accounts: ProviderAccount[],
  stored: string[] | null | undefined,
): boolean {
  if (Array.isArray(stored)) return true
  const initial = initialSelection(accounts)
  return selected.size !== initial.size || [...selected].some((id) => !initial.has(id))
}

/** The allowlist to save: what is ticked, plus stored ids the provider stopped
 * exposing — dropping those would silently un-select an account the user chose
 * while their bank was mid-outage.
 */
export function buildAllowlist(
  selected: Set<string>,
  accounts: ProviderAccount[],
  stored: string[] | null | undefined,
): string[] {
  const shown = new Set(accounts.map((a) => a.external_id))
  const kept = (stored ?? []).filter((id) => !shown.has(id))
  return [...accounts.filter((a) => selected.has(a.external_id)).map((a) => a.external_id), ...kept]
}
