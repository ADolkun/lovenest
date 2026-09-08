import { useEffect, useRef, useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { Link, useSearchParams } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import { assetErrorMessage, ownedTransfers } from '@/lib/api'
import { formatExactDecimal } from '@/lib/format'
import { useWorkspace } from '@/contexts/workspace-context'
import { usePrivacyMode } from '@/hooks/use-privacy-mode'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { NativeSelect } from '@/components/ui/native-select'
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import type { AssetGroup } from '@/types'
import type { HoldingEffect, IncidentCreate, LotSelection, MovementApplication, MovementPreview, MovementRequest, MovementSelection, RetainedMovement, TransferIndex, TransferLot, TransferPreview, TransferRead, TransferRequest } from '@/types/owned-transfers'

const selectClass = 'min-h-11 w-full min-w-0 rounded-md border border-input bg-card px-3 py-2 text-base sm:text-sm'
const positiveDecimal = (value: string) => /^\d+(\.\d+)?$/.test(value) && /[1-9]/.test(value)
const assetKey = (movement: RetainedMovement) => [movement.chain, movement.token_program, movement.token_address].filter(Boolean).join(':')
const isFee = (movement: RetainedMovement) => movement.classification === 'fee' || !!movement.quantity_role?.includes('fee')

function Reasons({ reasons }: { reasons: string[] }) {
  const { t } = useTranslation()
  return reasons.length > 0 && <ul className="list-inside list-disc space-y-1 text-sm text-warning-foreground">{[...new Set(reasons)].map((reason) => <li key={reason}>{t(`ownedTransfers.reasons.${reason}`, reason.replaceAll('_', ' '))}</li>)}</ul>
}

function ReviewError({ error, retry, reviewIndex }: { error: unknown; retry?: () => void; reviewIndex?: TransferIndex }) {
  const { t } = useTranslation()
  const { mask, privacyMode } = usePrivacyMode()
  if (!error) return null
  const detail = (error as { response?: { data?: { detail?: { dependencies?: unknown[]; reason_codes?: string[] } } } }).response?.data?.detail
  return <div role="alert" className="space-y-3 rounded-lg border border-warning/40 bg-warning/10 p-4 text-sm">
    <p>{privacyMode ? t('ownedTransfers.privateError', 'This operation could not finish. Refresh the saved review; source details are masked in privacy mode.') : assetErrorMessage(error, t('ownedTransfers.reviewFailed', 'The review could not finish. Refresh the saved state before trying again.'))}</p>
    <Reasons reasons={detail?.reason_codes ?? []} />
    {!!detail?.dependencies?.length && <div className="space-y-2"><p>{t('ownedTransfers.dependencies', 'Resolve these dependent records before changing this decision:')}</p><ul className="space-y-2">{detail.dependencies.map((dependency, index) => {
      const entry = typeof dependency === 'object' && dependency !== null ? dependency as Record<string, unknown> : { id: dependency }
      const id = String(entry.id ?? entry.transfer_id ?? entry.application_id ?? entry.transaction_id ?? '')
      const application = reviewIndex?.applications.find((item) => item.id === id)
      const transfer = entry.transfer_id || entry.type === 'transfer' || reviewIndex?.transfers.some((item) => item.id === id)
      return <li className="break-all" key={index}>{transfer || application ? <Link className="inline-flex min-h-11 items-center underline underline-offset-4" to={`/assets?${new URLSearchParams({ tab: 'activity', activity: 'transfers', ...(transfer ? { transfer: id } : { movement: application!.request.leg_id }) })}`}>{transfer ? t('ownedTransfers.openDependency', 'Open dependent transfer') : t('ownedTransfers.openDependentMovement', 'Open dependent movement')} {mask(id)}</Link> : <>{mask(Object.values(entry).map(String).join(' · '))} · <Link className="underline underline-offset-4" to="/assets?tab=activity">{t('ownedTransfers.reviewRelatedActivity', 'Review related activity')}</Link></>}</li>
    })}</ul></div>}
    {retry && <Button variant="outline" onClick={retry}>{t('ownedTransfers.refresh', 'Refresh review')}</Button>}
  </div>
}

function MovementFacts({ movement }: { movement: RetainedMovement }) {
  const { t } = useTranslation()
  const { mask } = usePrivacyMode()
  const fields = {
    quantity: movement.quantity, chain: movement.chain, token_address: movement.token_address, token_program: movement.token_program,
    source_address: movement.source_address, destination_address: movement.destination_address,
    source_owner: movement.source_owner, destination_owner: movement.destination_owner,
    raw_units: movement.raw_units, decimals: movement.decimals, quantity_role: movement.quantity_role, fee_payer: movement.fee_payer,
    event_time: movement.event_at ?? movement.event_date, time_precision: movement.time_precision,
    provider_status: movement.provider_status, network_status: movement.network_status, settlement_status: movement.settlement_status,
    transaction_ref: movement.transaction_ref, leg_ref: movement.leg_ref, source: movement.source, source_local_id: movement.source_local_id, source_locator: movement.source_locator,
  }
  return <div className="space-y-3">
    <dl className="grid min-w-0 gap-x-6 gap-y-3 sm:grid-cols-2">{Object.entries(fields).map(([field, value]) => <div className="min-w-0" key={field}><dt className="text-sm text-muted-foreground">{t(`ownedTransfers.fields.${field}`, field.replaceAll('_', ' '))}</dt><dd className="break-all text-sm tabular-nums">{value == null ? t('evidence.unknown', 'Unknown') : mask(field === 'quantity' ? formatExactDecimal(String(value)) : String(value))}</dd></div>)}</dl>
    <Reasons reasons={movement.reason_codes} />
  </div>
}

function LotLineage({ lots, index, title }: { lots: TransferLot[]; index: TransferIndex; title: string }) {
  const { t } = useTranslation()
  const { mask } = usePrivacyMode()
  if (!lots.length) return null
  return <details><summary className="cursor-pointer py-2 text-sm font-medium">{title}</summary><ul className="space-y-3 text-sm">{lots.map((lot) => {
    const holding = index.holdings.find((item) => item.id === lot.asset_id)
    return <li className="space-y-1 border-t border-border pt-3" key={lot.lot_id}>
      <p className="break-words">{mask(holding?.name ?? lot.asset_id)} · {holding?.currency}</p>
      <p className="break-words">{t('ownedTransfers.acquired', 'Acquired')}: {lot.acquired ?? t('evidence.unknown', 'Unknown')} · {t('ownedTransfers.quantity', 'Quantity')}: {mask(formatExactDecimal(lot.quantity))} · {t('ownedTransfers.originalCost', 'Original acquisition cost')}: {lot.acquisition_cost == null ? t('evidence.unknown', 'Unknown') : mask(formatExactDecimal(lot.acquisition_cost))}</p>
      <p className="break-all text-muted-foreground">{t('ownedTransfers.sourceLot', 'Source lot')}: {mask(lot.lot_id)}</p>
      <p className="break-all text-muted-foreground">{t('ownedTransfers.originalAcquisition', 'Original acquisition')}: {lot.root_transaction_id == null ? t('evidence.unknown', 'Unknown') : mask(lot.root_transaction_id)}</p>
      <div className="flex flex-wrap gap-3">{lot.source_leg_id && <Link className="break-all underline underline-offset-4" to={`/assets?${new URLSearchParams({ tab: 'activity', activity: 'transfers', movement: lot.source_leg_id })}`}>{t('ownedTransfers.openSource', 'Open source movement')} {mask(lot.source_leg_id)}</Link>}{lot.lineage.map((id) => <Link key={id} className="break-all underline underline-offset-4" to={`/assets?${new URLSearchParams({ tab: 'activity', activity: 'transfers', transfer: id })}`}>{t('ownedTransfers.openHop', 'Open transfer')} {mask(id)}</Link>)}</div>
      <Reasons reasons={lot.missing_links} />
    </li>
  })}</ul></details>
}

function Effects({ effects, index }: { effects: HoldingEffect[]; index: TransferIndex }) {
  const { t } = useTranslation()
  const { mask } = usePrivacyMode()
  return <div className="space-y-5">{effects.map((effect) => {
    const holding = index.holdings.find((item) => item.id === effect.asset_id)
    const fields = {
      replay_quantity: effect.quantity, provider_or_current_quantity: holding?.units,
      known_basis_quantity: effect.known_basis_quantity, unknown_basis_quantity: effect.unknown_basis_quantity,
      known_acquisition_cost: effect.known_acquisition_cost, performance_basis: effect.performance_basis,
      realized_gain: effect.realized_gain, known_realized_gain: effect.known_realized_gain, unknown_disposition_quantity: effect.unknown_disposition_quantity,
    }
    return <section className="space-y-3 border-t border-border pt-4" key={effect.asset_id}>
      <h4 className="font-medium">{mask(holding?.name ?? effect.asset_id)} {holding?.currency}</h4>
      <dl className="grid gap-3 sm:grid-cols-2">{Object.entries(fields).map(([field, value]) => <div key={field}><dt className="text-sm text-muted-foreground">{t(`ownedTransfers.fields.${field}`, field.replaceAll('_', ' '))}</dt><dd className="break-all text-sm tabular-nums">{value == null ? t('evidence.unknown', 'Unknown') : mask(formatExactDecimal(value))}</dd></div>)}</dl>
      <p className="text-sm text-muted-foreground">{effect.basis_complete ? t('ownedTransfers.basisSupported', 'Basis supported for this scope') : t('ownedTransfers.basisUnknown', 'Acquisition basis incomplete / unknown')} · {effect.settlement_complete ? t('ownedTransfers.settledEvidence', 'Settled movement evidence') : t('ownedTransfers.principalUnknown', 'Movement settlement unresolved')}</p>
      <Reasons reasons={effect.missing_links} />
      <LotLineage lots={effect.lots} index={index} title={t('ownedTransfers.lineage', 'Original acquisition lineage')} />
    </section>
  })}</div>
}

function LotPicker({ workspaceId, revision, assetId, legId, value, onChange, disabled }: { workspaceId: string; revision: string; assetId: string; legId: string; value: LotSelection[]; onChange: (value: LotSelection[]) => void; disabled: boolean }) {
  const { t } = useTranslation()
  const { mask, privacyMode } = usePrivacyMode()
  const lots = useQuery({ queryKey: ['owned-transfer-lots', workspaceId, assetId, legId, revision], queryFn: ({ signal }) => ownedTransfers.lots(workspaceId, assetId, legId, signal), enabled: !!assetId && !!legId, retry: false })
  if (!assetId) return <p className="text-sm text-muted-foreground">{t('ownedTransfers.chooseHoldingFirst', 'Choose the holding to inspect available source lots.')}</p>
  return <fieldset disabled={disabled} className="min-w-0 space-y-3">
    <legend className="mb-2 font-medium">{t('ownedTransfers.selectLots', 'Select the exact source lots')}</legend>
    <p className="max-w-prose text-sm text-muted-foreground">{t('ownedTransfers.lotsHelp', 'These quantities are available before this movement. Select only the supported portions; this records lineage without making a tax election.')}</p>
    {lots.isPending ? <p role="status">{t('common.loading', 'Loading…')}</p> : lots.isError ? <ReviewError error={lots.error} retry={() => { void lots.refetch() }} /> : <>
      <Reasons reasons={lots.data?.missing_links ?? []} />
      {!lots.data?.lots.length && <p className="text-sm text-muted-foreground">{t('ownedTransfers.noLots', 'No supported source inventory is available. Review acquisitions or an opening-history boundary in Source review.')}</p>}
      {lots.data?.lots.map((lot, index) => {
        const selected = value.find((item) => item.lot_id === lot.lot_id)
        const id = `lot-${legId}-${index}`
        return <div className="space-y-2 border-t border-border pt-3" key={lot.lot_id}>
          <label className="flex min-h-11 items-start gap-3 text-sm"><input className="mt-1" type="checkbox" checked={!!selected} onChange={(event) => onChange(event.target.checked ? [...value, { lot_id: lot.lot_id, quantity: '' }] : value.filter((item) => item.lot_id !== lot.lot_id))} /><span className="min-w-0"><strong>{t('ownedTransfers.lot', 'Lot')} {index + 1}</strong> · {lot.acquired ?? t('ownedTransfers.unknownDate', 'Acquisition date unknown')}<span className="mt-1 block break-all">{t('ownedTransfers.available', 'Available')}: {mask(formatExactDecimal(lot.quantity))} · {t('ownedTransfers.originalCost', 'Original acquisition cost')}: {lot.acquisition_cost == null ? t('evidence.unknown', 'Unknown') : mask(formatExactDecimal(lot.acquisition_cost))}</span><span className="mt-1 block break-all text-muted-foreground">{mask(lot.lot_id)}</span></span></label>
          {selected && <div className="max-w-sm space-y-1"><Label htmlFor={id}>{t('ownedTransfers.selectedQuantity', 'Selected quantity')} · {t('ownedTransfers.lot', 'Lot')} {index + 1}</Label><Input id={id} type={privacyMode ? 'password' : 'text'} inputMode="decimal" autoComplete="off" value={selected.quantity} aria-invalid={!positiveDecimal(selected.quantity)} onChange={(event) => onChange(value.map((item) => item.lot_id === lot.lot_id ? { ...item, quantity: event.target.value } : item))} /><p className="text-xs text-muted-foreground">{t('ownedTransfers.decimalHint', 'Enter an exact positive decimal; use a dot for the decimal separator.')}</p></div>}
          <Reasons reasons={lot.missing_links} />
        </div>
      })}
    </>}
  </fieldset>
}

function OwnershipPicker({ index, movement, assetId, value, onChange, disabled, refresh, title }: { index: TransferIndex; movement: RetainedMovement; assetId: string; value: string; onChange: (id: string) => void; disabled: boolean; refresh: () => Promise<unknown>; title: string }) {
  const { t } = useTranslation()
  const { mask, privacyMode } = usePrivacyMode()
  const [owner, setOwner] = useState('')
  const [address, setAddress] = useState('')
  const [account, setAccount] = useState('')
  const [since, setSince] = useState('')
  const [until, setUntil] = useState('')
  const [reason, setReason] = useState('')
  const [reviewed, setReviewed] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<unknown>(null)
  const [revoking, setRevoking] = useState(false)
  const alive = useRef(true)
  const lock = useRef(false)
  useEffect(() => { alive.current = true; return () => { alive.current = false } }, [])
  const groupId = index.holdings.find((holding) => holding.id === assetId)?.group_id
  const choices = index.ownership.filter((item) => item.group_id === groupId && !item.revoked_at)
  const selected = choices.find((item) => item.id === value)
  const id = `ownership-${movement.leg_id}`
  const save = async (revoke = false) => {
    if (disabled || lock.current || !groupId || !movement.chain) return
    lock.current = true; setBusy(true); setError(null)
    try {
      const result = revoke ? await ownedTransfers.revokeOwnership(index.workspace_id, value, index.revision) : await ownedTransfers.createOwnership(index.workspace_id, { group_id: groupId, beneficial_owner: owner.trim(), chain: movement.chain, address: address.trim() || null, source_account_id: account.trim() || null, valid_from: since || null, valid_until: until || null, reason: reason.trim(), evidence_observation_ids: [movement.observation_id] })
      if (!alive.current) return
      if (result.workspace_id !== index.workspace_id) throw new Error('Ownership destination changed')
      await refresh()
      if (alive.current) { onChange(revoke ? '' : result.id); setReviewed(false); setRevoking(false) }
    } catch (failure) { if (alive.current) setError(failure) }
    finally { lock.current = false; if (alive.current) setBusy(false) }
  }
  return <fieldset disabled={disabled || busy} className="space-y-3">
    <legend className="mb-2 text-sm font-medium">{title}</legend>
    <Label htmlFor={id}>{t('ownedTransfers.ownershipMapping', 'Confirmed ownership mapping')}</Label>
    <NativeSelect className={selectClass} id={id} value={value} onChange={(event) => { onChange(event.target.value); setRevoking(false) }}><option value="">{t('ownedTransfers.chooseOwnership', 'Select an explicit ownership assertion')}</option>{choices.map((item) => <option value={item.id} key={item.id}>{mask(item.beneficial_owner)} · {mask(item.address ?? item.source_account_id ?? '')} · {item.valid_from ?? t('evidence.unknown', 'Unknown')} – {item.valid_until ?? t('ownedTransfers.openEnded', 'Open ended')}</option>)}</NativeSelect>
    {selected && <p className="break-words text-sm text-muted-foreground">{mask(selected.reason)}</p>}
    {!disabled && <details className="space-y-3"><summary className="cursor-pointer py-2 text-sm font-medium">{t('ownedTransfers.recordOwnership', 'Record an ownership assertion')}</summary>
      <p className="text-sm text-muted-foreground">{t('ownedTransfers.ownershipHelp', 'Use the same owner identifier only for accounts with the same beneficial owner. Record the address or source account and the period supported by your evidence. Watching an address does not establish ownership.')}</p>
      {!groupId || !movement.chain ? <p role="status" className="text-sm">{t('ownedTransfers.mappingMissing', 'Choose a holding in a wallet and resolve the movement network before recording ownership.')}</p> : <>
        <div className="grid gap-3 sm:grid-cols-2">{[['owner', t('ownedTransfers.owner', 'Owner identifier'), owner, setOwner], ['address', t('ownedTransfers.address', 'Owned address'), address, setAddress], ['account', t('ownedTransfers.sourceAccount', 'Source account ID'), account, setAccount]] .map(([key, text, val, setter]) => <div className="space-y-1" key={String(key)}><Label htmlFor={`${id}-${key}`}>{String(text)}</Label><Input id={`${id}-${key}`} type={privacyMode ? 'password' : 'text'} value={String(val)} maxLength={key === 'owner' ? 128 : 255} onChange={(event) => (setter as (value: string) => void)(event.target.value)} /></div>)}</div>
        <div className="grid gap-3 sm:grid-cols-2"><div className="space-y-1"><Label htmlFor={`${id}-from`}>{t('ownedTransfers.validFrom', 'Ownership supported from')}</Label><Input id={`${id}-from`} type="date" value={since} onChange={(event) => setSince(event.target.value)} /></div><div className="space-y-1"><Label htmlFor={`${id}-until`}>{t('ownedTransfers.validUntil', 'Ownership supported until')}</Label><Input id={`${id}-until`} type="date" value={until} onChange={(event) => setUntil(event.target.value)} /></div></div>
        <Label htmlFor={`${id}-reason`}>{t('ownedTransfers.ownershipSource', 'Evidence supporting ownership')}</Label><Input id={`${id}-reason`} type={privacyMode ? 'password' : 'text'} maxLength={500} value={reason} onChange={(event) => setReason(event.target.value)} />
        <label className="flex min-h-11 items-start gap-3 text-sm"><input className="mt-1" type="checkbox" checked={reviewed} onChange={(event) => setReviewed(event.target.checked)} /><span>{t('ownedTransfers.ownershipConfirmed', 'I reviewed this account or address, beneficial owner and ownership period.')}</span></label>
        <Button disabled={!reviewed || !owner.trim() || (!address.trim() && !account.trim()) || !reason.trim() || (!!since && !!until && since > until)} onClick={() => { void save() }}>{t('ownedTransfers.saveOwnership', 'Save ownership assertion')}</Button>
      </>}
    </details>}
    {selected && !disabled && <div className="flex flex-wrap gap-2"><Button variant="ghost" onClick={() => setRevoking(!revoking)}>{t('ownedTransfers.revokeOwnership', 'Revoke ownership assertion')}</Button>{revoking && <Button variant="outline" onClick={() => { void save(true) }}>{t('ownedTransfers.confirmRevoke', 'Confirm revocation')}</Button>}</div>}
    <ReviewError error={error} reviewIndex={index} />
  </fieldset>
}

function FeeSelection({ index, movement, value, onChange, disabled, refresh }: { index: TransferIndex; movement: RetainedMovement; value: MovementSelection; onChange: (selection: MovementSelection) => void; disabled: boolean; refresh: () => Promise<unknown> }) {
  const { t } = useTranslation()
  const { mask } = usePrivacyMode()
  return <div className="space-y-4 border-t border-border pt-4">
    <p className="break-all text-sm">{t('ownedTransfers.feePayer', 'Reported fee payer')}: {movement.fee_payer == null ? t('evidence.unknown', 'Unknown') : mask(movement.fee_payer)} · {mask(formatExactDecimal(movement.quantity ?? t('evidence.unknown', 'Unknown')))} · {mask(assetKey(movement))}</p>
    <Label htmlFor={`fee-holding-${movement.leg_id}`}>{t('ownedTransfers.feeHolding', 'Fee payer holding')}</Label><NativeSelect id={`fee-holding-${movement.leg_id}`} className={selectClass} value={value.asset_id} disabled={disabled} onChange={(event) => onChange({ ...value, asset_id: event.target.value, ownership_id: '', allocations: [] })}><option value="">{t('ownedTransfers.chooseHolding', 'Select holding')}</option>{index.holdings.filter((holding) => holding.group_id === movement.group_id).map((holding) => <option key={holding.id} value={holding.id}>{mask(holding.name)} · {holding.currency}</option>)}</NativeSelect>
    <OwnershipPicker index={index} movement={movement} assetId={value.asset_id} value={value.ownership_id} onChange={(ownership_id) => onChange({ ...value, ownership_id })} disabled={disabled} refresh={refresh} title={t('ownedTransfers.feeOwnership', 'Fee payer ownership')} />
    <LotPicker workspaceId={index.workspace_id} revision={index.revision} assetId={value.asset_id} legId={movement.leg_id} value={value.allocations} onChange={(allocations) => onChange({ ...value, allocations })} disabled={disabled} />
  </div>
}

function IncidentForm({ index, movement, disabled, refresh }: { index: TransferIndex; movement: RetainedMovement; disabled: boolean; refresh: () => Promise<unknown> }) {
  const { t } = useTranslation()
  const { mask, privacyMode } = usePrivacyMode()
  const existing = index.incidents.find((item) => item.leg_id === movement.leg_id)
  const [note, setNote] = useState(existing?.note ?? '')
  const [status, setStatus] = useState<IncidentCreate['source_status']>(existing?.source_status ?? 'user_reported')
  const [feeIds, setFeeIds] = useState<string[]>(existing?.related_fee_leg_ids ?? [])
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<unknown>(null)
  const alive = useRef(true)
  const lock = useRef(false)
  useEffect(() => { alive.current = true; return () => { alive.current = false } }, [])
  const save = async () => {
    if (disabled || lock.current || !note.trim()) return
    lock.current = true; setBusy(true); setError(null)
    try {
      const result = await ownedTransfers.annotate(index.workspace_id, { leg_id: movement.leg_id, allegation: 'reported_scam', source_status: status, note: note.trim(), evidence_observation_ids: [movement.observation_id], related_fee_leg_ids: feeIds }, existing?.id)
      if (!alive.current) return
      if (result.workspace_id !== index.workspace_id) throw new Error('Annotation destination changed')
      await refresh()
    } catch (failure) { if (alive.current) setError(failure) }
    finally { lock.current = false; if (alive.current) setBusy(false) }
  }
  return <details className="space-y-3 border-t border-border pt-3"><summary className="cursor-pointer py-2 font-medium">{t('ownedTransfers.incident', 'Reported scam annotation')}</summary>
    <p className="max-w-prose text-sm text-muted-foreground">{t('ownedTransfers.incidentHelp', 'Keep an allegation and its supporting source with this external movement. Tax treatment remains unresolved; an annotation does not apply a sale, deductible loss or internal transfer, and does not attribute pooled downstream funds.')}</p>
    {existing && <p role="status" className="break-words text-sm">{t('ownedTransfers.savedAnnotation', 'Saved allegation')}: {mask(existing.note)} · {t(`ownedTransfers.${existing.source_status}`, existing.source_status.replaceAll('_', ' '))} · {t('ownedTransfers.taxUnknown', 'Tax treatment unresolved')}</p>}
    {!disabled && <fieldset disabled={busy} className="space-y-3"><Label htmlFor="incident-status">{t('ownedTransfers.allegationStatus', 'Allegation source status')}</Label><NativeSelect className={selectClass} id="incident-status" value={status} onChange={(event) => setStatus(event.target.value as IncidentCreate['source_status'])}>{(['user_reported', 'documented', 'disputed'] as const).map((item) => <option key={item} value={item}>{t(`ownedTransfers.${item}`, item.replaceAll('_', ' '))}</option>)}</NativeSelect><Label htmlFor="incident-note">{t('ownedTransfers.allegationNote', 'Allegation and supporting source')}</Label><textarea id="incident-note" className="min-h-28 w-full rounded-md border border-input bg-card p-3 text-base sm:text-sm" value={note} maxLength={2000} style={privacyMode ? { WebkitTextSecurity: 'disc' } as React.CSSProperties : undefined} onChange={(event) => setNote(event.target.value)} />
      <fieldset className="space-y-2"><legend className="mb-2 text-sm font-medium">{t('ownedTransfers.relatedFees', 'Separately evidenced fees')}</legend>{index.movements.filter(isFee).map((fee) => <label className="flex min-h-11 items-start gap-3 break-all text-sm" key={fee.leg_id}><input className="mt-1" type="checkbox" checked={feeIds.includes(fee.leg_id)} onChange={(event) => setFeeIds(event.target.checked ? [...feeIds, fee.leg_id] : feeIds.filter((id) => id !== fee.leg_id))} /><span>{mask(fee.source_local_id ?? fee.leg_id)} · {mask(formatExactDecimal(fee.quantity ?? t('evidence.unknown', 'Unknown')))} · {mask(assetKey(fee))}</span></label>)}</fieldset>
      <Button disabled={!note.trim()} onClick={() => { void save() }}>{t('ownedTransfers.saveAnnotation', 'Save allegation')}</Button>
    </fieldset>}
    <ReviewError error={error} reviewIndex={index} />
  </details>
}

function MovementReview({ index, movement, disabled, refresh }: { index: TransferIndex; movement: RetainedMovement; disabled: boolean; refresh: () => Promise<unknown> }) {
  const { t } = useTranslation()
  const { mask } = usePrivacyMode()
  const [selectedMode, setMode] = useState<'transfer' | 'movement'>(isFee(movement) ? 'movement' : 'transfer')
  const mode = movement.application_status === 'applied' && !isFee(movement) ? 'transfer' : selectedMode
  const [counterpartId, setCounterpartId] = useState('')
  const [sourceId, setSourceId] = useState(movement.asset_id ?? '')
  const [destinationId, setDestinationId] = useState('')
  const [sourceOwnership, setSourceOwnership] = useState('')
  const [destinationOwnership, setDestinationOwnership] = useState('')
  const [allocations, setAllocations] = useState<LotSelection[]>([])
  const [fees, setFees] = useState<MovementSelection[]>([])
  const [reason, setReason] = useState('')
  const [ordering, setOrdering] = useState(false)
  const [reviewed, setReviewed] = useState(false)
  const [preview, setPreview] = useState<{ result: TransferPreview | MovementPreview; body: TransferRequest | MovementRequest; baseRevision: string } | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<unknown>(null)
  const [notice, setNotice] = useState('')
  const generation = useRef(0)
  const lock = useRef(false)
  useEffect(() => () => { generation.current += 1 }, [])
  const change = () => { generation.current += 1; setPreview(null); setReviewed(false); setError(null); setNotice(''); setBusy(false) }
  const counterpart = index.movements.find((item) => item.leg_id === counterpartId)
  const outgoing = movement.direction === 'out' ? movement : counterpart
  const incoming = movement.direction === 'in' ? movement : counterpart
  const sourceMovement = mode === 'movement' ? movement : outgoing
  const sourceHoldingId = mode === 'transfer' && movement.direction === 'in' ? destinationId : sourceId
  const targetHoldingId = movement.direction === 'in' ? sourceId : destinationId
  const sourceOwnershipId = mode === 'transfer' && movement.direction === 'in' ? destinationOwnership : sourceOwnership
  const targetOwnershipId = movement.direction === 'in' ? sourceOwnership : destinationOwnership
  const sourceAssertion = index.ownership.find((item) => item.id === sourceOwnershipId)
  const targetAssertion = index.ownership.find((item) => item.id === targetOwnershipId)
  const sameOwner = !!sourceAssertion && !!targetAssertion && sourceAssertion.beneficial_owner === targetAssertion.beneficial_owner
  const requiresLots = mode === 'transfer' || movement.direction === 'out'
  const validLots = (!requiresLots || allocations.length > 0) && allocations.every((item) => positiveDecimal(item.quantity))
  const validFees = fees.every((fee) => fee.asset_id && fee.ownership_id && fee.allocations.length > 0 && fee.allocations.every((item) => positiveDecimal(item.quantity)))
  const livePreview = preview?.baseRevision === index.revision ? preview : null
  const application = index.applications.find((item) => item.id === movement.application_id)
  const linkedTransfer = index.transfers.find((item) => item.request.out_leg_id === movement.leg_id || item.request.in_leg_id === movement.leg_id)
  const canReview = !linkedTransfer && (movement.application_status === 'unapplied' || (movement.application_status === 'applied' && !isFee(movement)))
  const ready = canReview && !!sourceHoldingId && !!sourceOwnershipId && !!reason.trim() && validLots && validFees && (mode === 'movement' || (!!outgoing && !!incoming && !!targetHoldingId && !!targetOwnershipId && sameOwner))
  const run = async (confirm = false) => {
    if (disabled || lock.current || !ready || (confirm && (!livePreview?.result.can_confirm || !reviewed))) return
    lock.current = true; const requestGeneration = ++generation.current; setBusy(true); setError(null); setNotice('')
    const body: TransferRequest | MovementRequest = mode === 'transfer' ? { out_leg_id: outgoing!.leg_id, in_leg_id: incoming!.leg_id, source_asset_id: sourceHoldingId, destination_asset_id: targetHoldingId, source_ownership_id: sourceOwnershipId, destination_ownership_id: targetOwnershipId, allocations, fees: fees.map((fee) => ({ ...fee, reason: reason.trim() })), reason: reason.trim(), ordering_reviewed: ordering } : { leg_id: movement.leg_id, asset_id: sourceHoldingId, ownership_id: sourceOwnershipId, allocations, reason: reason.trim(), ordering_reviewed: ordering }
    try {
      if (confirm && livePreview) {
        const result = 'out_leg_id' in livePreview.body ? await ownedTransfers.confirm(index.workspace_id, { ...livePreview.body, expected_revision: livePreview.result.revision }) : await ownedTransfers.applyMovement(index.workspace_id, { ...livePreview.body, expected_revision: livePreview.result.revision })
        if (requestGeneration !== generation.current) return
        if (result.workspace_id !== index.workspace_id) throw new Error('Review destination changed')
        setPreview(null); setReviewed(false)
        await refresh()
        if (requestGeneration === generation.current) setNotice(t('ownedTransfers.saved', 'Decision saved. Holdings and evidence have been refreshed; unknown treatment remains unknown.'))
      } else {
        const result = 'out_leg_id' in body ? await ownedTransfers.preview(index.workspace_id, body) : await ownedTransfers.previewMovement(index.workspace_id, body)
        if (requestGeneration !== generation.current) return
        if (result.workspace_id !== index.workspace_id) throw new Error('Review destination changed')
        setPreview({ result, body, baseRevision: index.revision }); setReviewed(false)
      }
    } catch (failure) {
      if (requestGeneration !== generation.current) return
      setError(failure); setPreview(null); setReviewed(false)
      if (confirm) await refresh()
    } finally { lock.current = false; if (requestGeneration === generation.current) setBusy(false) }
  }
  return <div className="min-w-0 space-y-6">
    <MovementFacts movement={movement} />
    {linkedTransfer && <Link className="inline-flex min-h-11 items-center underline underline-offset-4" to={`/assets?${new URLSearchParams({ tab: 'activity', activity: 'transfers', transfer: linkedTransfer.id })}`}>{t('ownedTransfers.openConfirmed', 'Open transfer decision')} · {t(`ownedTransfers.${linkedTransfer.status}`, linkedTransfer.status)}</Link>}
    {application && (!linkedTransfer || linkedTransfer.status === 'reversed') && <DecisionDetail key={`${application.id}:${application.revision}`} decision={application} index={index} disabled={disabled} refresh={refresh} />}
    {canReview && <fieldset disabled={disabled || busy} className="min-w-0 space-y-5 border-t border-border pt-5">
      <legend className="px-1 font-semibold">{t('ownedTransfers.review', 'Review movement')}</legend>
      <div className="space-y-2"><Label htmlFor="movement-action">{t('ownedTransfers.action', 'Reviewed action')}</Label><NativeSelect className={selectClass} id="movement-action" value={mode} onChange={(event) => { change(); setMode(event.target.value as 'transfer' | 'movement'); setAllocations([]); setFees([]) }}>{!isFee(movement) && <option value="transfer">{t('ownedTransfers.ownedTransfer', 'Transfer between my owned accounts')}</option>}{movement.application_status === 'unapplied' && <option value="movement">{t('ownedTransfers.quantityOnly', 'Apply this quantity only; treatment unresolved')}</option>}</NativeSelect></div>
      {movement.application_status === 'applied' && <p className="text-sm text-muted-foreground">{t('ownedTransfers.enrichApplied', 'This quantity is already applied. Confirming its owned transfer adds the reviewed source lineage without applying the quantity again.')}</p>}
      <p className="max-w-prose text-sm text-muted-foreground">{mode === 'transfer' ? t('ownedTransfers.transferHelp', 'Confirm two retained movement legs and their common ownership, then select the original acquisition lots. Amount or date similarity alone cannot confirm a transfer.') : t('ownedTransfers.movementHelp', 'This records only the supported incoming, outgoing or fee quantity. It does not create acquisition cost, income, sale gain or a tax classification.')}</p>
      <div className="space-y-3"><Label htmlFor="movement-holding">{t('ownedTransfers.movementHolding', 'Holding for this movement')}</Label><NativeSelect className={selectClass} id="movement-holding" value={sourceId} onChange={(event) => { change(); setSourceId(event.target.value); setSourceOwnership(''); setAllocations([]) }}><option value="">{t('ownedTransfers.chooseHolding', 'Select holding')}</option>{index.holdings.filter((holding) => holding.group_id === movement.group_id).map((holding) => <option key={holding.id} value={holding.id}>{mask(holding.name)} · {holding.currency}{holding.is_archived ? ` · ${t('ownedTransfers.archived', 'Archived')}` : ''}</option>)}</NativeSelect>
        <OwnershipPicker key={`${movement.leg_id}:${sourceId}`} index={index} movement={movement} assetId={sourceId} value={sourceOwnership} onChange={(id) => { change(); setSourceOwnership(id) }} disabled={disabled || busy} refresh={refresh} title={t('ownedTransfers.thisOwnership', 'Ownership of this endpoint')} />
      </div>
      {mode === 'transfer' && <div className="space-y-4 border-t border-border pt-4"><Label htmlFor="transfer-counterpart">{t('ownedTransfers.counterpart', 'Corresponding retained movement')}</Label><NativeSelect className={selectClass} id="transfer-counterpart" value={counterpartId} onChange={(event) => { change(); setCounterpartId(event.target.value); setDestinationId(''); setDestinationOwnership(''); setAllocations([]) }}><option value="">{t('ownedTransfers.chooseCounterpart', 'Select the other movement leg')}</option>{index.movements.filter((item) => item.direction !== movement.direction && item.direction !== 'unknown' && !isFee(item) && item.leg_id !== movement.leg_id).map((item) => <option key={item.leg_id} value={item.leg_id}>{mask(item.source_local_id ?? item.leg_id)} · {item.event_date ?? t('evidence.unknown', 'Unknown')} · {mask(formatExactDecimal(item.quantity ?? t('evidence.unknown', 'Unknown')))} · {mask(assetKey(item))}</option>)}</NativeSelect>
        {counterpart && <><details><summary className="cursor-pointer py-2 text-sm font-medium">{t('ownedTransfers.otherEvidence', 'Other endpoint evidence')}</summary><MovementFacts movement={counterpart} /></details><Label htmlFor="counterpart-holding">{t('ownedTransfers.otherHolding', 'Holding for the other endpoint')}</Label><NativeSelect className={selectClass} id="counterpart-holding" value={destinationId} onChange={(event) => { change(); setDestinationId(event.target.value); setDestinationOwnership(''); setAllocations([]) }}><option value="">{t('ownedTransfers.chooseHolding', 'Select holding')}</option>{index.holdings.filter((holding) => holding.group_id === counterpart.group_id).map((holding) => <option key={holding.id} value={holding.id}>{mask(holding.name)} · {holding.currency}{holding.is_archived ? ` · ${t('ownedTransfers.archived', 'Archived')}` : ''}</option>)}</NativeSelect><OwnershipPicker key={`${counterpart.leg_id}:${destinationId}`} index={index} movement={counterpart} assetId={destinationId} value={destinationOwnership} onChange={(id) => { change(); setDestinationOwnership(id) }} disabled={disabled || busy} refresh={refresh} title={t('ownedTransfers.otherOwnership', 'Ownership of the other endpoint')} />{sourceAssertion && targetAssertion && !sameOwner && <p role="alert" className="text-sm text-warning-foreground">{t('ownedTransfers.differentOwners', 'The selected assertions name different beneficial owners. This cannot be confirmed as an owned transfer.')}</p>}</>}
      </div>}
      {requiresLots && sourceMovement && <LotPicker workspaceId={index.workspace_id} revision={index.revision} assetId={sourceHoldingId} legId={sourceMovement.leg_id} value={allocations} onChange={(next) => { change(); setAllocations(next) }} disabled={disabled || busy} />}
      {mode === 'transfer' && <details className="space-y-3 border-t border-border pt-3"><summary className="cursor-pointer py-2 font-medium">{t('ownedTransfers.fees', 'Review separately evidenced fee units')}</summary><p className="text-sm text-muted-foreground">{t('ownedTransfers.feesHelp', 'Select a fee only when an owned holding paid it. A third-party fee does not debit your holdings. Fee units remain separate from principal and their tax treatment stays unresolved.')}</p>{index.movements.filter(isFee).map((fee) => {
        const selected = fees.find((item) => item.leg_id === fee.leg_id)
        return <div key={fee.leg_id}><label className="flex min-h-11 items-start gap-3 break-all text-sm"><input className="mt-1" type="checkbox" checked={!!selected} onChange={(event) => { change(); setFees(event.target.checked ? [...fees, { leg_id: fee.leg_id, asset_id: fee.asset_id ?? '', ownership_id: '', allocations: [], reason: '' }] : fees.filter((item) => item.leg_id !== fee.leg_id)) }} /><span>{mask(fee.source_local_id ?? fee.leg_id)} · {mask(formatExactDecimal(fee.quantity ?? t('evidence.unknown', 'Unknown')))} · {mask(assetKey(fee))} · {t(`ownedTransfers.${fee.application_status}`, fee.application_status)}</span></label>{selected && <FeeSelection index={index} movement={fee} value={selected} onChange={(next) => { change(); setFees(fees.map((item) => item.leg_id === fee.leg_id ? next : item)) }} disabled={disabled || busy} refresh={refresh} />}</div>
      })}</details>}
      <div className="space-y-2"><Label htmlFor="transfer-reason">{t('ownedTransfers.reviewReason', 'Evidence supporting this decision')}</Label><Input id="transfer-reason" maxLength={500} value={reason} onChange={(event) => { change(); setReason(event.target.value) }} /></div>
      <label className="flex min-h-11 items-start gap-3 text-sm"><input className="mt-1" type="checkbox" checked={ordering} onChange={(event) => { change(); setOrdering(event.target.checked) }} /><span>{t('ownedTransfers.ordering', 'I reviewed the actual ordering where the source reports only a date. This does not invent a timestamp.')}</span></label>
      <Button disabled={!ready || disabled || busy} onClick={() => { void run() }}>{busy ? t('ownedTransfers.working', 'Reviewing…') : t('ownedTransfers.preview', 'Preview exact effects')}</Button>
    </fieldset>}
    {disabled && <p className="text-sm text-muted-foreground">{t('ownedTransfers.viewer', 'Viewers can inspect saved evidence and decisions. An editor can confirm ownership or apply movements.')}</p>}
    {preview && !livePreview && <p role="status" className="text-sm text-warning-foreground">{t('ownedTransfers.changed', 'The evidence or holdings changed. Preview again before confirming.')}</p>}
    {livePreview && <section className="space-y-4 border-t border-border pt-5" aria-label={t('ownedTransfers.effects', 'Reviewed effects')}><h3 className="font-semibold">{t('ownedTransfers.effects', 'Reviewed effects')}</h3><Reasons reasons={livePreview.result.reason_codes} />{'principal_quantity' in livePreview.result && <dl className="grid gap-3 sm:grid-cols-2">{Object.entries({ principal_quantity: livePreview.result.principal_quantity, original_acquisition_cost: livePreview.result.acquisition_cost, known_acquisition_cost: livePreview.result.known_acquisition_cost, performance_basis: livePreview.result.performance_basis, unknown_basis_quantity: livePreview.result.unknown_basis_quantity }).map(([field, amount]) => <div key={field}><dt className="text-sm text-muted-foreground">{t(`ownedTransfers.fields.${field}`, field.replaceAll('_', ' '))}</dt><dd className="break-all text-sm tabular-nums">{amount == null ? t('evidence.unknown', 'Unknown') : mask(formatExactDecimal(amount))}</dd></div>)}</dl>}<p className="max-w-prose text-sm text-muted-foreground">{t('ownedTransfers.costDistinction', 'Original acquisition cost follows the selected lots. Performance basis follows the source weighted average; the two can differ. Neither certifies a filing-ready tax result. Provider quantity remains independent of incomplete replay.')}</p>{'selected_lots' in livePreview.result && <LotLineage lots={livePreview.result.selected_lots} index={index} title={t('ownedTransfers.consumedLineage', 'Selected / consumed source lineage')} />}<Effects effects={livePreview.result.effects} index={index} />{'fee_movements' in livePreview.result && livePreview.result.fee_movements.map((fee) => <details key={fee.leg_id}><summary className="cursor-pointer py-2 text-sm font-medium">{t('ownedTransfers.feeEvidence', 'Fee evidence')} · {mask(formatExactDecimal(fee.quantity ?? t('evidence.unknown', 'Unknown')))}</summary><MovementFacts movement={fee} /></details>)}<label className="flex min-h-11 items-start gap-3 text-sm"><input className="mt-1" type="checkbox" checked={reviewed} disabled={disabled || busy || !livePreview.result.can_confirm} onChange={(event) => setReviewed(event.target.checked)} /><span>{t('ownedTransfers.confirmReview', 'I reviewed both endpoint ownership mappings, selected quantities, fees and the effects above; unresolved facts remain unresolved.')}</span></label><Button disabled={disabled || busy || !reviewed || !livePreview.result.can_confirm} onClick={() => { void run(true) }}>{mode === 'transfer' ? t('ownedTransfers.confirm', 'Confirm owned transfer') : t('ownedTransfers.applyQuantity', 'Apply reviewed quantity')}</Button></section>}
    <ReviewError error={error} reviewIndex={index} retry={() => { change(); void refresh() }} />
    {notice && <p role="status" className="text-sm">{notice}</p>}
    {movement.direction === 'out' && !isFee(movement) && !linkedTransfer && <IncidentForm key={`${movement.leg_id}:${index.incidents.find((item) => item.leg_id === movement.leg_id)?.updated_at ?? ''}`} index={index} movement={movement} disabled={disabled || busy} refresh={refresh} />}
  </div>
}

function DecisionDetail({ decision, index, disabled, refresh }: { decision: TransferRead | MovementApplication; index: TransferIndex; disabled: boolean; refresh: () => Promise<unknown> }) {
  const { t } = useTranslation()
  const { mask } = usePrivacyMode()
  const [reviewed, setReviewed] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<unknown>(null)
  const alive = useRef(true)
  const lock = useRef(false)
  useEffect(() => { alive.current = true; return () => { alive.current = false } }, [])
  const transfer = 'out_leg_id' in decision.request
  const reverse = async () => {
    if (disabled || lock.current || !reviewed) return
    lock.current = true; setBusy(true); setError(null)
    try {
      const result = transfer ? await ownedTransfers.reverse(index.workspace_id, decision.id, decision.revision) : await ownedTransfers.reverseMovement(index.workspace_id, decision.id, decision.revision)
      if (!alive.current) return
      if (result.workspace_id !== index.workspace_id) throw new Error('Reversal destination changed')
      await refresh()
      if (alive.current) setReviewed(false)
    } catch (failure) { if (alive.current) { setError(failure); setReviewed(false); await refresh() } }
    finally { lock.current = false; if (alive.current) setBusy(false) }
  }
  const ids = 'out_leg_id' in decision.request ? [decision.request.out_leg_id, decision.request.in_leg_id] : [decision.request.leg_id]
  return <section className="min-w-0 space-y-4">
    <p className="text-sm font-medium">{t('ownedTransfers.decisionStatus', 'Decision status')}: {t(`ownedTransfers.${decision.status}`, decision.status)}</p>
    <Reasons reasons={decision.reason_codes} />
    <p className="break-words text-sm">{mask(decision.request.reason)}</p>
    {'principal_quantity' in decision && <p className="break-all text-sm tabular-nums">{t('ownedTransfers.principal', 'Principal')}: {mask(formatExactDecimal(decision.principal_quantity))} · {t('ownedTransfers.originalCost', 'Original acquisition cost')}: {decision.acquisition_cost == null ? t('evidence.unknown', 'Unknown') : mask(formatExactDecimal(decision.acquisition_cost))} · {t('ownedTransfers.performanceCost', 'Performance basis')}: {decision.performance_basis == null ? t('evidence.unknown', 'Unknown') : mask(formatExactDecimal(decision.performance_basis))}</p>}
    <div className="space-y-2">{ids.map((id, position) => <Link className="flex min-h-11 items-center break-all underline underline-offset-4" key={id} to={`/assets?${new URLSearchParams({ tab: 'activity', activity: 'transfers', movement: id })}`}>{position === 0 ? t('ownedTransfers.openSource', 'Open source movement') : t('ownedTransfers.openDestination', 'Open destination movement')} · {mask(index.movements.find((item) => item.leg_id === id)?.source_local_id ?? id)}</Link>)}</div>
    {'selected_lots' in decision && <LotLineage lots={decision.selected_lots} index={index} title={t('ownedTransfers.consumedLineage', 'Selected / consumed source lineage')} />}
    <Effects effects={decision.effects} index={index} />
    {decision.status !== 'reversed' && !disabled && <fieldset disabled={busy} className="space-y-3 border-t border-border pt-4"><legend className="px-1 font-medium">{transfer ? t('ownedTransfers.reverse', 'Reverse transfer') : t('ownedTransfers.reverseMovement', 'Reverse quantity application')}</legend><p className="max-w-prose text-sm text-muted-foreground">{t('ownedTransfers.reverseHelp', 'Reversal restores this decision’s applications together and keeps original evidence. Dependent transfers, sales or fee allocations can prevent reversal; resolve those records first. Independently supported quantities may remain.')}</p><label className="flex min-h-11 items-start gap-3 text-sm"><input className="mt-1" type="checkbox" checked={reviewed} onChange={(event) => setReviewed(event.target.checked)} /><span>{t('ownedTransfers.reverseAcknowledgement', 'I reviewed the affected holdings and understand that both sides must reverse together.')}</span></label><Button variant="outline" disabled={!reviewed} onClick={() => { void reverse() }}>{transfer ? t('ownedTransfers.confirmReverse', 'Confirm transfer reversal') : t('ownedTransfers.confirmReverseMovement', 'Confirm quantity reversal')}</Button></fieldset>}
    <ReviewError error={error} reviewIndex={index} retry={() => { void refresh() }} />
  </section>
}

export function OwnedTransfersPanel({ workspaceId, wallets, scopeWalletIds }: { workspaceId: string; wallets: AssetGroup[]; scopeWalletIds: string[] | null }) {
  const { t } = useTranslation()
  const { canWrite } = useWorkspace()
  const { mask } = usePrivacyMode()
  const [params, setParams] = useSearchParams()
  const client = useQueryClient()
  const focusTarget = useRef<HTMLElement | null>(null)
  const query = useQuery({ queryKey: ['owned-transfers', workspaceId], queryFn: ({ signal }) => ownedTransfers.index(workspaceId, signal), retry: false })
  const index = query.data?.workspace_id === workspaceId ? query.data : undefined
  const search = params.get('movement_search') ?? ''
  const status = params.get('movement_status') ?? ''
  const asset = params.get('movement_asset') ?? ''
  const direction = params.get('movement_direction') ?? ''
  const source = params.get('movement_source') ?? ''
  const since = params.get('movement_since') ?? ''
  const until = params.get('movement_until') ?? ''
  const view = params.get('transfer_view') === 'decisions' ? 'decisions' : 'movements'
  const page = Math.max(0, Number(params.get('movement_page')) || 0)
  // ponytail: paginate the existing workspace bootstrap locally until the API exposes cursors.
  const scoped = index?.movements.filter((movement) => scopeWalletIds === null || (movement.group_id !== null && scopeWalletIds.includes(movement.group_id))) ?? []
  const scopedLegIds = new Set(scoped.map((movement) => movement.leg_id))
  const filtered = scoped.filter((movement) => (!status || movement.application_status === status || movement.settlement_status === status || (status === 'unresolved' && movement.reason_codes.length > 0)) && (!asset || assetKey(movement) === asset) && (!direction || movement.direction === direction) && (!source || movement.source === source) && (!since || !movement.event_date || movement.event_date >= since) && (!until || !movement.event_date || movement.event_date <= until) && JSON.stringify(movement).toLowerCase().includes(search.trim().toLowerCase()))
  const decisions = index?.transfers.filter((decision) => (scopeWalletIds === null || [decision.request.out_leg_id, decision.request.in_leg_id].some((id) => scopedLegIds.has(id))) && (!status || decision.status === status) && (!asset || [decision.request.out_leg_id, decision.request.in_leg_id].some((id) => index.movements.some((movement) => movement.leg_id === id && assetKey(movement) === asset))) && (!search || JSON.stringify(decision).toLowerCase().includes(search.trim().toLowerCase()))) ?? []
  const count = view === 'decisions' ? decisions.length : filtered.length
  const pages = Math.max(1, Math.ceil(count / 25))
  const currentPage = Math.min(page, pages - 1)
  const selectedMovement = index?.movements.find((movement) => movement.leg_id === params.get('movement') || (movement.observation_ref === params.get('observation_ref') && movement.leg_key === params.get('leg_key')))
  const selectedDecision = index?.transfers.find((decision) => decision.id === params.get('transfer'))
  const hasSelection = params.has('movement') || params.has('transfer') || params.has('observation_ref')
  const update = (changes: Record<string, string>, replace = true) => setParams((previous) => { const next = new URLSearchParams(previous); next.delete('movement_page'); for (const [key, value] of Object.entries(changes)) { if (value) next.set(key, value); else next.delete(key) } return next }, { replace })
  const open = (kind: 'movement' | 'transfer', id: string) => { focusTarget.current = document.activeElement as HTMLElement; update({ movement: '', transfer: '', observation_ref: '', leg_key: '', [kind]: id }, false) }
  const close = () => update({ movement: '', transfer: '', observation_ref: '', leg_key: '' })
  const refresh = async () => {
    await Promise.all(['owned-transfer-lots', 'investment-evidence', 'asset-transactions', 'asset-tax-lots', 'assets', 'asset-groups', 'portfolio-trend', 'accounts', 'dashboard', 'import-logs', 'reports'].map((key) => client.invalidateQueries({ queryKey: [key] })))
    return query.refetch()
  }
  const invalidWindow = !!since && !!until && since > until
  return <section className="min-w-0 space-y-5" aria-label={t('ownedTransfers.title', 'Movements and owned transfers')}>
    <div className="space-y-2"><h2 className="text-lg font-semibold">{t('ownedTransfers.title', 'Movements and owned transfers')}</h2><p className="max-w-prose text-sm text-muted-foreground">{t('ownedTransfers.intro', 'Start with retained evidence, confirm ownership and review exact lot quantities. Unmatched movements, unknown acquisition cost and incomplete settlement remain visible.')}</p><Link className="inline-flex min-h-11 items-center text-sm underline underline-offset-4" to="/import?tab=investments">{t('ownedTransfers.importSources', 'Review or import investment sources')}</Link></div>
    <div className="grid min-w-0 gap-3 sm:grid-cols-2 lg:grid-cols-4">
      <div className="space-y-1"><Label htmlFor="transfer-view">{t('ownedTransfers.show', 'Show')}</Label><NativeSelect id="transfer-view" className={selectClass} value={view} onChange={(event) => update({ transfer_view: event.target.value, movement_status: '' })}><option value="movements">{t('ownedTransfers.retained', 'Retained movements')}</option><option value="decisions">{t('ownedTransfers.decisions', 'Transfer decisions')}</option></NativeSelect></div>
      <div className="space-y-1"><Label htmlFor="movement-asset">{t('ownedTransfers.asset', 'Network / asset identity')}</Label><NativeSelect id="movement-asset" className={selectClass} value={asset} onChange={(event) => update({ movement_asset: event.target.value })}><option value="">{t('common.all', 'All')}</option>{[...new Set(scoped.map(assetKey))].filter(Boolean).map((key) => <option key={key} value={key}>{mask(key)}</option>)}</NativeSelect></div>
      <div className="space-y-1"><Label htmlFor="movement-status">{t('ownedTransfers.status', 'Status')}</Label><NativeSelect id="movement-status" className={selectClass} value={status} onChange={(event) => update({ movement_status: event.target.value })}><option value="">{t('common.all', 'All')}</option>{(view === 'decisions' ? ['confirmed', 'unresolved', 'reversed'] : ['unapplied', 'applied', 'reversed', 'settled', 'pending', 'failed', 'unknown', 'unresolved']).map((item) => <option key={item} value={item}>{t(`ownedTransfers.${item}`, item.replaceAll('_', ' '))}</option>)}</NativeSelect></div>
      <div className="space-y-1"><Label htmlFor="movement-search">{t('ownedTransfers.search', 'Search source or reference')}</Label><Input id="movement-search" type="search" value={search} onChange={(event) => update({ movement_search: event.target.value })} /></div>
      {view === 'movements' && <><div className="space-y-1"><Label htmlFor="movement-direction">{t('ownedTransfers.direction', 'Direction')}</Label><NativeSelect id="movement-direction" className={selectClass} value={direction} onChange={(event) => update({ movement_direction: event.target.value })}><option value="">{t('common.all', 'All')}</option>{['in', 'out', 'unknown'].map((item) => <option key={item} value={item}>{t(`evidence.direction.${item}`, item)}</option>)}</NativeSelect></div><div className="space-y-1"><Label htmlFor="movement-source">{t('ownedTransfers.source', 'Source')}</Label><NativeSelect id="movement-source" className={selectClass} value={source} onChange={(event) => update({ movement_source: event.target.value })}><option value="">{t('common.all', 'All')}</option>{[...new Set(scoped.map((movement) => movement.source))].map((item) => <option key={item} value={item}>{item}</option>)}</NativeSelect></div><div className="space-y-1"><Label htmlFor="movement-since">{t('ownedTransfers.since', 'From date (inclusive)')}</Label><Input id="movement-since" type="date" value={since} onChange={(event) => update({ movement_since: event.target.value })} /></div><div className="space-y-1"><Label htmlFor="movement-until">{t('ownedTransfers.until', 'Through date (inclusive)')}</Label><Input id="movement-until" type="date" value={until} onChange={(event) => update({ movement_until: event.target.value })} /></div></>}
    </div>
    {(since || until) && <p className="text-xs text-muted-foreground">{t('ownedTransfers.dateScope', 'Dates use reported source precision. Undated records remain visible because their inclusion cannot be resolved.')}</p>}
    {invalidWindow && <p role="alert" className="text-sm text-warning-foreground">{t('ownedTransfers.invalidWindow', 'The end date must be on or after the start date.')}</p>}
    <ReviewError error={query.error} retry={() => { void query.refetch() }} />
    {query.isPending ? <p role="status">{t('ownedTransfers.loading', 'Loading retained movements…')}</p> : query.data && !index ? <p role="alert">{t('ownedTransfers.scopeChanged', 'The workspace changed. Refresh this review before continuing.')}</p> : index && !invalidWindow && <>
      <p className="text-sm text-muted-foreground">{t('ownedTransfers.coverage', 'Retained evidence is not complete history. Open a movement to inspect settlement, source gaps and exact original references.')}</p>
      {count === 0 ? <p role="status" className="border-y border-border py-8 text-sm text-muted-foreground">{scoped.length === 0 ? t('ownedTransfers.empty', 'No retained movements in this wallet scope. Import source evidence or explicitly collect owned wallet history first.') : t('ownedTransfers.noMatches', 'No records match these filters. Clear a filter to inspect other retained evidence.')}</p> : <div className="divide-y divide-border border-y border-border">{view === 'movements' ? filtered.slice(currentPage * 25, (currentPage + 1) * 25).map((movement) => <Button variant="ghost" className="h-auto min-h-16 w-full justify-start whitespace-normal rounded-none px-2 py-4 text-left" key={movement.leg_id} aria-label={`${mask(movement.source_local_id ?? movement.transaction_ref ?? movement.leg_id)} · ${mask(formatExactDecimal(movement.quantity ?? t('evidence.unknown', 'Unknown')))} · ${t(`evidence.direction.${movement.direction}`, movement.direction)}`} onClick={() => open('movement', movement.leg_id)}><span className="grid min-w-0 flex-1 gap-2 sm:grid-cols-[minmax(0,1fr)_auto]"><span className="min-w-0"><strong className="block break-all">{mask(movement.source_local_id ?? movement.transaction_ref ?? movement.leg_id)}</strong><span className="mt-1 block break-words text-xs font-normal text-muted-foreground">{mask(wallets.find((wallet) => wallet.id === movement.group_id)?.name ?? t('ownedTransfers.unmapped', 'Unmapped wallet'))} · {movement.event_date ?? t('ownedTransfers.unknownDate', 'Acquisition date unknown')} · {mask(assetKey(movement))}</span></span><span className="space-y-1 sm:text-right"><span className="block break-all tabular-nums">{mask(formatExactDecimal(movement.quantity ?? t('evidence.unknown', 'Unknown')))} · {t(`evidence.direction.${movement.direction}`, movement.direction)}</span><span className="block text-xs font-normal text-muted-foreground">{t(`ownedTransfers.${movement.settlement_status}`, movement.settlement_status)} · {t(`ownedTransfers.${movement.application_status}`, movement.application_status)}{movement.reason_codes.length > 0 ? ` · ${t('ownedTransfers.gaps', 'Evidence gaps')}` : ''}</span></span></span></Button>) : decisions.slice(currentPage * 25, (currentPage + 1) * 25).map((decision) => <Button variant="ghost" className="h-auto min-h-16 w-full justify-start whitespace-normal rounded-none px-2 py-4 text-left" key={decision.id} aria-label={`${t('ownedTransfers.decision', 'Owned transfer decision')} · ${mask(formatExactDecimal(decision.principal_quantity))} · ${t(`ownedTransfers.${decision.status}`, decision.status)}`} onClick={() => open('transfer', decision.id)}><span className="min-w-0 flex-1 space-y-1"><strong className="block break-all">{mask(index.holdings.find((holding) => holding.id === decision.request.source_asset_id)?.name ?? decision.request.source_asset_id)} → {mask(index.holdings.find((holding) => holding.id === decision.request.destination_asset_id)?.name ?? decision.request.destination_asset_id)}</strong><span className="block text-sm font-normal">{mask(formatExactDecimal(decision.principal_quantity))} · {t(`ownedTransfers.${decision.status}`, decision.status)} · {formatExactDecimal(decision.unknown_basis_quantity) !== '0' ? t('ownedTransfers.basisUnknown', 'Acquisition basis incomplete / unknown') : t('ownedTransfers.sourceCost', 'Original lot cost retained')}</span></span></Button>)}</div>}
      {pages > 1 && <div className="flex items-center justify-between gap-3"><Button variant="outline" disabled={currentPage === 0} onClick={() => update({ movement_page: String(currentPage - 1) })}>{t('common.previous', 'Previous')}</Button><span className="text-sm tabular-nums">{currentPage + 1} / {pages}</span><Button variant="outline" disabled={currentPage + 1 >= pages} onClick={() => update({ movement_page: String(currentPage + 1) })}>{t('common.next', 'Next')}</Button></div>}
    </>}
    <Dialog open={hasSelection} onOpenChange={(open) => { if (!open) close() }}><DialogContent className="min-w-0 sm:max-w-3xl" onCloseAutoFocus={(event) => { if (focusTarget.current?.isConnected) { event.preventDefault(); focusTarget.current.focus() } }}><DialogHeader><DialogTitle>{selectedDecision ? t('ownedTransfers.decision', 'Owned transfer decision') : t('ownedTransfers.review', 'Review movement')}</DialogTitle><DialogDescription>{t('ownedTransfers.detailHelp', 'Review retained source evidence, original lineage and exact effects in this workspace.')}</DialogDescription></DialogHeader>{index ? selectedDecision ? <DecisionDetail key={`${workspaceId}:${selectedDecision.id}:${selectedDecision.revision}:${params.toString()}`} decision={selectedDecision} index={index} disabled={!canWrite || query.isError} refresh={refresh} /> : selectedMovement ? <MovementReview key={`${workspaceId}:${selectedMovement.leg_id}:${JSON.stringify(scopeWalletIds)}:${params.toString()}`} index={index} movement={selectedMovement} disabled={!canWrite || query.isError} refresh={refresh} /> : <p role="alert">{t('ownedTransfers.unavailable', 'This retained record is unavailable in the selected workspace. Refresh the review or return to the list.')}</p> : <p role="status">{t('common.loading', 'Loading…')}</p>}<ReviewError error={query.error} retry={() => { void query.refetch() }} /></DialogContent></Dialog>
  </section>
}
