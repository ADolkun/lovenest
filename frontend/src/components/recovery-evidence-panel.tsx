import { useEffect, useRef, useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { Link, useSearchParams } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import { useWorkspace } from '@/contexts/workspace-context'
import { usePrivacyMode } from '@/hooks/use-privacy-mode'
import { assetErrorMessage } from '@/lib/api'
import { recovery, type RecoveryFilters } from '@/lib/recovery-api'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { NativeSelect } from '@/components/ui/native-select'
import { Skeleton } from '@/components/ui/skeleton'
import { RecoveryEvidenceForm, type RecoveryDraft } from './recovery-evidence-form'
import { RecoveryReviewForm } from './recovery-review-form'
import type { AssetGroup } from '@/types'
import type { EvidenceLeg, EvidenceObservation } from '@/types/investment-evidence'
import { RECOVERY_ROLES, type RecoveryDetails, type RecoveryEntryInput, type RecoveryEntryRead, type RecoveryPackage, type RecoveryState } from '@/types/recovery-evidence'

const selectClass = 'min-h-11 rounded-md border border-input bg-card px-3 text-base sm:text-sm'
const detailKeys = ['account_bucket', 'boundary_kind', 'claim_amount', 'claim_currency', 'proceeds', 'proceeds_currency', 'cash_credited', 'cash_currency', 'acquisition_date', 'statement_date', 'reported_cost', 'reported_cost_currency', 'provisional_allocation', 'allocation_currency'] as const
const sourceKinds = { allowed_claim: 'recovery_notice', recovery_notice: 'recovery_notice', tax_workpaper: 'tax_workpaper', equity_statement: 'balance_snapshot', platform_ledger: 'primary_activity', receiving_receipt: 'primary_activity', disposition: 'primary_activity', cash_proceeds: 'primary_activity' } as const

function recoveryError(error: unknown, fallback: string): string {
  const detail = (error as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail
  if (Array.isArray(detail)) return detail.map((issue: { loc?: (string | number)[]; msg?: string }) => `${String(issue.loc?.filter((part) => typeof part === 'string').at(-1) ?? 'source').replaceAll('_', ' ')}: ${issue.msg ?? fallback}`).join('; ')
  return error instanceof Error && !('response' in error) ? error.message : assetErrorMessage(error, fallback)
}

async function draftEntries(draft: RecoveryDraft, observations: EvidenceObservation[]): Promise<RecoveryEntryInput[]> {
  const { source, role } = draft
  const existing = observations.find((item) => item.reference === source.existing_observation_id)
  if (source.existing_observation_id && !existing) throw new Error('The selected retained source is unavailable. Refresh the source list.')
  const rows = existing ? draft.legs.slice(0, 1) : draft.legs
  const legs: EvidenceLeg[] = rows.map((row, index) => ({
    key: `asset-${index + 1}`, asset_symbol: row.asset_symbol || null, asset_id: null, chain: row.chain || null, token_address: row.token_address || null, isin: row.isin || null, provider_asset_id: row.provider_asset_id || null,
    direction: role === 'receiving_receipt' ? 'in' : role === 'disposition' ? 'out' : 'unknown', classification: role === 'receiving_receipt' ? 'transfer' : role === 'disposition' ? 'sell' : 'unknown',
    quantity: row.quantity || null, unit_price: null, subtotal: null, total: null, fee: row.fee || null, fee_currency: row.fee_currency || null,
    valuation_amount: row.valuation_amount || null, valuation_currency: row.valuation_currency || null, acquisition_basis: row.acquisition_basis || null,
    external_funding_amount: null, external_funding_currency: null, transaction_ref: null, leg_ref: null, execution_id: null,
  }))
  const observation: EvidenceObservation = {
    reference: source.source_reference, source_reference: source.source_reference, source: 'recovery_manual', source_kind: sourceKinds[role], provider: source.provider,
    source_account_id: source.source_account_id || null, source_local_id: source.source_reference, source_locator: source.source_locator,
    observed_at: null, event_time_raw: source.event_time_raw || null, event_date: source.event_date || null, event_at: source.event_at || null, timezone: source.timezone || null,
    time_precision: (source.time_precision || 'unknown') as EvidenceObservation['time_precision'], provider_status: source.provider_status || null, network_status: null,
    settlement_status: (source.settlement_status || 'unknown') as EvidenceObservation['settlement_status'], order_ref: null, historical_workspace_label: source.historical_workspace_label || null, coverage: [], legs,
  }
  return Promise.all(rows.map(async (row, index) => ({
    key: Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256', new TextEncoder().encode(JSON.stringify([existing?.reference ?? [source.provider, source.source_account_id, source.source_reference], existing ? source.existing_leg_key : legs[index].key, role])))), (byte) => byte.toString(16).padStart(2, '0')).join(''),
    observation_id: existing?.reference ?? null, observation: existing ? null : observation, leg_key: existing ? source.existing_leg_key : legs[index].key,
    case_key: source.case_key, round_key: source.round_key || null, round_asset_key: row.round_asset_key || null, role,
    reported_state: (source.reported_state || 'candidate') as RecoveryState,
    details: Object.fromEntries(detailKeys.map((key) => [key, row[key] || source[key] || null])) as RecoveryDetails,
    missing_evidence: (source.missing_evidence || '').split(';').map((value) => value.trim()).filter(Boolean),
  })))
}

export function RecoveryEvidencePanel({ scopeWalletIds = null }: { scopeWalletIds?: string[] | null }) {
  const { current } = useWorkspace()
  const { t } = useTranslation()
  const { mask } = usePrivacyMode()
  const [params, setParams] = useSearchParams()
  const workspaceId = current?.id ?? ''
  const wallets = useQuery({ queryKey: ['recovery-wallets', workspaceId], queryFn: ({ signal }) => recovery.wallets(workspaceId, signal), enabled: !!workspaceId })
  const scoped = wallets.data?.filter((wallet) => scopeWalletIds === null || scopeWalletIds.includes(wallet.id)) ?? []
  const groupId = params.get('recovery_wallet') ?? params.get('wallet') ?? (scopeWalletIds?.length === 1 ? scopeWalletIds[0] : '')
  const group = scoped.find((wallet) => wallet.id === groupId)
  const filters: RecoveryFilters = { group_id: groupId, ...Object.fromEntries(['case_key', 'round_key', 'role', 'state', 'q'].flatMap((key) => params.get(`recovery_${key}`) ? [[key, params.get(`recovery_${key}`)!]] : [])) }
  return <section className="min-w-0 space-y-5" aria-label={t('recovery.title', 'Recovery evidence')}>
    <div className="space-y-2"><h2 className="text-lg font-semibold">{t('recovery.title', 'Recovery evidence')}</h2><p className="max-w-prose text-sm text-muted-foreground">{t('recovery.intro', 'Reconcile claims, distribution rounds, receipts and workpapers without adding them together. This is a private evidence review, not a tax determination.')}</p></div>
    <p className="text-sm font-medium">{t('evidence.workspace', 'Workspace')}: {mask(current?.name ?? t('recovery.unknown', 'Unknown'))}</p>
    <div className="max-w-xl space-y-1.5"><Label htmlFor="recovery-destination">{t('recovery.destination', 'Recovery destination wallet / account')}</Label><NativeSelect id="recovery-destination" className={selectClass} value={group?.id ?? ''} disabled={wallets.isFetching} onChange={(event) => setParams((previous) => { const next = new URLSearchParams(previous); for (const key of [...next.keys()]) if (key.startsWith('recovery_')) next.delete(key); next.set('recovery_wallet', event.target.value); return next })}><option value="">{t('recovery.chooseDestination', 'Choose an existing wallet / account')}</option>{scoped.map((wallet) => <option key={wallet.id} value={wallet.id}>{mask(wallet.name)}</option>)}</NativeSelect></div>
    {wallets.isPending && <Skeleton className="h-16 w-full" />}
    {wallets.isError && <div role="alert"><p>{t('recovery.walletError', 'Wallets could not be loaded.')}</p><Button variant="outline" onClick={() => void wallets.refetch()}>{t('common.retry', 'Retry')}</Button></div>}
    {wallets.isSuccess && !scoped.length && <p className="text-sm">{t('recovery.noWallets', 'No wallets match this workspace and filter. Select another scope or add a wallet in Accounts before retaining evidence.')} <Link className="underline" to="/accounts">{t('nav.accounts', 'Accounts')}</Link></p>}
    {group && <RecoveryWallet key={`${workspaceId}:${group.id}:${JSON.stringify(filters)}`} workspaceId={workspaceId} group={group} wallets={wallets.data ?? []} filters={filters} />}
  </section>
}

function RecoveryWallet({ workspaceId, group, wallets, filters }: { workspaceId: string; group: AssetGroup; wallets: AssetGroup[]; filters: RecoveryFilters }) {
  const { canWrite } = useWorkspace()
  const { t } = useTranslation()
  const { mask } = usePrivacyMode()
  const client = useQueryClient()
  const [, setParams] = useSearchParams()
  const [sourceGroup, setSourceGroup] = useState(group.id)
  const [preview, setPreview] = useState<{ entries: RecoveryEntryInput[]; data: RecoveryPackage } | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<unknown>(null)
  const [notice, setNotice] = useState('')
  const [page, setPage] = useState(0)
  const [formVersion, setFormVersion] = useState(0)
  const generation = useRef(0)
  const lock = useRef(false)
  useEffect(() => () => { generation.current += 1 }, [])
  const key = ['recovery', workspaceId, filters]
  const query = useQuery({ queryKey: key, queryFn: ({ signal }) => recovery.list(workspaceId, filters, signal), retry: false })
  const sources = useQuery({ queryKey: ['recovery-sources', workspaceId, sourceGroup], queryFn: ({ signal }) => recovery.sources(workspaceId, sourceGroup, signal), retry: false })
  const context = useQuery({ queryKey: ['recovery', workspaceId, { group_id: sourceGroup }], queryFn: ({ signal }) => recovery.list(workspaceId, { group_id: sourceGroup }, signal), retry: false })
  const observations = sources.data?.target.workspace_id === workspaceId && sources.data.target.group_id === sourceGroup ? sources.data.observations : []
  const data = preview?.data ?? query.data
  const valid = data?.workspace_id === workspaceId && data.group_id === group.id
  const view = valid ? data : undefined
  const checked = (result: RecoveryPackage) => {
    if (result.workspace_id !== workspaceId || result.group_id !== group.id) throw new Error(t('recovery.changedTarget', 'The response destination changed. Refresh and review the selected wallet again.'))
    return result
  }
  const invalidateDraft = () => { generation.current += 1; setPreview(null); setBusy(false); setError(null); setNotice('') }
  const perform = async (operation: () => Promise<RecoveryPackage>, retain = false) => {
    if (lock.current || !canWrite) return
    lock.current = true
    const current = ++generation.current
    setBusy(true); setError(null); setNotice('')
    try {
      const result = checked(await operation())
      if (current !== generation.current) return
      setPreview(null); setPage(0)
      if (retain) { setFormVersion((value) => value + 1); void client.invalidateQueries({ queryKey: ['recovery-sources', workspaceId] }) }
      await client.invalidateQueries({ queryKey: ['recovery', workspaceId] })
      if (current === generation.current) setNotice(t('recovery.saved', 'Evidence review saved. Holdings, basis and tax treatment are unchanged.'))
      return result
    } catch (failure) {
      if (current !== generation.current) return
      setError(failure); setPreview(null)
      await query.refetch()
    } finally { lock.current = false; if (current === generation.current) setBusy(false) }
  }
  const prepare = async (draft: RecoveryDraft) => {
    if (lock.current || !canWrite) return
    lock.current = true
    const current = ++generation.current
    setBusy(true); setError(null); setNotice(''); setPreview(null)
    try {
      const entries = await draftEntries(draft, observations)
      const result = checked(await recovery.preview(workspaceId, group.id, entries))
      if (current === generation.current) setPreview({ entries, data: result })
    } catch (failure) { if (current === generation.current) setError(failure) }
    finally { lock.current = false; if (current === generation.current) setBusy(false) }
  }
  const download = async (format: 'json' | 'csv') => {
    if (!view || preview || busy || query.isFetching || query.isError) return
    const current = ++generation.current
    setBusy(true); setError(null)
    try {
      const blob = await recovery.export(workspaceId, filters, view.revision, format)
      if (current !== generation.current) return
      const url = URL.createObjectURL(blob)
      const anchor = document.createElement('a')
      anchor.href = url; anchor.download = `recovery-evidence.${format}`
      document.body.appendChild(anchor); anchor.click(); anchor.remove(); URL.revokeObjectURL(url)
    } catch (failure) { if (current === generation.current) { setError(failure); await query.refetch() } }
    finally { if (current === generation.current) setBusy(false) }
  }
  const label = (value: string) => t(`recovery.value.${value}`, value.replaceAll('_', ' '))
  const entries = view?.entries.filter((entry) => !entry.reason_codes.includes('related_context')) ?? []
  const relatedEntries = view?.entries.filter((entry) => entry.reason_codes.includes('related_context')) ?? []
  const pages = Math.max(1, Math.ceil(entries.length / 25))
  const activePage = Math.min(page, pages - 1)
  const related = context.data?.workspace_id === workspaceId && context.data.group_id === sourceGroup ? context.data : null
  const reviewContext = view ? { ...view, entries: [...new Map([...view.entries, ...(related?.entries ?? [])].map((entry) => [entry.id ?? entry.key, entry])).values()], reviews: [...new Map([...view.reviews, ...(related?.reviews ?? [])].map((review) => [review.id, review])).values()] } : null
  return <div className="min-w-0 space-y-6">
    <form className="space-y-3 border-y border-border py-4" onChange={invalidateDraft} onSubmit={(event) => { event.preventDefault(); const values = new FormData(event.currentTarget); setParams((previous) => { const next = new URLSearchParams(previous); for (const name of ['case_key', 'round_key', 'role', 'state', 'q']) { const value = String(values.get(name) ?? '').trim(); if (value) next.set(`recovery_${name}`, value); else next.delete(`recovery_${name}`) } return next }) }}>
      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-5">
        {(['case_key', 'round_key', 'q'] as const).map((name) => <div key={name} className="space-y-1.5"><Label htmlFor={`recovery-filter-${name}`}>{t(`recovery.filter.${name}`, name === 'q' ? 'Search source, asset or reason' : name === 'case_key' ? 'Filter case' : 'Filter round')}</Label><Input className="min-h-11" id={`recovery-filter-${name}`} name={name} defaultValue={filters[name] ?? ''} /></div>)}
        <div className="space-y-1.5"><Label htmlFor="recovery-filter-role">{t('recovery.filter.role', 'Filter role')}</Label><NativeSelect className={selectClass} id="recovery-filter-role" name="role" defaultValue={filters.role ?? ''}><option value="">{t('common.all', 'All')}</option>{RECOVERY_ROLES.map((role) => <option key={role} value={role}>{label(role)}</option>)}</NativeSelect></div>
        <div className="space-y-1.5"><Label htmlFor="recovery-filter-state">{t('recovery.filter.state', 'Filter state')}</Label><NativeSelect className={selectClass} id="recovery-filter-state" name="state" defaultValue={filters.state ?? ''}><option value="">{t('common.all', 'All')}</option>{['confirmed', 'candidate', 'conflict', 'missing', 'reported', 'modeled', 'unverified', 'supported'].map((state) => <option key={state} value={state}>{label(state)}</option>)}</NativeSelect></div>
      </div>
      <div className="flex flex-wrap gap-2"><Button type="submit" variant="outline">{t('recovery.filter.apply', 'Apply filters')}</Button><Button type="reset" variant="ghost" onClick={() => { invalidateDraft(); setParams((previous) => { const next = new URLSearchParams(previous); for (const name of ['case_key', 'round_key', 'role', 'state', 'q']) next.delete(`recovery_${name}`); return next }) }}>{t('recovery.filter.clear', 'Clear filters')}</Button></div>
    </form>
    {canWrite && <details className="border-b border-border pb-5"><summary className="cursor-pointer py-3 font-medium focus-visible:outline-2 focus-visible:outline-ring">{t('recovery.addOrAttach', 'Add evidence or attach a retained source')}</summary><div className="space-y-4 pt-3">
      <div className="max-w-xl space-y-1.5"><Label htmlFor="recovery-source-wallet">{t('recovery.sourceWallet', 'Source wallet for retained evidence')}</Label><NativeSelect id="recovery-source-wallet" className={selectClass} value={sourceGroup} disabled={busy} onChange={(event) => { invalidateDraft(); setSourceGroup(event.target.value); setFormVersion((value) => value + 1) }}>{wallets.map((wallet) => <option key={wallet.id} value={wallet.id}>{mask(wallet.name)}</option>)}</NativeSelect></div>
      <p className="text-sm text-muted-foreground">{t('recovery.sourceBoundary', 'Retained sources keep their original account. New evidence belongs to the selected recovery destination; choosing a source account does not transfer assets.')}</p>
      {sources.isError && <div role="alert" className="text-sm"><p>{t('recovery.sourcesError', 'Retained sources could not be loaded. Manual entry remains available.')}</p><Button variant="outline" onClick={() => void sources.refetch()}>{t('common.retry', 'Retry')}</Button></div>}
      <RecoveryEvidenceForm key={`${formVersion}:${sourceGroup}`} busy={busy} observations={observations} onPreview={(draft) => void prepare(draft)} onChange={invalidateDraft} />
      <Link className="inline-flex min-h-11 items-center text-sm underline" to={`/import?${new URLSearchParams({ tab: 'investments', mode: 'evidence', wallet: sourceGroup })}`}>{t('recovery.importCsv', 'Import a CSV in Source review, then attach its retained evidence here')}</Link>
    </div></details>}
    {!canWrite && <p className="text-sm text-muted-foreground">{t('recovery.viewer', 'Viewers can read and export saved evidence. An editor can retain sources and record reviews.')}</p>}
    {query.isPending && <div role="status"><span className="sr-only">{t('recovery.loading', 'Loading recovery evidence…')}</span><Skeleton className="h-28 w-full" /></div>}
    {(error || query.isError) && <div role="alert" className="space-y-2 text-sm text-warning-foreground"><p>{recoveryError(error ?? query.error, t('recovery.error', 'The review could not be loaded or saved. Refresh before trying again.'))}</p><Button variant="outline" onClick={() => { invalidateDraft(); void query.refetch() }}>{t('recovery.refresh', 'Refresh recovery review')}</Button></div>}
    {data && !valid && <p role="alert">{t('recovery.changedTarget', 'The response destination changed. Refresh and review the selected wallet again.')}</p>}
    {notice && <p role="status" className="text-sm">{notice}</p>}
    {view && <>
      <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between"><div className="space-y-1"><h3 className="font-semibold">{mask(group.name)}</h3><p className="text-sm">{t('recovery.counts', '{{rounds}} rounds · {{assets}} asset records · {{observations}} evidence entries', { rounds: view.round_count, assets: view.asset_record_count, observations: entries.length })}</p></div><div className="flex flex-wrap gap-2">{(['json', 'csv'] as const).map((format) => <Button key={format} variant="outline" disabled={busy || !!preview || query.isFetching || query.isError} onClick={() => void download(format)}>{t('recovery.export', 'Export {{format}}', { format: format.toUpperCase() })}</Button>)}</div></div>
      <p className="max-w-prose text-xs text-muted-foreground">{t('recovery.exportScope', 'Exports include all matching entries and their related review context, not only this page. Unlike currencies, claims, receipts and workpapers are not summed.')}</p>
      {preview && <div className="space-y-3 border-y border-border py-4"><p role="status" className="text-sm">{t('recovery.previewOnly', 'Preview only: saving these observations adds no financial quantities or basis.')}</p><p className="text-xs text-muted-foreground">{t('recovery.previewScope', 'Preview includes saved destination evidence and this source. Your filters resume after save or cancel.')}</p><div className="flex flex-wrap gap-2"><Button disabled={!canWrite || busy} onClick={() => void perform(() => recovery.retain(workspaceId, group.id, preview.entries, preview.data.revision), true)}>{t('recovery.save', 'Save evidence')}</Button><Button variant="ghost" disabled={busy} onClick={invalidateDraft}>{t('common.cancel', 'Cancel')}</Button></div></div>}
      {view.coverage.length > 0 && <section aria-label={t('recovery.coverage', 'Coverage and scope')} className="space-y-3 border-y border-border py-4 text-sm"><h3 className="font-medium">{t('recovery.coverage', 'Coverage and scope')}</h3><ul className="list-inside list-disc space-y-1">{[...new Set(view.coverage)].map((reason) => <li key={reason} className="break-words">{mask(label(reason))}</li>)}</ul></section>}
      {(view.missing_evidence.length > 0 || view.allocation_blockers.length > 0) && <section aria-label={t('recovery.gaps', 'Evidence gaps and model blockers')} className="space-y-3 border-y border-border py-4 text-sm"><h3 className="font-medium">{t('recovery.gaps', 'Evidence gaps and model blockers')}</h3><ul className="list-inside list-disc space-y-1">{[...new Set([...view.missing_evidence, ...view.allocation_blockers])].map((reason) => <li key={reason} className="break-words">{mask(label(reason))}</li>)}</ul><p>{t('recovery.noFinalization', 'Missing inputs and unresolved controlling assumptions prevent final allocation. Correcting a reference alone does not verify a model or filing assertion.')}</p></section>}
      {!entries.length ? <p role="status" className="py-6 text-sm text-muted-foreground">{Object.keys(filters).length > 1 ? t('recovery.noMatches', 'No evidence matches these filters. Clear filters to inspect other retained sources.') : t('recovery.empty', 'No recovery evidence is saved for this wallet. Add a source or attach retained evidence to start.')}</p> : <div role="table" aria-label={t('recovery.table', 'Recovery reconciliation')} className="min-w-0 divide-y divide-border border-y border-border">
        <div role="row" className="hidden grid-cols-[1.2fr_1fr_1fr_1fr] gap-3 py-3 text-xs font-medium text-muted-foreground sm:grid">{['Source role / reference', 'Round / asset', 'Reported quantity / date', 'Evidence / application state'].map((text) => <div role="columnheader" key={text}>{t(`recovery.column.${text}`, text)}</div>)}</div>
        {entries.slice(activePage * 25, (activePage + 1) * 25).map((entry) => <RecoveryRow key={`${view.revision}:${entry.id ?? entry.key}`} entry={entry} data={reviewContext!} busy={busy || !!preview || query.isError || query.isFetching || context.isError} canWrite={canWrite && !preview} onSave={(review) => void perform(() => recovery.review(workspaceId, group.id, [review], view.revision))} />)}
      </div>}
      {relatedEntries.length > 0 && <details className="border-t border-border pt-4"><summary className="cursor-pointer py-3 text-sm font-medium">{t('recovery.relatedContext', 'Related evidence outside the selected filters')} ({relatedEntries.length})</summary><div role="table" aria-label={t('recovery.relatedContext', 'Related evidence outside the selected filters')}>{relatedEntries.map((entry) => <RecoveryRow key={entry.id ?? entry.key} entry={entry} data={reviewContext!} busy={true} canWrite={false} onSave={() => {}} />)}</div></details>}
      {pages > 1 && <div className="flex items-center justify-between gap-3"><Button variant="outline" disabled={activePage === 0} onClick={() => setPage(activePage - 1)}>{t('common.previous', 'Previous')}</Button><span className="text-sm tabular-nums">{activePage + 1} / {pages}</span><Button variant="outline" disabled={activePage + 1 >= pages} onClick={() => setPage(activePage + 1)}>{t('common.next', 'Next')}</Button></div>}
      <p className="text-xs text-muted-foreground">{t('recovery.precisionFooter', 'Unknown values remain unknown; displayed source precision is preserved. A source-confirmed notice can still have a missing receipt and unknown basis.')}</p>
    </>}
  </div>
}

function RecoveryRow({ entry, data, busy, canWrite, onSave }: { entry: RecoveryEntryRead; data: RecoveryPackage; busy: boolean; canWrite: boolean; onSave: (review: Parameters<typeof recovery.review>[2][number]) => void }) {
  const { t } = useTranslation()
  const { mask } = usePrivacyMode()
  const [reviewing, setReviewing] = useState(false)
  const unknown = t('recovery.unknown', 'Unknown')
  const label = (value: string) => t(`recovery.value.${value}`, value.replaceAll('_', ' '))
  const source = entry.observation
  const leg = source.legs.find((item) => item.key === entry.leg_key)
  const reviews = data.reviews.filter((review) => review.entry_id === entry.id || review.target_entry_id === entry.id)
  const fields = { original_source_wallet: entry.source_group_name, source_provider: source.provider, source_reference: source.source_local_id, source_locator: source.source_locator, historical_source_account: source.source_account_id, historical_workspace_context: source.historical_workspace_label, reported_time: source.event_time_raw ?? source.event_at ?? source.event_date, source_precision: source.time_precision, timezone: source.timezone, retrieved_at: source.observed_at, provider_status: source.provider_status, network_status: source.network_status, settlement_status: source.settlement_status, ...leg, ...entry.details }
  return <div role="row" aria-label={`${label(entry.role)}: ${mask(source.source_local_id ?? source.source_locator)}`} className="min-w-0 py-3"><div role="cell"><details>
    <summary className="cursor-pointer rounded-md py-2 focus-visible:outline-2 focus-visible:outline-ring"><span className="grid min-w-0 gap-2 text-sm sm:grid-cols-[1.2fr_1fr_1fr_1fr] sm:gap-3"><span className="min-w-0 break-words"><strong>{label(entry.role)}</strong><span className="mt-1 block break-all text-xs text-muted-foreground">{mask(source.source_local_id ?? source.source_locator)}</span></span><span className="break-words">{mask(entry.case_key)} · {mask(entry.round_key ?? t('recovery.unassignedRound', 'Round unassigned'))}<span className="block">{mask(leg?.asset_symbol ?? unknown)} · {mask(entry.round_asset_key ?? t('recovery.unassignedAsset', 'Asset reference unassigned'))}</span></span><span className="break-words tabular-nums">{mask(leg?.quantity ?? unknown)}<span className="block text-xs">{mask(source.event_time_raw ?? source.event_at ?? source.event_date ?? unknown)} · {label(source.time_precision)}</span></span><span className="break-words"><span className="block">{t('recovery.sourceState', 'Source state')}: {label(entry.reported_state)}</span><span className="block">{t('recovery.quantityApplication', 'Quantity application')}: {entry.role === 'receiving_receipt' ? label(entry.application.status) : t('recovery.evidenceOnly', 'Evidence only')}</span></span></span></summary>
    <div className="space-y-4 py-4 text-sm">
      <h4 className="font-medium">{t('recovery.originalFacts', 'Original source facts and separately reported details')}</h4>
      <dl className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">{Object.entries(fields).filter(([key]) => !['key', 'derivation', 'asset_id'].includes(key)).map(([key, value]) => <div key={key} className="min-w-0"><dt className="text-muted-foreground">{label(key)}</dt><dd className="mt-1 break-all">{mask(value == null ? unknown : typeof value === 'object' ? Object.entries(value).map(([name, text]) => `${label(name)}: ${text}`).join('; ') : String(value))}</dd></div>)}</dl>
      {source.source_fields && <dl className="grid gap-3 sm:grid-cols-2">{Object.entries(source.source_fields).map(([key, value]) => <div key={key}><dt className="text-muted-foreground">{t('recovery.original', 'Original')} {label(key)}</dt><dd className="break-words">{mask(value ?? unknown)}</dd></div>)}</dl>}
      <ul className="list-inside list-disc space-y-1">{[...new Set([...entry.missing_evidence, ...entry.reason_codes, ...entry.application.reason_codes])].map((reason) => <li key={reason} className="break-words">{mask(label(reason))}</li>)}</ul>
      {entry.role === 'receiving_receipt' && entry.application.status === 'unsupported' && <p>{t('recovery.offchainUnsupported', 'This receipt remains evidence only. Quantity application requires independently supported movement evidence; missing chain facts or basis are never invented.')}</p>}
      {entry.application.leg_id && entry.application.status !== 'unsupported' && <Link className="inline-flex min-h-11 items-center underline" to={`/assets?${new URLSearchParams({ tab: 'activity', activity: 'transfers', movement: entry.application.leg_id, ...(entry.source_group_id ? { wallet: entry.source_group_id } : {}) })}`}>{t('recovery.movement', 'Review supported quantity movement')}</Link>}
      {reviews.length > 0 && <div className="space-y-4"><h4 className="font-medium">{t('recovery.recordedReviews', 'Recorded relationships, corrections and model assertions')}</h4>{reviews.map((review) => <div key={review.id} className="space-y-2 border-t border-border pt-3"><p className="font-medium">{label(review.kind)} · {label(review.relation_state ?? review.assertion_status ?? 'modeled')} · {review.is_current ? t('recovery.current', 'Current review') : t('recovery.superseded', 'Superseded review retained')}</p><dl className="grid gap-3 sm:grid-cols-2">{Object.entries({ meaning: review.relation_kind ?? review.assertion_kind, field: review.field, asserted_value: review.value, currency: review.currency, proposed_correction: review.proposed_value, source: review.source_locator, reason: review.reason, created_at: review.created_at, related_source: data.entries.find((item) => item.id === review.target_entry_id)?.observation.source_locator ?? null, documented_account_mapping: review.account_mapping_evidence, documented_timing: review.timing_evidence, quantity_adjustment: review.quantity_adjustment, adjustment_source: review.adjustment_evidence, supporting_sources: review.supporting_observation_ids.map((id) => data.entries.find((item) => item.observation_id === id)?.observation.source_locator ?? id).join('; ') || null, required_inputs: review.required_entry_ids.map((id) => data.entries.find((item) => item.id === id)?.observation.source_locator ?? id).join('; ') || null, required_assumptions: review.required_review_ids.map((id) => data.reviews.find((item) => item.id === id)?.reason ?? id).join('; ') || null }).map(([key, value]) => <div key={key}><dt className="text-muted-foreground">{label(key)}</dt><dd className="break-words">{mask(value ?? unknown)}</dd></div>)}</dl>{review.blockers.length > 0 && <ul className="list-inside list-disc">{review.blockers.map((reason) => <li key={reason}>{mask(label(reason))}</li>)}</ul>}{review.kind === 'allocation' && <p>{review.ready_for_review ? t('recovery.ready', 'Inputs ready for review; allocation and filing treatment are not finalized.') : t('recovery.blocked', 'Allocation blocked by unresolved inputs or assumptions.')}</p>}</div>)}</div>}
      {canWrite && entry.id && <><Button variant="outline" disabled={busy} onClick={() => setReviewing(!reviewing)} aria-expanded={reviewing}>{reviewing ? t('recovery.closeReview', 'Close review form') : t('recovery.addReview', 'Add relationship, correction or model review')}</Button>{reviewing && <RecoveryReviewForm entry={entry} data={data} busy={busy} onSave={onSave} />}</>}
    </div>
  </details></div></div>
}
