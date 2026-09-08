import { useState, type FormEvent } from 'react'
import { useQuery } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { ownedTransfers } from '@/lib/api'
import { recovery } from '@/lib/recovery-api'
import { usePrivacyMode } from '@/hooks/use-privacy-mode'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { NativeSelect } from '@/components/ui/native-select'
import type { RecoveryEntryRead, RecoveryPackage, RecoveryReviewInput } from '@/types/recovery-evidence'

const selectClass = 'min-h-11 rounded-md border border-input bg-card px-3 text-base sm:text-sm'
export function RecoveryReviewForm({ entry, data, busy, onSave }: { entry: RecoveryEntryRead; data: RecoveryPackage; busy: boolean; onSave: (review: RecoveryReviewInput) => void }) {
  const { t } = useTranslation()
  const { mask } = usePrivacyMode()
  const [reviewKey] = useState(() => crypto.randomUUID())
  const [kind, setKind] = useState<RecoveryReviewInput['kind']>('relation')
  const [assertionKind, setAssertionKind] = useState('reported_cost')
  const [relatedGroup, setRelatedGroup] = useState(data.group_id)
  const wallets = useQuery({ queryKey: ['recovery-wallets', data.workspace_id], queryFn: ({ signal }) => recovery.wallets(data.workspace_id, signal) })
  const related = useQuery({ queryKey: ['recovery', data.workspace_id, { group_id: relatedGroup }], queryFn: ({ signal }) => recovery.list(data.workspace_id, { group_id: relatedGroup }, signal), retry: false })
  const transfers = useQuery({ queryKey: ['owned-transfers', data.workspace_id], queryFn: ({ signal }) => ownedTransfers.index(data.workspace_id, signal), enabled: kind === 'relation', retry: false })
  const label = (value: string) => t(`recovery.review.${value}`, value.replaceAll('_', ' '))
  const extra = related.data?.workspace_id === data.workspace_id && related.data.group_id === relatedGroup ? related.data : null
  const entries = [...new Map([...data.entries, ...(extra?.entries ?? [])].filter((item) => item.id).map((item) => [item.id, item])).values()]
  const reviews = [...new Map([...data.reviews, ...(extra?.reviews ?? [])].map((item) => [item.id, item])).values()]
  const title = (item: RecoveryEntryRead) => mask(`${item.source_group_name ?? t('recovery.unknown', 'Unknown')} · ${label(item.role)} · ${item.observation.source_local_id ?? item.observation.source_locator} · ${item.observation.legs.find((leg) => leg.key === item.leg_key)?.asset_symbol ?? t('recovery.unknown', 'Unknown')}`)
  const field = (name: string, text: string, required = false) => <div className="space-y-1.5" key={name}><Label htmlFor={`${entry.id}-${name}`}>{text}</Label><Input className="min-h-11" id={`${entry.id}-${name}`} name={name} maxLength={500} required={required} /></div>
  const select = (name: string, text: string, values: string[]) => <div className="space-y-1.5"><Label htmlFor={`${entry.id}-${name}`}>{text}</Label><NativeSelect className={selectClass} id={`${entry.id}-${name}`} name={name}>{values.map((value) => <option value={value} key={value}>{label(value)}</option>)}</NativeSelect></div>
  const submit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    if (busy || !entry.id) return
    const values = new FormData(event.currentTarget)
    const text = (name: string) => String(values.get(name) ?? '').trim() || null
    const list = (name: string) => String(values.get(name) ?? '').split(';').map((value) => value.trim()).filter(Boolean)
    onSave({
      key: `${reviewKey}:${kind}`, kind, entry_id: entry.id, target_entry_id: text('target_entry_id'), supersedes_id: text('supersedes_id'),
      relation_kind: text('relation_kind') as RecoveryReviewInput['relation_kind'], relation_state: text('relation_state') as RecoveryReviewInput['relation_state'],
      assertion_kind: text('assertion_kind') as RecoveryReviewInput['assertion_kind'], assertion_status: text('assertion_status') as RecoveryReviewInput['assertion_status'],
      value: text('value'), currency: text('currency'), field: text('field'), proposed_value: text('proposed_value'),
      source_locator: text('source_locator') ?? '', reason: text('reason') ?? '',
      supporting_observation_ids: values.getAll('supporting_observation_ids').map(String), required_entry_ids: values.getAll('required_entry_ids').map(String), required_review_ids: values.getAll('required_review_ids').map(String),
      missing_evidence: list('missing_evidence'), conflicting_fields: list('conflicting_fields'), owned_transfer_id: text('owned_transfer_id'),
      account_mapping_evidence: text('account_mapping_evidence'), timing_evidence: text('timing_evidence'), quantity_adjustment: text('quantity_adjustment'), adjustment_evidence: text('adjustment_evidence'),
    })
  }
  const observations = [...new Map(entries.map((item) => [item.observation_id, item.observation])).entries()].filter(([id]) => id)
  return <form onSubmit={submit} className="space-y-4 border-t border-border pt-4">
    <fieldset disabled={busy} className="space-y-4">
      <legend className="mb-3 font-semibold">{t('recovery.recordReview', 'Record a separate review')}</legend>
      <p className="max-w-prose text-sm text-muted-foreground">{t('recovery.reviewHelp', 'Original sources stay unchanged. A supported link or correction does not verify acquisition basis, a model allocation, or a filing assumption.')}</p>
      <div className="space-y-1.5"><Label htmlFor={`${entry.id}-kind`}>{t('recovery.reviewKind', 'Review type')}</Label><NativeSelect id={`${entry.id}-kind`} value={kind} onChange={(event) => setKind(event.target.value as typeof kind)} className={selectClass}>{['relation', 'assertion', 'correction', 'allocation'].map((value) => <option key={value} value={value}>{label(value)}</option>)}</NativeSelect></div>
      <div className="space-y-1.5"><Label htmlFor={`${entry.id}-related-wallet`}>{t('recovery.relatedWallet', 'Wallet containing related evidence / model inputs')}</Label><NativeSelect id={`${entry.id}-related-wallet`} value={relatedGroup} onChange={(event) => setRelatedGroup(event.target.value)} className={selectClass}>{wallets.data?.map((wallet) => <option key={wallet.id} value={wallet.id}>{mask(wallet.name)}</option>)}</NativeSelect>{related.isFetching && <p role="status" className="text-xs">{t('recovery.loadingRelated', 'Loading related evidence…')}</p>}{(related.isError || wallets.isError) && <div role="alert" className="text-sm"><p>{t('recovery.relatedError', 'Related evidence could not be loaded. Refresh before confirming this review.')}</p><Button type="button" variant="outline" onClick={() => { void related.refetch(); void wallets.refetch() }}>{t('common.retry', 'Retry')}</Button></div>}</div>
      <div key={kind} className="grid gap-4 sm:grid-cols-2">
        {kind === 'relation' && <>
          {select('relation_kind', t('recovery.relationship', 'Relationship'), ['claim_notice', 'notice_receipt', 'receipt_disposition', 'disposition_proceeds', 'candidate_acquisition', 'owned_transfer_reference'])}
          {select('relation_state', t('recovery.relationshipState', 'Relationship state'), ['candidate', 'confirmed', 'conflict', 'missing'])}
          <div className="space-y-1.5"><Label htmlFor={`${entry.id}-target_entry_id`}>{t('recovery.target', 'Related evidence')}</Label><NativeSelect className={selectClass} id={`${entry.id}-target_entry_id`} name="target_entry_id"><option value="">{t('recovery.missingTarget', 'No supported target / missing evidence')}</option>{entries.filter((item) => item.id !== entry.id).map((item) => <option key={item.id} value={item.id!}>{title(item)}</option>)}</NativeSelect></div>
          {field('account_mapping_evidence', t('recovery.accountCompatibility', 'Documented source / account compatibility'))}
          {field('timing_evidence', t('recovery.timeCompatibility', 'Documented time / precision compatibility'))}
          {field('quantity_adjustment', t('recovery.adjustment', 'Reported rounding / fee adjustment (signed amount)'))}
          {field('adjustment_evidence', t('recovery.adjustmentEvidence', 'Source supporting the rounding / fee adjustment'))}
          <div className="space-y-1.5"><Label htmlFor={`${entry.id}-owned_transfer_id`}>{t('recovery.ownedTransfer', 'Confirmed owned transfer (if supported)')}</Label><NativeSelect className={selectClass} id={`${entry.id}-owned_transfer_id`} name="owned_transfer_id"><option value="">{t('recovery.noTransfer', 'No confirmed transfer selected')}</option>{transfers.data?.workspace_id === data.workspace_id && transfers.data.transfers.filter((item) => item.status === 'confirmed').map((item) => <option key={item.id} value={item.id}>{mask(`${item.created_at} · ${item.request.reason}`)}</option>)}</NativeSelect>{transfers.isError && <p role="alert">{t('recovery.transferLoadError', 'Transfer references could not be loaded. Leave unresolved or retry.')} <Button type="button" variant="ghost" onClick={() => void transfers.refetch()}>{t('common.retry', 'Retry')}</Button></p>}</div>
        </>}
        {kind === 'assertion' && <>
          <div className="space-y-1.5"><Label htmlFor={`${entry.id}-assertion_kind`}>{t('recovery.assertionKind', 'Assertion meaning')}</Label><NativeSelect className={selectClass} id={`${entry.id}-assertion_kind`} name="assertion_kind" value={assertionKind} onChange={(event) => setAssertionKind(event.target.value)}>{['reported_cost', 'provisional_allocation', 'valuation', 'account_mapping', 'lot_mapping', 'accounting_assumption', 'filing_assertion'].map((value) => <option key={value} value={value}>{label(value)}</option>)}</NativeSelect></div>
          {assertionKind === 'filing_assertion' ? <div><input type="hidden" name="assertion_status" value="unverified" /><p role="status" className="text-sm text-muted-foreground">{t('recovery.filingUnverified', 'Filing assertion remains unverified. Selecting a workpaper or other source does not establish filed-record support or a tax determination.')}</p></div> : select('assertion_status', t('recovery.assertionStatus', 'Assertion status'), ['unverified', 'reported', 'modeled', 'supported', 'conflict', 'missing'])}
          {field('value', t('recovery.assertionValue', 'Asserted amount (blank means unknown)'))}
          {field('currency', t('recovery.assertionCurrency', 'Assertion currency'))}
          {field('field', t('recovery.namedAssumption', 'Named field / accounting assumption'))}
        </>}
        {kind === 'correction' && <>
          {field('field', t('recovery.correctionField', 'Source field / reference being corrected'), true)}
          {field('proposed_value', t('recovery.correctionValue', 'Proposed correction (original retained)'))}
          {select('assertion_status', t('recovery.correctionStatus', 'Correction status'), ['unverified', 'reported', 'supported', 'conflict', 'missing'])}
        </>}
        {kind === 'allocation' && <>
          {select('assertion_status', t('recovery.modelStatus', 'Model review status'), ['modeled', 'unverified', 'conflict', 'missing'])}
          {field('field', t('recovery.modelName', 'Model / allocation reference'), true)}
          {field('value', t('recovery.modelValue', 'Modeled amount (blank means unknown)'))}
          {field('currency', t('recovery.assertionCurrency', 'Assertion currency'))}
          <p className="text-sm text-muted-foreground">{t('recovery.modelHelp', 'This records a model for review. Required inputs, unresolved defects and controlling assumptions remain blockers; there is no finalization or tax write.')}</p>
        </>}
        {field('source_locator', t('recovery.reviewSource', 'Review source / document reference'), true)}
        {field('reason', t('recovery.reviewReason', 'Evidence and reason for this review'), true)}
        {field('missing_evidence', t('recovery.reviewMissing', 'Missing evidence / model defects (separate with semicolons)'))}
        {field('conflicting_fields', t('recovery.reviewConflicts', 'Disputed fields / mappings (separate with semicolons)'))}
        <div className="space-y-1.5"><Label htmlFor={`${entry.id}-supersedes_id`}>{t('recovery.supersedes', 'Previous review this replaces')}</Label><NativeSelect className={selectClass} id={`${entry.id}-supersedes_id`} name="supersedes_id"><option value="">{t('recovery.newReview', 'New separate review')}</option>{reviews.filter((review) => review.entry_id === entry.id && review.kind === kind && review.is_current).map((review) => <option key={review.id} value={review.id}>{mask(`${label(review.kind)} · ${review.field ?? review.relation_kind ?? review.assertion_kind ?? ''} · ${review.reason}`)}</option>)}</NativeSelect></div>
      </div>
      <fieldset className="space-y-2"><legend className="mb-2 text-sm font-medium">{t('recovery.supportingSources', 'Supporting retained sources')}</legend>{observations.map(([id, observation]) => <label className="flex min-h-11 items-start gap-3 break-words text-sm" key={id}><input className="mt-1" type="checkbox" name="supporting_observation_ids" value={id!} /><span>{mask(`${observation.provider} · ${observation.source_local_id ?? observation.source_locator}`)}</span></label>)}</fieldset>
      {kind === 'allocation' && <div className="grid gap-4 sm:grid-cols-2">
        <fieldset className="space-y-2"><legend className="mb-2 text-sm font-medium">{t('recovery.requiredInputs', 'Required source inputs')}</legend>{entries.map((item) => <label key={item.id} className="flex min-h-11 items-start gap-3 break-words text-sm"><input className="mt-1" type="checkbox" name="required_entry_ids" value={item.id!} /><span>{title(item)}</span></label>)}</fieldset>
        <fieldset className="space-y-2"><legend className="mb-2 text-sm font-medium">{t('recovery.requiredAssumptions', 'Required valuation / controlling assumption reviews')}</legend>{reviews.filter((item) => item.is_current).map((review) => <label key={review.id} className="flex min-h-11 items-start gap-3 break-words text-sm"><input className="mt-1" type="checkbox" name="required_review_ids" value={review.id} /><span>{mask(`${label(review.kind)} · ${review.field ?? review.assertion_kind ?? ''} · ${review.reason}`)}</span></label>)}</fieldset>
      </div>}
      <Button type="submit" disabled={busy || related.isFetching || related.isError || wallets.isError}>{t('recovery.saveReview', 'Save separate review')}</Button>
    </fieldset>
  </form>
}
