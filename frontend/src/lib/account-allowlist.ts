import type { ProviderAccount } from '../types'

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
