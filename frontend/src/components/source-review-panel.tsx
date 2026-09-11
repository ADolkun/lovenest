import { useEffect, useRef, useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { useWorkspace } from '@/contexts/workspace-context'
import { usePrivacyMode } from '@/hooks/use-privacy-mode'
import { assetErrorMessage } from '@/lib/api'
import { sourceReviews, type ReviewedSource, type SourceReviewEffects, type SourceReviewPreview, type SourceReviewRequest, type SourceSemantics } from '@/lib/source-review-api'
import type { EvidencePreview } from '@/types/investment-evidence'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { NativeSelect } from '@/components/ui/native-select'

const selectClass = 'min-h-11 w-full rounded-md border border-input bg-card px-3 text-base sm:text-sm'
const words = (value: string) => value.replaceAll('_', ' ')
const initialSemantics: SourceSemantics = { clock_role: 'reported_unknown', amount_field: 'total', amount_meaning: 'unknown', decimal_places: null }

export function SourceReviewPanel({ workspaceId, groupId, evidence, disabled }: { workspaceId: string; groupId: string; evidence: EvidencePreview; disabled: boolean }) {
  const { canWrite } = useWorkspace()
  const { privacyMode } = usePrivacyMode()
  if (privacyMode) return null
  return <SourceReviewContent key={`${workspaceId}:${groupId}:${canWrite}:${evidence.revision}`} workspaceId={workspaceId} groupId={groupId} evidence={evidence} disabled={disabled} canWrite={canWrite} />
}

function SemanticsFields({ title, value, onChange }: { title: string; value: SourceSemantics; onChange: (value: SourceSemantics) => void }) {
  const { t } = useTranslation()
  return <fieldset className="grid gap-3 sm:grid-cols-2">
    <legend className="mb-3 font-medium">{title}</legend>
    <label className="space-y-1"><span>{t('sourceReview.clockRole', 'Declared clock role')}</span><NativeSelect aria-label={`${title}: ${t('sourceReview.clockRole', 'Declared clock role')}`} className={selectClass} value={value.clock_role} onChange={event => onChange({ ...value, clock_role: event.target.value as SourceSemantics['clock_role'] })}>{['reported_unknown', 'execution', 'posted', 'settled'].map(role => <option key={role} value={role}>{t(`sourceReview.${role}`, words(role))}</option>)}</NativeSelect></label>
    <label className="space-y-1"><span>{t('sourceReview.amountField', 'Source amount field')}</span><NativeSelect aria-label={`${title}: ${t('sourceReview.amountField', 'Source amount field')}`} className={selectClass} value={value.amount_field} onChange={event => onChange({ ...value, amount_field: event.target.value as SourceSemantics['amount_field'], amount_meaning: 'unknown' })}>{['unit_price', 'subtotal', 'total', 'valuation_amount', 'fee', 'acquisition_basis'].map(field => <option key={field} value={field}>{t(`evidence.field.${field}`, words(field))}</option>)}</NativeSelect></label>
    <label className="space-y-1"><span>{t('sourceReview.amountMeaning', 'Declared amount meaning')}</span><NativeSelect aria-label={`${title}: ${t('sourceReview.amountMeaning', 'Declared amount meaning')}`} className={selectClass} value={value.amount_meaning} onChange={event => onChange({ ...value, amount_meaning: event.target.value as SourceSemantics['amount_meaning'] })}>{['unknown', 'execution_unit_price', 'execution_subtotal', 'fee_inclusive_total', 'valuation', 'reported_fee', 'reported_basis'].map(meaning => <option key={meaning} value={meaning}>{t(`sourceReview.${meaning}`, words(meaning))}</option>)}</NativeSelect></label>
    <label className="space-y-1"><span>{t('sourceReview.precision', 'Documented decimal places (optional)')}</span><Input aria-label={`${title}: ${t('sourceReview.precision', 'Documented decimal places (optional)')}`} type="number" min={0} max={128} value={value.decimal_places ?? ''} onChange={event => onChange({ ...value, decimal_places: event.target.value === '' ? null : Number(event.target.value) })} /></label>
  </fieldset>
}

function SourceFacts({ sources }: { sources: ReviewedSource[] }) {
  const { t } = useTranslation()
  const unknown = t('evidence.unknown', 'Unknown')
  return <div className="grid gap-6 sm:grid-cols-2">{sources.map((source, index) => <div key={`${source.leg_id}:${index}`} className="min-w-0 space-y-2">
    <h4 className="font-medium">{index === 0 ? t('sourceReview.selectedSource', 'Selected source') : t('sourceReview.existingSource', 'Existing activity source')}</h4>
    <p className="break-all">{source.observation.source} · {source.observation.source_local_id ?? unknown} · {source.observation.source_locator}</p>
    <p className="break-words">{t('evidence.reportedTime', 'Reported event time')}: {source.observation.event_time_raw ?? source.observation.event_at ?? source.observation.event_date ?? unknown} · {words(source.semantics.clock_role)} · {source.observation.time_precision}</p>
    <dl className="space-y-2">{Object.entries({ quantity: source.leg.quantity, [source.semantics.amount_field]: source.leg[source.semantics.amount_field], amount_meaning: words(source.semantics.amount_meaning), execution_currency: source.leg.execution_currency, original_source_fee: source.leg.fee, unit_price_origin: source.leg.unit_price_origin }).map(([key, value]) => <div key={key}><dt className="text-muted-foreground">{t(`sourceReview.${key}`, words(key))}</dt><dd className="break-all tabular-nums">{value ?? unknown}</dd></div>)}</dl>
  </div>)}</div>
}

function FinancialEffects({ effects }: { effects: SourceReviewEffects }) {
  const { t } = useTranslation()
  const unknown = t('evidence.unknown', 'Unknown')
  return <div className="space-y-3">
    {effects.before && effects.after ? <>
      <div className="grid gap-4 sm:grid-cols-2">{[['Before', effects.before, effects.cost_before], ['After', effects.after, effects.cost_after]].map(([title, entry, cost]) => {
        const row = entry as NonNullable<SourceReviewEffects['before']>
        return <div key={String(title)}><h4 className="mb-2 font-medium">{t(`sourceReview.${String(title).toLowerCase()}`, String(title))}</h4><dl className="space-y-2">{Object.entries({ quantity: row.quantity, unit_price: row.price, additional_ledger_fee: row.fee, acquisition_cost: cost, date: row.date }).map(([field, value]) => <div key={field}><dt className="text-muted-foreground">{t(`sourceReview.${field}`, words(field))}</dt><dd className="break-all tabular-nums">{String(value ?? unknown)}</dd></div>)}</dl></div>
      })}</div>
      <p className="break-all">{t('sourceReview.delta', 'Quantity / acquisition cost change')}: {effects.units_delta} / {effects.cost_delta}</p>
      <p>{effects.fee_treatment === 'included_no_additional_fee' ? t('sourceReview.inclusiveFee', 'The selected total includes fees. Additional ledger fee is 0; the original source fee amount remains as reported.') : t(`sourceReview.${effects.fee_treatment}`, words(effects.fee_treatment ?? 'unknown'))} {t('sourceReview.originalFee', 'Original source fee')}: {effects.original_source_fee ?? unknown}</p>
      {effects.provider_snapshot_preserved && <p>{t('sourceReview.providerPreserved', 'Provider quantity and valuation stay unchanged.')} {t('sourceReview.reportedQuantity', 'Reported quantity')}: {effects.reported_quantity ?? unknown}. {!effects.provider_quantity_matches && t('sourceReview.providerMismatch', 'Corrected history does not establish agreement with that quantity; financial caches stay unqualified.')}</p>}
    </> : <p>{t('sourceReview.noEconomics', 'This association changes no quantities, financial values or application ownership. Unequal source facts and existing conflicts remain visible.')}</p>}
    <p className="text-muted-foreground">{t('sourceReview.taxBoundary', 'Acquisition cost here is a performance input. This review does not establish complete tax basis, history, funding or lot elections.')}</p>
  </div>
}

function SourceReviewContent({ workspaceId, groupId, evidence, disabled, canWrite }: { workspaceId: string; groupId: string; evidence: EvidencePreview; disabled: boolean; canWrite: boolean }) {
  const { t } = useTranslation()
  const queryClient = useQueryClient()
  const queryKey = ['investment-source-reviews', workspaceId, groupId]
  const saved = useQuery({ queryKey, queryFn: ({ signal }) => sourceReviews.list(workspaceId, groupId, signal), retry: false })
  const [sourceId, setSourceId] = useState('')
  const [targetId, setTargetId] = useState('')
  const [sourceMeaning, setSourceMeaning] = useState<SourceSemantics>(initialSemantics)
  const [targetMeaning, setTargetMeaning] = useState<SourceSemantics>(initialSemantics)
  const [sameExecution, setSameExecution] = useState(false)
  const [reason, setReason] = useState('')
  const [preview, setPreview] = useState<SourceReviewPreview | null>(null)
  const [reviewed, setReviewed] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const generation = useRef(0)
  const mutationLock = useRef(false)
  useEffect(() => () => { generation.current += 1 }, [])
  const validScope = saved.data?.target.workspace_id === workspaceId && saved.data.target.group_id === groupId
  const packageData = validScope ? saved.data : undefined
  const unavailable = disabled || busy || saved.isFetching || !packageData || saved.isError
  const blocked = unavailable || !canWrite
  const observations = new Map(evidence.observations.map(row => [row.reference, row]))
  const choices = packageData?.legs.map(row => ({ ...row, observation: observations.get(row.observation_ref) })) ?? []
  const superseded = new Set(packageData?.reviews.map(row => row.supersedes_id))
  function clearPreview() { generation.current += 1; setPreview(null); setReviewed(false); setError(null); setNotice(null) }
  function checkScope(value: { target: EvidencePreview['target'] }) {
    if (value.target.workspace_id !== workspaceId || value.target.group_id !== groupId) throw new Error(t('evidence.destinationChanged', 'The preview destination changed. Refresh and review the selected wallet again.'))
  }
  async function runPreview(request: SourceReviewRequest) {
    if (blocked) return
    clearPreview()
    const current = generation.current
    setBusy(true)
    try {
      const value = await sourceReviews.preview(workspaceId, request)
      if (current !== generation.current) return
      checkScope(value)
      setPreview(value)
    } catch (failure) {
      if (current === generation.current) setError(assetErrorMessage(failure, t('sourceReview.previewError', 'Could not preview this review. Refresh and try again.')))
    } finally { if (current === generation.current) setBusy(false) }
  }
  async function applyPreview() {
    if (blocked || mutationLock.current || !reviewed || !preview?.supported) return
    mutationLock.current = true
    const current = generation.current
    setBusy(true)
    try {
      const value = await sourceReviews.confirm(workspaceId, preview)
      if (current !== generation.current) return
      checkScope(value)
      queryClient.setQueryData(queryKey, value)
      setPreview(null); setReviewed(false)
      setNotice(t('sourceReview.saved', 'Review saved. Original source facts and the audit remain retained.'))
      const keys = ['investment-evidence', 'investment-timeline', 'investment-timeline-event', 'investment-timeline-source']
      if (preview.request.action === 'correct' || preview.request.action === 'reverse') keys.push('assets', 'asset-groups', 'asset-transactions', 'asset-tax-lots', 'asset-values', 'asset-trend', 'portfolio-trend', 'dashboard')
      for (const key of keys) void queryClient.invalidateQueries({ queryKey: [key] })
    } catch (failure) {
      if (current !== generation.current) return
      setPreview(null); setReviewed(false)
      setError(assetErrorMessage(failure, t('sourceReview.applyError', 'The review could not be applied. Refresh and preview again.')))
      await saved.refetch()
    } finally { mutationLock.current = false; if (current === generation.current) setBusy(false) }
  }
  async function exportReviews() {
    if (unavailable || !packageData) return
    const current = generation.current
    setBusy(true)
    try {
      const blob = await sourceReviews.export(workspaceId, groupId, packageData.revision)
      if (current !== generation.current) return
      const url = URL.createObjectURL(blob)
      const link = document.createElement('a'); link.href = url; link.download = 'source-reviews.json'; link.click(); URL.revokeObjectURL(url)
    } catch (failure) { if (current === generation.current) setError(assetErrorMessage(failure, t('sourceReview.exportError', 'Refresh the source reviews before exporting.'))) }
    finally { if (current === generation.current) setBusy(false) }
  }
  return <details className="border-y border-border py-4" onToggle={event => { if (!event.currentTarget.open && !busy) clearPreview() }}>
    <summary className="cursor-pointer py-2 font-semibold focus-visible:outline-2 focus-visible:outline-ring">{t('sourceReview.title', 'Source meanings and acquisition corrections')}</summary>
    <div className="mt-4 space-y-6 text-sm">
      <p className="max-w-prose text-muted-foreground">{t('sourceReview.intro', 'Record what different clocks and amounts mean, then separately preview a supported correction to one existing acquisition. Sales, transfers, recovery dependencies and unsupported values require separate review.')}</p>
      {saved.isPending && <p role="status">{t('sourceReview.loading', 'Loading source reviews…')}</p>}
      {(saved.isError || error) && <div role="alert" className="space-y-2 text-warning-foreground"><p>{error ?? t('sourceReview.loadError', 'Source reviews could not be loaded.')}</p><Button variant="outline" disabled={busy} onClick={() => { clearPreview(); void saved.refetch() }}>{t('evidence.refreshReview', 'Refresh review')}</Button></div>}
      {saved.data && !validScope && <p role="alert">{t('evidence.destinationChanged', 'The preview destination changed. Refresh and review the selected wallet again.')}</p>}
      {notice && <p role="status">{notice}</p>}
      {packageData && canWrite && <fieldset disabled={blocked} className="space-y-4">
        <legend className="mb-3 font-medium">{t('sourceReview.associate', 'Document a sourced association')}</legend>
        <div className="grid gap-4 sm:grid-cols-2">{[['Selected source', sourceId, setSourceId], ['Existing activity source', targetId, setTargetId]].map(([title, value, setter]) => <label className="min-w-0 space-y-1" key={String(title)}><span>{t(`sourceReview.${title === 'Selected source' ? 'selectedSource' : 'existingSource'}`, String(title))}</span><NativeSelect className={selectClass} value={String(value)} onChange={event => { clearPreview(); (setter as (value: string) => void)(event.target.value) }}><option value="">{t('sourceReview.chooseSource', 'Choose retained source')}</option>{choices.map(row => <option key={row.leg_id} value={row.leg_id}>{row.observation?.source_local_id ?? row.observation_ref} · {row.observation?.legs.find(leg => leg.key === row.leg_key)?.asset_symbol ?? t('evidence.unknown', 'Unknown')} · {row.leg_key}{row.transaction_id ? ' · applied' : ''}</option>)}</NativeSelect></label>)}</div>
        <SemanticsFields title={t('sourceReview.selectedSource', 'Selected source')} value={sourceMeaning} onChange={value => { clearPreview(); setSourceMeaning(value) }} />
        <SemanticsFields title={t('sourceReview.existingSource', 'Existing activity source')} value={targetMeaning} onChange={value => { clearPreview(); setTargetMeaning(value) }} />
        <label className="flex items-start gap-2"><input type="checkbox" className="mt-1" checked={sameExecution} onChange={event => { clearPreview(); setSameExecution(event.target.checked) }} /><span>{t('sourceReview.sameExecution', 'I reviewed source identifiers and account evidence that these records describe the same acquisition. Similar amounts or times alone are insufficient.')}</span></label>
        <label className="block space-y-1"><span>{t('sourceReview.reason', 'Source evidence and reason')}</span><Input value={reason} maxLength={500} onChange={event => { clearPreview(); setReason(event.target.value) }} /></label>
        <Button variant="outline" disabled={blocked || !sourceId || !targetId || sourceId === targetId || !reason.trim()} onClick={() => void runPreview({ group_id: groupId, request_key: crypto.randomUUID(), action: 'associate', source_leg_id: sourceId, target_leg_id: targetId, source_semantics: sourceMeaning, target_semantics: targetMeaning, same_execution_reviewed: sameExecution, reason: reason.trim() })}>{t('sourceReview.previewAssociation', 'Preview association')}</Button>
      </fieldset>}
      {preview && <section aria-label={t('sourceReview.preview', 'Source review preview')} className="space-y-5 border-y border-border py-5">
        <h3 className="font-semibold">{t(`sourceReview.preview_${preview.request.action}`, `${words(preview.request.action)} preview`)} · {preview.target.workspace_name} · {preview.target.group_name}</h3>
        <SourceFacts sources={preview.sources} /><FinancialEffects effects={preview.effects} />
        {!preview.supported && <div role="alert"><p>{t('sourceReview.unsupported', 'This correction is not supported:')}</p><ul className="list-inside list-disc">{preview.blockers.map(reason => <li key={reason}>{t(`sourceReview.blocker.${reason}`, words(reason))}</li>)}</ul></div>}
        {preview.supported && <label className="flex items-start gap-2"><input type="checkbox" className="mt-1" checked={reviewed} disabled={blocked} onChange={event => setReviewed(event.target.checked)} /><span>{t('sourceReview.reviewed', 'I reviewed the source meanings, destination and exact effects above.')}</span></label>}
        <div className="flex flex-wrap gap-2"><Button disabled={blocked || !preview.supported || !reviewed} onClick={() => void applyPreview()}>{preview.request.action === 'correct' ? t('sourceReview.applyCorrection', 'Apply acquisition correction') : preview.request.action === 'reverse' ? t('sourceReview.applyReversal', 'Apply correction reversal') : t('sourceReview.saveAssociation', 'Save source review')}</Button><Button variant="outline" disabled={busy} onClick={clearPreview}>{t('common.cancel', 'Cancel')}</Button></div>
      </section>}
      {packageData && <section className="space-y-4" aria-label={t('sourceReview.history', 'Source review audit')}>
        <div className="flex flex-wrap items-center justify-between gap-3"><h3 className="font-medium">{t('sourceReview.history', 'Source review audit')}</h3><Button variant="outline" disabled={unavailable || !packageData.reviews.length} onClick={() => void exportReviews()}>{t('sourceReview.export', 'Export reviews (JSON)')}</Button></div>
        {!packageData.reviews.length && <p className="text-muted-foreground">{t('sourceReview.empty', 'No source interpretations or corrections are recorded for this wallet.')}</p>}
        {[...packageData.reviews].reverse().map(review => <details key={review.id} className="border-t border-border pt-3"><summary className="cursor-pointer py-2 focus-visible:outline-2 focus-visible:outline-ring">{t(`sourceReview.${review.payload.request.action}`, words(review.payload.request.action))} · {review.created_at} · {superseded.has(review.id) ? t('sourceReview.superseded', 'Superseded') : t('sourceReview.retained', 'Retained')}</summary><div className="space-y-4 py-3"><p className="break-words">{review.payload.request.reason}</p><SourceFacts sources={review.payload.sources} /><FinancialEffects effects={review.payload.effects} />{canWrite && !superseded.has(review.id) && <div className="flex flex-wrap gap-2">{(review.payload.request.action === 'associate' ? ['correct', 'revoke'] as const : review.payload.request.action === 'correct' ? ['reverse'] as const : []).map(action => <Button key={action} variant="outline" disabled={blocked} onClick={() => void runPreview({ group_id: groupId, request_key: crypto.randomUUID(), action, review_id: review.id, reason: review.payload.request.reason })}>{action === 'correct' ? t('sourceReview.previewCorrection', 'Preview acquisition correction') : action === 'reverse' ? t('sourceReview.previewReversal', 'Preview correction reversal') : t('sourceReview.previewRevoke', 'Preview association revocation')}</Button>)}</div>}</div></details>)}
      </section>}
    </div>
  </details>
}
