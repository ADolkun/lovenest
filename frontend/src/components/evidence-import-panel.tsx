import { useEffect, useRef, useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { Link } from 'react-router-dom'
import { assets, assetErrorMessage } from '@/lib/api'
import { timeline } from '@/lib/timeline-api'
import { useWorkspace } from '@/contexts/workspace-context'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { ImportHistory } from '@/components/import-history'
import { NativeSelect } from '@/components/ui/native-select'
import type { AssetGroup, AssetImportPreview } from '@/types'
import type { EvidenceDecision, EvidenceObservation, EvidencePreview, EvidenceRecord, EvidenceSourceKind } from '@/types/investment-evidence'

const SELECT_CLASS = 'min-h-10 rounded-md border border-border bg-card px-3 py-2 text-base focus-visible:outline-2 focus-visible:outline-ring sm:text-sm'
const SOURCE_KINDS: EvidenceSourceKind[] = ['primary_activity', 'balance_snapshot', 'remaining_lots', 'tax_workpaper', 'recovery_notice']
const MATCH_STATES = ['linked', 'candidate', 'conflicting', 'unmatched'] as const
const MAPPING_FIELDS = ['ticker', 'date', 'quantity', 'price', 'kind', 'currency', 'execution_currency', 'unit_price_currency', 'subtotal_currency', 'total_currency', 'cost_basis', 'external_id', 'execution_id', 'subtotal', 'total', 'fee', 'fee_currency', 'valuation_currency', 'valuation_amount', 'external_funding_amount', 'external_funding_currency', 'provider_status', 'network_status', 'transaction_ref', 'order_ref', 'leg_ref', 'chain', 'token_address', 'token_program', 'source_address', 'destination_address', 'source_owner', 'destination_owner', 'raw_units', 'decimals', 'quantity_role', 'fee_payer', 'fee_semantics', 'provider_asset_id', 'isin', 'timezone', 'historical_workspace_label', 'date_sold', 'proceeds']
const label = (value: string) => value.replaceAll('_', ' ')

export function EvidenceImportPanel({ mode = 'evidence', initialGroupId = '' }: { mode?: 'evidence' | 'opening_lots'; initialGroupId?: string }) {
  const { t } = useTranslation()
  const { current } = useWorkspace()
  const [groupId, setGroupId] = useState(initialGroupId)
  const wallets = useQuery({ queryKey: ['asset-groups', current?.id, 'source-review'], queryFn: ({ signal }) => timeline.wallets(current!.id, signal), enabled: !!current })
  const group = wallets.data?.find((wallet) => wallet.id === groupId)
  return <section className="space-y-4" aria-label={t('evidence.sourceReview', 'Source review')}>
    <p className="max-w-prose text-sm text-muted-foreground">{t('evidence.intro', 'Keep source observations, review their links, then apply supported activity. Saving evidence does not change holdings or establish acquisition basis.')}</p>
    <div className="max-w-xl space-y-2">
      <Label htmlFor="evidence-wallet">{t('evidence.destination', 'Destination wallet / account')}</Label>
      <NativeSelect id="evidence-wallet" className={SELECT_CLASS} value={groupId} onChange={(event) => setGroupId(event.target.value)} disabled={wallets.isFetching}>
        <option value="">{t('assetImport.chooseWallet')}</option>
        {wallets.data?.map((wallet) => <option key={wallet.id} value={wallet.id}>{wallet.name}</option>)}
      </NativeSelect>
      {wallets.isPending && <p role="status" className="text-sm text-muted-foreground">{t('evidence.loadingWallets', 'Loading wallets…')}</p>}
      {wallets.isError && <div role="alert" className="space-y-2 text-sm text-warning-foreground"><p>{assetErrorMessage(wallets.error, t('evidence.walletError', 'Could not load wallets.'))}</p><Button variant="outline" onClick={() => wallets.refetch()}>{t('common.retry', 'Retry')}</Button></div>}
      {wallets.isSuccess && !wallets.data.length && <p className="text-sm text-muted-foreground">{t('assetImport.noWalletsYet')}</p>}
    </div>
    {group && current && <EvidenceWalletReview key={`${current.id}:${group.id}:${mode}`} group={group} workspaceId={current.id} mode={mode} />}
  </section>
}

function EvidenceWalletReview({ group, workspaceId, mode }: { group: AssetGroup; workspaceId: string; mode: 'evidence' | 'opening_lots' }) {
  const { t } = useTranslation()
  const { canWrite } = useWorkspace()
  const queryClient = useQueryClient()
  const [file, setFile] = useState<File | null>(null)
  const [provider, setProvider] = useState(group.source === 'manual' ? 'csv' : group.source)
  const [sourceAccount, setSourceAccount] = useState('')
  const [sourceKind, setSourceKind] = useState<EvidenceSourceKind>(mode === 'opening_lots' ? 'remaining_lots' : 'primary_activity')
  const [dateFormat, setDateFormat] = useState('')
  const [mapping, setMapping] = useState<Record<string, string>>({})
  const [columns, setColumns] = useState<string[]>([])
  const [detectedMapping, setDetectedMapping] = useState<Record<string, string>>({})
  const [openingDate, setOpeningDate] = useState('')
  const [openingAssumption, setOpeningAssumption] = useState('')
  const [overlapReviewed, setOverlapReviewed] = useState(false)
  const [allowUnpriced, setAllowUnpriced] = useState(false)
  const [preview, setPreview] = useState<AssetImportPreview | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)
  const [mutating, setMutating] = useState(false)
  const [filter, setFilter] = useState('all')
  const [search, setSearch] = useState('')
  const [page, setPage] = useState(0)
  const boundary = mode === 'opening_lots' && openingDate && openingAssumption.trim() && overlapReviewed ? { as_of: openingDate, assumption: openingAssumption.trim(), overlap_reviewed: true } : undefined
  const queryKey = ['investment-evidence', workspaceId, group.id, boundary]
  const stored = useQuery({ queryKey, queryFn: () => assets.evidence(group.id, boundary), retry: false })
  const request = useRef(0)
  const upload = useRef<HTMLInputElement>(null)
  const mutationLock = useRef(false)
  useEffect(() => () => { request.current += 1 }, [])

  function invalidatePreview() {
    request.current += 1
    setPreview(null)
    setLoading(false)
    setError(null)
    setNotice(null)
    setPage(0)
  }

  function checkedDestination(result: EvidencePreview) {
    if (result.target.workspace_id !== workspaceId || result.target.group_id !== group.id) {
      throw new Error(t('evidence.destinationChanged', 'The preview destination changed. Refresh and review the selected wallet again.'))
    }
    return result
  }

  async function runPreview() {
    if (!file || !provider.trim() || mutating || (mode === 'opening_lots' && !boundary)) return
    const generation = ++request.current
    setPreview(null)
    setError(null)
    setNotice(null)
    setLoading(true)
    try {
      const result = await assets.previewImport(file, { mode, group_id: group.id, connection_id: group.connection_id, provider: provider.trim(), source_account_id: sourceAccount.trim() || undefined, source_kind: sourceKind, column_mapping: mapping, date_format: dateFormat || undefined, opening_boundary: boundary })
      if (generation !== request.current) return
      if (result.evidence) checkedDestination(result.evidence)
      setPreview(result)
      setColumns(result.csv_columns)
      setDetectedMapping(result.column_mapping ?? {})
      setPage(0)
    } catch (failure) {
      if (generation === request.current) setError(failure instanceof Error && !('response' in failure) ? failure.message : assetErrorMessage(failure, t('assetImport.previewError')))
    } finally {
      if (generation === request.current) setLoading(false)
    }
  }

  async function mutate(operation: () => Promise<EvidencePreview>) {
    if (!canWrite || mutationLock.current || loading) return
    mutationLock.current = true
    const generation = ++request.current
    setMutating(true)
    setError(null)
    setNotice(null)
    try {
      const result = checkedDestination(await operation())
      if (generation !== request.current) return
      queryClient.setQueryData(queryKey, result)
      setPreview(null)
      setFile(null)
      if (upload.current) upload.current.value = ''
      setNotice(t('evidence.saved', 'Review updated. Acquisition basis and coverage remain as reported below.'))
      setPage(0)
      for (const key of ['assets', 'asset-groups', 'import-logs']) queryClient.invalidateQueries({ queryKey: [key] })
    } catch (failure) {
      if (generation !== request.current) return
      setError(failure instanceof Error && !('response' in failure) ? failure.message : assetErrorMessage(failure, t('evidence.saveError', 'Could not save the review. Refresh and try again.')))
      // A failed or lost response may already have committed. Re-read before retrying.
      setPreview(null)
      await stored.refetch()
    } finally {
      mutationLock.current = false
      if (generation === request.current) setMutating(false)
    }
  }

  const view = preview?.evidence ?? (!file ? stored.data : null)
  const validDestination = view?.target.workspace_id === workspaceId && view?.target.group_id === group.id
  const records = validDestination ? view.records : []
  const observations = new Map(view?.observations.map((observation) => [observation.reference, observation]))
  const filtered = records.filter((record) => {
    const observation = observations.get(record.observation_ref)
    return (filter === 'all' || record.match_status === filter) && JSON.stringify([record.source_refs, record.reason_codes, observation?.legs, observation?.source_account_id]).toLowerCase().includes(search.trim().toLowerCase())
  })
  const pageCount = Math.max(1, Math.ceil(filtered.length / 25))
  const currentPage = Math.min(page, pageCount - 1)
  const busy = mutating || loading || stored.isFetching
  const actionable = !!view && validDestination && !file && canWrite && !busy && !stored.isError && (mode !== 'opening_lots' || !!boundary)

  return <div className="space-y-5">
    {canWrite && <div className="rounded-xl border border-border bg-card p-4 sm:p-5">
      <fieldset disabled={mutating} className="space-y-4">
        <legend className="mb-3 text-base font-semibold">{t('evidence.addSource', 'Add source evidence')}</legend>
        <div className="grid gap-4 sm:grid-cols-2">
          <div className="space-y-2"><Label htmlFor="evidence-file">{t('evidence.csvFile', 'CSV source file')}</Label><Input ref={upload} id="evidence-file" type="file" accept=".csv,text/csv" onChange={(event) => { invalidatePreview(); setMapping({}); setColumns([]); setDetectedMapping({}); setFile(event.target.files?.[0] ?? null) }} /></div>
          <div className="space-y-2"><Label htmlFor="evidence-kind">{t('evidence.sourceKind', 'Source kind')}</Label><NativeSelect id="evidence-kind" value={sourceKind} className={SELECT_CLASS} onChange={(event) => { invalidatePreview(); setSourceKind(event.target.value as EvidenceSourceKind) }}>{SOURCE_KINDS.map((kind) => <option value={kind} key={kind}>{t(`evidence.kind.${kind}`, label(kind))}</option>)}</NativeSelect></div>
          <div className="space-y-2"><Label htmlFor="evidence-provider">{t('evidence.provider', 'Source provider')}</Label><Input id="evidence-provider" value={provider} maxLength={64} onChange={(event) => { invalidatePreview(); setProvider(event.target.value) }} /><p className="text-xs text-muted-foreground">{t('evidence.providerHint', 'Use the provider identity, such as coinbase or robinhood, consistently across its sources.')}</p></div>
          <div className="space-y-2"><Label htmlFor="evidence-source-account">{t('evidence.sourceAccount', 'Source account ID (if supplied)')}</Label><Input id="evidence-source-account" value={sourceAccount} maxLength={255} onChange={(event) => { invalidatePreview(); setSourceAccount(event.target.value) }} /><p className="text-xs text-muted-foreground">{t('evidence.sourceAccountHint', 'A historical label is context. The destination is the selected workspace and wallet above.')}</p></div>
        </div>
        <details className="text-sm">
          <summary className="cursor-pointer py-2 font-medium focus-visible:outline-2 focus-visible:outline-ring">{t('evidence.mapping', 'Date format and column mapping')}</summary>
          <div className="mt-3 grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
            <div className="space-y-1"><Label htmlFor="evidence-date-format">{t('import.dateFormat')}</Label><NativeSelect id="evidence-date-format" value={dateFormat} className={SELECT_CLASS} onChange={(event) => { invalidatePreview(); setDateFormat(event.target.value) }}><option value="">{t('evidence.auto', 'Auto detect')}</option>{['DD/MM/YYYY', 'MM/DD/YYYY', 'YYYY-MM-DD'].map((format) => <option key={format}>{format}</option>)}</NativeSelect></div>
            {MAPPING_FIELDS.map((field) => <div key={field} className="space-y-1"><Label htmlFor={`evidence-map-${field}`}>{t(`evidence.field.${field}`, label(field))}</Label><Input id={`evidence-map-${field}`} list="evidence-columns" value={mapping[field] ?? detectedMapping[field] ?? ''} placeholder={field in mapping ? t('evidence.unmapped', 'Left unmapped') : t('evidence.auto', 'Auto detect')} onChange={(event) => { invalidatePreview(); setMapping((previous) => ({ ...previous, [field]: event.target.value })) }} /></div>)}
          </div>
          <datalist id="evidence-columns">{columns.map((column) => <option key={column} value={column}>{column}</option>)}</datalist>
          <p className="mt-2 text-xs text-muted-foreground">{t('evidence.mappingHint', 'Enter the exact source column name to override detection. Clearing an edited field leaves it unmapped.')}</p>
        </details>
      </fieldset>
    </div>}
    {mode === 'opening_lots' && <fieldset disabled={mutating} className="space-y-3 rounded-xl border border-border bg-muted/40 p-4 text-sm">
      <legend className="px-1 font-semibold">{t('evidence.openingBoundary', 'Standalone lot backfill boundary')}</legend>
      <div className="grid gap-3 sm:grid-cols-2"><div className="space-y-2"><Label htmlFor="opening-lot-date">{t('evidence.openingDate', 'Opening history as of')}</Label><Input id="opening-lot-date" type="date" required value={openingDate} onChange={(e) => { invalidatePreview(); setOpeningDate(e.target.value) }} /></div><div className="space-y-2"><Label htmlFor="opening-lot-assumption">{t('evidence.openingAssumption', 'Opening history assumption')}</Label><Input id="opening-lot-assumption" maxLength={500} required value={openingAssumption} onChange={(e) => { invalidatePreview(); setOpeningAssumption(e.target.value) }} /></div></div>
      <label className="flex items-start gap-2"><input type="checkbox" checked={overlapReviewed} onChange={(e) => { invalidatePreview(); setOverlapReviewed(e.target.checked) }} className="mt-1" /><span>{t('evidence.reviewedOverlap', 'I reviewed the opening history and overlap with existing acquisitions. These lots must not add the same inventory twice.')}</span></label>
    </fieldset>}
    {canWrite && <div className="flex flex-wrap gap-2"><Button onClick={runPreview} disabled={!file || !provider.trim() || loading || mutating || (mode === 'opening_lots' && !boundary)}>{loading ? t('evidence.previewing', 'Reading source…') : t('evidence.preview', 'Preview source')}</Button>{file && <Button variant="outline" disabled={mutating} onClick={() => { invalidatePreview(); setFile(null); if (upload.current) upload.current.value = '' }}>{t('evidence.cancelUpload', 'Cancel upload')}</Button>}</div>}
    {error && <div role="alert" className="space-y-2 rounded-lg border border-warning/30 bg-warning/10 p-4 text-sm text-warning-foreground"><p>{error}</p><Button variant="outline" disabled={busy} onClick={() => file ? runPreview() : stored.refetch()}>{t('evidence.refreshReview', 'Refresh review')}</Button></div>}
    {notice && <p role="status" className="text-sm text-muted-foreground">{notice}</p>}
    {stored.isPending && !file && <p role="status">{t('evidence.loading', 'Loading source review…')}</p>}
    {stored.isError && !file && <div role="alert" className="space-y-2 text-sm text-warning-foreground"><p>{assetErrorMessage(stored.error, t('evidence.loadError', 'Source review could not be loaded.'))}</p><Button variant="outline" onClick={() => stored.refetch()}>{t('common.retry', 'Retry')}</Button></div>}
    {preview?.parse_error && <p role="alert" className="text-sm text-warning-foreground">{preview.parse_error}</p>}
    {!!preview?.errors.length && <div role="alert" className="space-y-2 text-sm text-warning-foreground"><p>{t('evidence.refusedRows', 'Some rows or values could not be interpreted. Review these gaps before continuing.')}</p><ul className="list-inside list-disc">{preview.errors.map((row, index) => <li key={`${row.row}:${index}`}>{t('evidence.row', 'Row')} {row.row}: {label(row.reason)} {row.detail ?? ''}</li>)}</ul></div>}
    {!!preview?.unmapped_columns?.length && <p className="text-sm text-warning-foreground">{t('evidence.unmappedColumns', 'Columns not interpreted')}: {preview.unmapped_columns.join(', ')}</p>}
    {view && !validDestination && <p role="alert" className="text-sm text-warning-foreground">{t('evidence.destinationChanged', 'The preview destination changed. Refresh and review the selected wallet again.')}</p>}
    {view && validDestination && <>
      <div className="flex flex-col gap-3 border-b border-border pb-4 sm:flex-row sm:items-center sm:justify-between">
        <div className="min-w-0"><h2 className="break-words text-base font-semibold">{view.target.workspace_name} · {view.target.group_name}</h2><p className="mt-1 text-sm text-muted-foreground">{view.target.account_id ? `${t('evidence.accountId', 'Account ID')}: ${view.target.account_id}` : t('evidence.noAccount', 'No linked cash account. Evidence belongs to this wallet.')}</p></div>
        {file && <Button disabled={!canWrite || busy || !view.observations.length || (mode === 'opening_lots' && !boundary)} onClick={() => mutate(async () => (await assets.importEvidence({ mode, observations: view.observations, decisions: [], expected_revision: view.revision, group_id: group.id, connection_id: group.connection_id, filename: file.name, opening_boundary: boundary })).evidence)}>{t('evidence.saveObservations', 'Save observations')}</Button>}
      </div>
      {file && <p className="text-sm text-muted-foreground">{t('evidence.saveFirst', 'Preview only. Save observations to review links and application; this step adds no holdings or basis.')}</p>}
      {canWrite && !file && <label className="flex items-start gap-2 text-sm text-muted-foreground"><input type="checkbox" className="mt-1" checked={allowUnpriced} disabled={busy} onChange={(event) => setAllowUnpriced(event.target.checked)} /><span>{t('assetImport.allowUnpriced')}<span className="mt-1 block text-xs">{t('evidence.unpricedBoundary', 'Allow unavailable market quotes only. Missing acquisition basis remains unknown.')}</span></span></label>}
      <div className="flex flex-col gap-3 sm:flex-row">
        <div className="space-y-1 sm:w-56"><Label htmlFor="evidence-filter">{t('evidence.statusFilter', 'Match status')}</Label><NativeSelect id="evidence-filter" className={SELECT_CLASS} value={filter} onChange={(event) => { setFilter(event.target.value); setPage(0) }}><option value="all">{t('common.all', 'All')} ({records.length})</option>{MATCH_STATES.map((status) => <option key={status} value={status}>{t(`evidence.status.${status}`, label(status))} ({records.filter((record) => record.match_status === status).length})</option>)}</NativeSelect></div>
        <div className="flex-1 space-y-1"><Label htmlFor="evidence-search">{t('evidence.search', 'Search source IDs, assets or reasons')}</Label><Input id="evidence-search" type="search" value={search} onChange={(event) => { setSearch(event.target.value); setPage(0) }} /></div>
      </div>
      {!records.length ? <p className="rounded-lg border border-border p-5 text-sm text-muted-foreground">{t('evidence.empty', 'No source observations in this wallet. Preview an exchange or brokerage CSV to start.')}</p> : !filtered.length ? <p role="status" className="py-5 text-sm text-muted-foreground">{t('evidence.noMatches', 'No observations match these filters.')}</p> : <div className="divide-y divide-border border-y border-border">
        {filtered.slice(currentPage * 25, (currentPage + 1) * 25).map((record) => <EvidenceRecordReview key={`${view.revision}:${record.observation_ref}:${record.leg_key}`} groupId={group.id} record={record} observation={observations.get(record.observation_ref)} disabled={!actionable} onDecision={(decision) => mutate(async () => (await assets.confirmEvidence({ group_id: group.id, expected_revision: view.revision, decisions: [decision], opening_boundary: boundary, allow_unpriced: allowUnpriced })).evidence)} onUnlink={(id) => mutate(() => assets.unlinkEvidence(id, view.revision, boundary))} />)}
      </div>}
      {pageCount > 1 && <div className="flex items-center justify-between gap-3"><Button variant="outline" disabled={currentPage === 0} onClick={() => setPage(currentPage - 1)}>{t('common.previous', 'Previous')}</Button><span className="text-sm tabular-nums">{currentPage + 1} / {pageCount}</span><Button variant="outline" disabled={currentPage + 1 >= pageCount} onClick={() => setPage(currentPage + 1)}>{t('common.next', 'Next')}</Button></div>}
      <details className="rounded-xl border border-border p-4 text-sm" open>
        <summary className="cursor-pointer font-semibold focus-visible:outline-2 focus-visible:outline-ring">{t('evidence.coverage', 'Balance inputs and evidence gaps')}</summary>
        <p className="mt-3 text-muted-foreground">{t('evidence.coverageBoundary', 'A matching quantity does not establish complete history, acquisition basis, or original funding. Unknown values remain unknown.')}</p>
        {!view.reconciliation.length && <p className="mt-3 text-muted-foreground">{t('evidence.noCoverage', 'No balance reconciliation inputs are available.')}</p>}
        {view.reconciliation.map((row, index) => <div key={`${row.asset_symbol}:${index}`} className="mt-4 space-y-2 border-t border-border pt-4">
          <h3 className="font-semibold">{row.asset_symbol ?? t('evidence.unknownAsset', 'Unknown asset')}</h3>
          {[row.chain, row.token_address, row.provider_asset_id, row.isin].some(Boolean) && <p className="break-all text-xs text-muted-foreground">{[row.chain, row.token_address, row.provider_asset_id, row.isin].filter(Boolean).join(' · ')}</p>}
          <dl className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">{Object.entries({ opening_quantity: row.opening_quantity, opening_assumption: row.opening_assumption, opening_as_of: row.opening_as_of, settled_movement_quantity: row.settled_movement_quantity, snapshot_quantity: row.snapshot_quantity, snapshot_as_of: row.snapshot_as_of, expected_closing_quantity: row.expected_closing_quantity, discrepancy: row.discrepancy }).map(([key, value]) => <div key={key}><dt className="text-muted-foreground">{t(`evidence.field.${key}`, label(key))}</dt><dd className="mt-1 break-words tabular-nums">{value ?? t('evidence.unknown', 'Unknown')}</dd></div>)}</dl>
          <p>{row.basis_complete ? t('evidence.basisComplete', 'Basis supported for this evidence scope') : t('evidence.basisIncomplete', 'Acquisition basis incomplete / unknown')} · {row.history_complete ? t('evidence.historyComplete', 'History supported for this evidence scope') : t('evidence.historyIncomplete', 'History incomplete')}</p>
          {row.unresolved_fee_semantics && <p className="text-warning-foreground">{t('evidence.feeUnknown', 'Fee semantics unresolved')}</p>}{row.unresolved_funding_semantics && <p className="text-warning-foreground">{t('evidence.fundingUnknown', 'Original funding unresolved')}</p>}
          {!!row.missing_coverage.length && <ul className="list-inside list-disc text-warning-foreground">{row.missing_coverage.map((reason) => <li key={reason}>{label(reason)}</li>)}</ul>}
        </div>)}
      </details>
    </>}
    <ImportHistory entity="asset_evidence" disabled={busy} onUndo={invalidatePreview} />
  </div>
}

