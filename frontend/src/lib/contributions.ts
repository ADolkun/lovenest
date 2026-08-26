import type {
  AssetContribution,
  ContributionKind,
  ContributionParty,
  ContributionSummary,
  ContributionYear,
} from '@/types'

export const CONTRIBUTION_KINDS: readonly ContributionKind[] = ['contribution', 'distribution']
export const CONTRIBUTION_PARTIES: readonly ContributionParty[] = ['self', 'employer']

/** The form's state: every field a string, as the house forms keep them. */
export interface ContributionDraft {
  groupId: string
  kind: ContributionKind
  party: ContributionParty
  amount: string
  date: string
  taxYear: string
  vestedOn: string
  notes: string
}

export interface ContributionPayload {
  group_id: string
  kind: ContributionKind
  party: ContributionParty
  amount: number
  date: string
  tax_year: number
  vested_on: string | null
  notes: string | null
}

/** Years desc, with the gross figure annual limits are measured against. */
export interface AnnualRow extends ContributionYear {
  gross: number
}

export function yearOf(date: string): number {
  return Number(date.slice(0, 4))
}

export function emptyDraft(groupId: string, today: string): ContributionDraft {
  return {
    groupId,
    kind: 'contribution',
    party: 'self',
    amount: '',
    date: today,
    taxYear: String(yearOf(today)),
    vestedOn: '',
    notes: '',
  }
}

export function draftFromContribution(row: AssetContribution): ContributionDraft {
  return {
    groupId: row.group_id,
    kind: row.kind,
    party: row.party,
    amount: String(row.amount),
    date: row.date,
    taxYear: String(row.tax_year),
    vestedOn: row.vested_on ?? '',
    notes: row.notes ?? '',
  }
}

/**
 * The one case where `tax_year` is worth pointing at: a total for a year that
 * predates provider coverage is typed in as one row dated today, so the date
 * and the year it counts against disagree on purpose.
 */
export function isPriorYearEntry(draft: ContributionDraft): boolean {
  const taxYear = draft.taxYear.trim() ? Number(draft.taxYear) : NaN
  if (!draft.date || !Number.isFinite(taxYear)) return false
  return taxYear !== yearOf(draft.date)
}

/**
 * The first thing wrong with the draft, as an i18n key, or null. The last two
 * checks mirror the server's validator so the user is told before a 422.
 */
export function draftError(draft: ContributionDraft): string | null {
  if (!draft.groupId) return 'assets.contribErrWallet'
  const amount = Number(draft.amount)
  if (!draft.amount.trim() || !Number.isFinite(amount) || amount <= 0) return 'assets.contribErrAmount'
  if (!draft.date) return 'assets.contribErrDate'
  const taxYear = draft.taxYear.trim() ? Number(draft.taxYear) : NaN
  if (!Number.isInteger(taxYear) || taxYear < 1900 || taxYear > 2200) return 'assets.contribErrTaxYear'
  if (draft.party === 'employer' && draft.kind !== 'contribution') return 'assets.contribErrEmployerDistribution'
  if (draft.vestedOn && draft.party !== 'employer') return 'assets.contribErrVestingOwnMoney'
  return null
}

export function draftPayload(draft: ContributionDraft): ContributionPayload {
  return {
    group_id: draft.groupId,
    kind: draft.kind,
    party: draft.party,
    amount: Number(draft.amount),
    date: draft.date,
    tax_year: Number(draft.taxYear),
    vested_on: draft.party === 'employer' && draft.vestedOn ? draft.vestedOn : null,
    notes: draft.notes.trim() || null,
  }
}

export function summariesByWallet(summaries: ContributionSummary[]): Map<string, ContributionSummary> {
  return new Map(summaries.map((s) => [s.group_id, s]))
}

export function annualRows(summary: ContributionSummary | undefined): AnnualRow[] {
  if (!summary) return []
  return [...summary.years]
    .sort((a, b) => b.tax_year - a.tax_year)
    .map((year) => ({ ...year, gross: year.own + year.employer }))
}

/** Newest movement first; ties broken so the order never shuffles on refetch. */
export function rowsByWallet(rows: AssetContribution[]): Map<string, AssetContribution[]> {
  const byWallet = new Map<string, AssetContribution[]>()
  for (const row of rows) {
    const bucket = byWallet.get(row.group_id)
    if (bucket) bucket.push(row)
    else byWallet.set(row.group_id, [row])
  }
  for (const bucket of byWallet.values()) {
    bucket.sort(
      (a, b) =>
        b.date.localeCompare(a.date) || b.tax_year - a.tax_year || a.id.localeCompare(b.id),
    )
  }
  return byWallet
}