function EvidenceRecordReview({ groupId, record, observation, disabled, onDecision, onUnlink }: { groupId: string; record: EvidenceRecord; observation?: EvidenceObservation; disabled: boolean; onDecision: (decision: EvidenceDecision) => void; onUnlink: (id: string) => void }) {
  const { t } = useTranslation()
  const [selected, setSelected] = useState<Record<string, string>>({})
  const [reason, setReason] = useState('')
  const [reviewApply, setReviewApply] = useState(false)
  const [settlementConfirmed, setSettlementConfirmed] = useState(false)
  const [unlink, setUnlink] = useState<string | null>(null)
  const leg = observation?.legs.find((item) => item.key === record.leg_key)
  const unknown = t('evidence.unknown', 'Unknown')
  const allocations = Object.entries(selected).map(([leg_id, quantity]) => ({ leg_id, quantity: quantity || null }))
  const allocationValid = allocations.every(({ quantity }) => quantity === null || /^\d+(\.\d+)?$/.test(quantity))
  const decision = { observation_ref: record.observation_ref, leg_key: record.leg_key }
  const needsSettlement = observation?.settlement_status === 'unknown'
  const amountFields = leg ? ['quantity', 'unit_price', 'execution_currency', 'unit_price_origin', 'subtotal', 'total', 'fee', 'fee_currency', 'valuation_currency', 'valuation_amount', 'external_funding_amount', 'external_funding_currency', 'acquisition_basis', 'chain', 'token_address', 'isin', 'provider_asset_id', 'transaction_ref', 'leg_ref', 'execution_id', 'token_program', 'source_address', 'destination_address', 'source_owner', 'destination_owner', 'raw_units', 'decimals', 'quantity_role', 'fee_payer', 'fee_semantics'] as const : []
  return <details className="py-4">
    <summary className="cursor-pointer rounded-md py-1 focus-visible:outline-2 focus-visible:outline-ring">
      <span className="inline-flex max-w-full flex-wrap items-baseline gap-x-3 gap-y-1 align-middle text-sm"><strong>{leg?.asset_symbol ?? t('evidence.unknownAsset', 'Unknown asset')}</strong><span className="tabular-nums">{leg?.quantity ?? unknown} · {t(`evidence.direction.${leg?.direction ?? 'unknown'}`, leg?.direction ?? unknown)}</span><span className={record.match_status === 'conflicting' ? 'text-warning-foreground' : 'text-muted-foreground'}>{t(`evidence.status.${record.match_status}`, label(record.match_status))}</span><span>{t(`evidence.application.${record.application_status}`, record.application_status === 'not_applicable' ? 'Evidence only' : label(record.application_status))}</span><span className="max-w-full break-all text-muted-foreground">{observation?.source_local_id ?? observation?.source_locator ?? record.observation_ref}</span></span>
    </summary>
    <div className="mt-4 space-y-4 text-sm">
      <p>{t('evidence.reportedTime', 'Reported event time')}: {observation?.event_time_raw ?? observation?.event_at ?? observation?.event_date ?? unknown} · {t('evidence.precision', 'Precision')}: {observation?.time_precision ?? unknown} · {t('evidence.timezone', 'Timezone')}: {observation?.timezone ?? unknown}</p>
      <p>{t('evidence.providerStatus', 'Provider status')}: {observation?.provider_status ?? unknown} · {t('evidence.networkStatus', 'Network status')}: {observation?.network_status ?? unknown} · {t('evidence.settlement', 'Settlement')}: {observation?.settlement_status ?? unknown}</p>
      <p>{t('evidence.classification', 'Classification')}: {leg?.classification ?? unknown} · {t('evidence.sourceKind', 'Source kind')}: {observation?.source_kind ? label(observation.source_kind) : unknown}</p>
      {leg && ['transfer', 'unknown', 'fee', 'send', 'receive', 'withdrawal', 'deposit'].includes(leg.classification) && <Link className="inline-flex min-h-11 items-center text-sm font-medium underline underline-offset-4 focus-visible:outline-2 focus-visible:outline-ring" to={`/assets?${new URLSearchParams({ tab: 'activity', activity: 'transfers', wallet: groupId, observation_ref: record.observation_ref, leg_key: record.leg_key })}`}>{t('ownedTransfers.reviewMovement', 'Review movement and ownership')}</Link>}
      <p>{t('evidence.observedAt', 'Observed / retrieved at')}: {observation?.observed_at ?? unknown} · {t('evidence.sourceAccount', 'Source account ID (if supplied)')}: {observation?.source_account_id ?? unknown}</p>
      <dl className="grid gap-x-5 gap-y-3 sm:grid-cols-2 lg:grid-cols-3">{amountFields.map((field) => <div key={field}><dt className="text-muted-foreground">{t(`evidence.field.${field}`, label(field))}</dt><dd className="mt-1 break-all tabular-nums">{leg?.[field] ?? unknown}</dd></div>)}</dl>
      <div><h3 className="font-medium">{t('evidence.sourceRefs', 'Original source references')}</h3><ul className="mt-2 space-y-2 text-muted-foreground">{record.source_refs.map((ref, index) => <li key={`${ref.observation_ref}:${ref.leg_key}:${index}`} className="break-all">{ref.source} · {ref.source_local_id ?? unknown} · {ref.source_locator} · {ref.leg_key}</li>)}</ul></div>
      {!!observation?.source_fields && Object.keys(observation.source_fields).length > 0 && <dl className="grid gap-3 sm:grid-cols-2">{Object.entries(observation.source_fields).map(([key, value]) => <div key={key}><dt className="text-muted-foreground">{t('evidence.original', 'Original')} {label(key)}</dt><dd className="break-words">{value ?? unknown}</dd></div>)}</dl>}
      {observation?.historical_workspace_label && <p>{t('evidence.historicalWorkspace', 'Historical workspace label (context only)')}: {observation.historical_workspace_label}</p>}
      {!!record.reason_codes.length && <ul className="list-inside list-disc">{record.reason_codes.map((code) => <li key={code}>{label(code)}</li>)}</ul>}
      {!!record.conflicting_fields.length && <p className="text-warning-foreground">{t('evidence.conflictingFields', 'Conflicting fields')}: {record.conflicting_fields.map(label).join(', ')}</p>}
      {record.application_status === 'already_applied' && <p className="font-medium">{t('evidence.alreadyApplied', 'Already on the ledger. Linking supporting evidence adds 0 units and 0 ledger rows.')}</p>}
      {!!record.candidate_legs.length && <fieldset disabled={disabled} className="space-y-3">
        <legend className="mb-2 font-medium">{t('evidence.candidates', 'Review possible supporting legs')}</legend>
        {record.candidate_legs.map((candidate) => <div key={candidate.leg_id} className="space-y-2">
          <label className="flex items-start gap-2"><input type="checkbox" className="mt-1" checked={candidate.leg_id in selected} onChange={(event) => setSelected((previous) => { const next = { ...previous }; if (event.target.checked) next[candidate.leg_id] = ''; else delete next[candidate.leg_id]; return next })} /><span className="min-w-0 break-words">{candidate.asset_symbol ?? unknown} · {candidate.direction} · {candidate.quantity ?? unknown} · {candidate.event_date ?? unknown} · {candidate.classification}<span className="mt-1 block break-all text-xs text-muted-foreground">{t('evidence.event', 'Event')}: {candidate.event_id} · {t('evidence.leg', 'Leg')}: {candidate.leg_id}</span>{candidate.source_refs.map((ref, index) => <span key={index} className="mt-1 block break-all text-xs text-muted-foreground">{ref.source} · {ref.source_local_id ?? unknown} · {ref.source_locator}</span>)}</span></label>
          {candidate.leg_id in selected && <label className="flex max-w-md flex-col gap-1 pl-6">{t('evidence.allocation', 'Supported quantity (blank = full leg)')}<Input inputMode="decimal" value={selected[candidate.leg_id]} onChange={(event) => setSelected({ ...selected, [candidate.leg_id]: event.target.value })} /></label>}
        </div>)}
        <label className="flex max-w-xl flex-col gap-1">{t('evidence.linkReason', 'Evidence supporting this link')}<Input value={reason} maxLength={500} onChange={(event) => setReason(event.target.value)} /></label>
        <p className="text-muted-foreground">{t('evidence.linkEffect', 'Confirming a source link changes evidence associations only: 0 added ledger rows, 0 added units, and no basis adjustment. Similar amounts or times alone do not establish a match.')}</p>
        <Button variant="outline" disabled={disabled || !allocations.length || !allocationValid || !reason.trim()} onClick={() => onDecision({ ...decision, action: 'link', allocations, reason: reason.trim() })}>{t('evidence.confirmLink', 'Confirm source link')}</Button>
      </fieldset>}
      {record.application_status === 'eligible' && <div className="space-y-3">
        <p>{t('evidence.applyEffect', 'Applying this activity adds')}: {record.effects.ledger_rows} {t('evidence.ledgerRows', 'ledger rows')} · {t('evidence.unitsChange', 'Units change')}: {record.effects.units_delta ?? unknown} · {t('evidence.basisChange', 'Basis change')}: {record.effects.basis_delta ?? unknown}</p>
        <label className="flex items-start gap-2"><input type="checkbox" className="mt-1" checked={reviewApply} disabled={disabled} onChange={(event) => setReviewApply(event.target.checked)} /><span>{t('evidence.reviewApply', 'I reviewed the source, destination and financial effect above.')}</span></label>
        {needsSettlement && <label className="flex items-start gap-2"><input type="checkbox" className="mt-1" checked={settlementConfirmed} disabled={disabled} onChange={(event) => setSettlementConfirmed(event.target.checked)} /><span>{t('evidence.confirmSettlement', 'I verified this activity settled. The source did not report settlement status.')}</span></label>}
        <Button disabled={disabled || !reviewApply || (needsSettlement && !settlementConfirmed)} onClick={() => onDecision({ ...decision, action: 'apply', settlement_confirmed: settlementConfirmed })}>{t('evidence.apply', 'Apply supported activity')}</Button>
      </div>}
      {!!record.link_ids.length && <div className="space-y-3"><p className="text-muted-foreground">{t('evidence.unlinkEffect', 'Unlinking preserves original observations and any already-applied activity. It does not undo a financial import.')}</p>{record.link_ids.map((id, index) => {
        const linked = record.links?.find((item) => item.link_id === id)
        const source = linked?.leg.source_refs.map((ref) => ref.source_local_id ?? ref.source_locator).join(', ')
        return <div key={id} className="space-y-2">
          {linked && <p className="break-words">{t('evidence.linkedTarget', 'Linked activity')}: {linked.leg.asset_symbol ?? unknown} · {linked.quantity ?? linked.leg.quantity ?? unknown} · {source}<span className="mt-1 block break-all text-xs text-muted-foreground">{t('evidence.leg', 'Leg')}: {linked.leg.leg_id}</span></p>}
          <div className="flex flex-wrap items-center gap-2"><Button variant="outline" disabled={disabled} aria-label={source ? `${t('evidence.unlink', 'Unlink source')}: ${source}` : undefined} onClick={() => setUnlink(id)}>{t('evidence.unlink', 'Unlink source')} {record.link_ids.length > 1 ? index + 1 : ''}</Button>{unlink === id && <><Button variant="outline" disabled={disabled} onClick={() => onUnlink(id)}>{t('evidence.confirmUnlink', 'Confirm unlink; retain activity')}</Button><Button variant="ghost" onClick={() => setUnlink(null)}>{t('common.cancel')}</Button></>}</div>
        </div>
      })}</div>}
    </div>
  </details>
}
