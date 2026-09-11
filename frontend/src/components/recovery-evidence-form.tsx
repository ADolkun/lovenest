import { useState, type FormEvent } from 'react'
import { useTranslation } from 'react-i18next'
import { usePrivacyMode } from '@/hooks/use-privacy-mode'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { NativeSelect } from '@/components/ui/native-select'
import type { EvidenceObservation } from '@/types/investment-evidence'
import type { Asset } from '@/types'
import { RECOVERY_ROLES, type RecoveryRole } from '@/types/recovery-evidence'

export type RecoveryDraft = { role: RecoveryRole; source: Record<string, string>; legs: Record<string, string>[] }
const selectClass = 'min-h-11 rounded-md border border-input bg-card px-3 text-base sm:text-sm'
const roleFields: Record<RecoveryRole, [string, string][]> = {
  allowed_claim: [['claim_amount', 'Claim face amount'], ['claim_currency', 'Claim currency'], ['boundary_kind', 'Claim boundary / basis of statement']],
  platform_ledger: [['boundary_kind', 'Ledger boundary']],
  recovery_notice: [],
  receiving_receipt: [['cash_credited', 'Cash credited'], ['cash_currency', 'Cash credited currency']],
  disposition: [['acquisition_date', 'Reported acquisition date'], ['acquisition_basis', 'Reported acquisition basis'], ['proceeds', 'Reported sale proceeds'], ['proceeds_currency', 'Proceeds currency']],
  cash_proceeds: [['cash_credited', 'Cash credited'], ['cash_currency', 'Cash credited currency']],
  equity_statement: [['reported_cost', 'Displayed stock cost'], ['reported_cost_currency', 'Displayed cost currency'], ['statement_date', 'Statement date as reported']],
  tax_workpaper: [['provisional_allocation', 'Modeled allocation'], ['allocation_currency', 'Allocation currency']],
}

/** A source is entered once; several asset rows may describe one distribution round. */
export function RecoveryEvidenceForm({ busy, observations, holdings, onPreview, onChange }: { busy: boolean; observations: EvidenceObservation[]; holdings: Asset[]; onPreview: (draft: RecoveryDraft) => void; onChange: () => void }) {
  const { t } = useTranslation()
  const { mask } = usePrivacyMode()
  const [role, setRole] = useState<RecoveryRole>('recovery_notice')
  const [rows, setRows] = useState([0])
  const [nextRow, setNextRow] = useState(1)
  const [precision, setPrecision] = useState('unknown')
  const [existing, setExisting] = useState('')
  const [associations, setAssociations] = useState<Record<number, string>>({})
  const selected = observations.find((item) => item.reference === existing)
  const field = (name: string, title: string, options: { required?: boolean; decimal?: boolean; type?: string } = {}) => <div key={name} className="min-w-0 space-y-1.5">
    <Label htmlFor={`recovery-${name}`}>{t(`recovery.field.${name.replace(/^leg-\d+-/, '')}`, title)}</Label>
    <Input id={`recovery-${name}`} name={name} className="min-h-11" maxLength={128} required={options.required} type={options.type ?? 'text'} inputMode={options.decimal ? 'decimal' : undefined} autoComplete="off" />
  </div>
  const submit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    if (busy) return
    const data = new FormData(event.currentTarget)
    const source = Object.fromEntries([...data.entries()].filter(([key]) => !key.startsWith('leg-')).map(([key, value]) => [key, String(value).trim()]))
    const legs = rows.map((row) => Object.fromEntries([...data.entries()].filter(([key]) => key.startsWith(`leg-${row}-`)).map(([key, value]) => [key.replace(`leg-${row}-`, ''), String(value).trim()])))
    onPreview({ role, source, legs })
  }
  return <form onSubmit={submit} onChange={onChange} className="space-y-5">
    <fieldset disabled={busy} className="space-y-5">
      <legend className="mb-3 font-semibold">{t('recovery.add', 'Add recovery source evidence')}</legend>
      <p className="max-w-prose text-sm text-muted-foreground">{t('recovery.formHelp', 'Enter only what the source reports. Leave unavailable values blank; zero is a reported value. Saving evidence changes no holdings, basis or tax treatment.')}</p>
      <div className="grid gap-4 sm:grid-cols-2">
        <div className="space-y-1.5"><Label htmlFor="recovery-existing">{t('recovery.existing', 'Source to use')}</Label><NativeSelect id="recovery-existing" name="existing_observation_id" className={selectClass} value={existing} onChange={(event) => setExisting(event.target.value)}><option value="">{t('recovery.manual', 'Enter source facts manually')}</option>{observations.map((item) => <option key={item.reference} value={item.reference}>{mask(item.provider)} · {mask(item.source_local_id ?? item.source_locator)} · {item.event_time_raw ?? item.event_date ?? t('recovery.unknown', 'Unknown')}</option>)}</NativeSelect></div>
        {selected && <div className="space-y-1.5"><Label htmlFor="recovery-existing-leg">{t('recovery.existingLeg', 'Retained source asset leg')}</Label><NativeSelect key={selected.reference} id="recovery-existing-leg" name="existing_leg_key" className={selectClass}>{selected.legs.map((leg) => <option key={leg.key} value={leg.key}>{mask(leg.asset_symbol ?? t('recovery.unknown', 'Unknown'))} · {mask(leg.quantity ?? t('recovery.unknown', 'Unknown'))} · {mask(leg.key)}</option>)}</NativeSelect></div>}
      </div>
      {selected && <p className="text-sm text-muted-foreground">{t('recovery.attachHelp', 'Original source facts remain unchanged. Add only recovery context and separate reported details below. Record acquisition-basis corrections as a separate review assertion.')}</p>}
      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
        <div className="space-y-1.5"><Label htmlFor="recovery-role">{t('recovery.role', 'Evidence role')}</Label><NativeSelect id="recovery-role" value={role} className={selectClass} onChange={(event) => setRole(event.target.value as RecoveryRole)}>{RECOVERY_ROLES.map((value) => <option key={value} value={value}>{t(`recovery.role.${value}`, value.replaceAll('_', ' '))}</option>)}</NativeSelect></div>
        {!selected && <>{field('provider', 'Source provider', { required: true })}
        {field('source_reference', 'Source reference', { required: true })}
        {field('source_locator', 'Source document / row locator', { required: true })}
        {field('source_account_id', 'Historical source account')}
        {field('account_bucket', 'Source account segment (Earn, Custody, BIA or wallet)')}
        {field('historical_workspace_label', 'Historical workspace label (context only)')}</>}
        {field('case_key', 'Recovery case', { required: true })}
        {field('round_key', 'Distribution round reference')}
        {!selected && <><div className="space-y-1.5"><Label htmlFor="recovery-time_precision">{t('recovery.precision', 'Source date precision')}</Label><NativeSelect id="recovery-time_precision" name="time_precision" value={precision} className={selectClass} onChange={(event) => setPrecision(event.target.value)}>{['unknown', 'date', 'minute', 'second', 'fractional'].map((value) => <option key={value} value={value}>{t(`recovery.precision.${value}`, value)}</option>)}</NativeSelect></div>
        {field('event_time_raw', 'Source date / time as written')}
        {precision === 'date' && field('event_date', 'Reported date', { type: 'date' })}
        {!['unknown', 'date'].includes(precision) && field('event_at', 'Reported instant with timezone (ISO 8601)')}
        {field('timezone', 'Source timezone')}
        {field('provider_status', 'Source execution status')}
        <div className="space-y-1.5"><Label htmlFor="recovery-settlement_status">{t('recovery.settlement', 'Receipt / activity settlement')}</Label><NativeSelect id="recovery-settlement_status" name="settlement_status" className={selectClass}>{['unknown', 'settled', 'pending', 'failed'].map((value) => <option key={value} value={value}>{t(`recovery.settlement.${value}`, value)}</option>)}</NativeSelect></div></>}
        <div className="space-y-1.5"><Label htmlFor="recovery-reported_state">{t('recovery.reportedState', 'Source fact state')}</Label><NativeSelect id="recovery-reported_state" name="reported_state" className={selectClass}>{['candidate', 'confirmed', 'conflict', 'missing'].map((value) => <option key={value} value={value}>{t(`recovery.state.${value}`, value)}</option>)}</NativeSelect></div>
        {field('missing_evidence', 'Missing evidence (one item per semicolon)')}
      </div>
      <p className="text-xs text-muted-foreground">{t('recovery.dateHelp', 'An absent source or statement date stays unknown. Retrieval and import dates do not replace it. Historical labels do not change the destination above.')}</p>
      {(selected ? rows.slice(0, 1) : rows).map((row, index) => <fieldset key={row} className="space-y-3 border-t border-border pt-4">
        <legend className="px-1 text-sm font-medium">{t('recovery.assetRow', 'Asset record')} {index + 1}</legend>
        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
          {!selected && <>{field(`leg-${row}-asset_symbol`, 'Reported asset / currency')}
          {field(`leg-${row}-quantity`, 'Reported quantity', { decimal: true })}
          <div className="min-w-0 space-y-1.5"><Label htmlFor={`recovery-leg-${row}-asset_id`}>{t('recovery.associateHolding', 'Associate existing destination holding (optional)')}</Label><NativeSelect id={`recovery-leg-${row}-asset_id`} name={`leg-${row}-asset_id`} className={selectClass} value={associations[row] ?? ''} onChange={(event) => setAssociations({ ...associations, [row]: event.target.value })}><option value="">{t('recovery.noAssociation', 'No holding association')}</option>{associations[row] && !holdings.some((holding) => holding.id === associations[row]) && <option value={associations[row]}>{t('recovery.selectedHoldingUnavailable', 'Selected holding unavailable — choose again')}</option>}{holdings.map((holding) => <option key={holding.id} value={holding.id}>{mask(`${holding.name}${holding.ticker ? ` · ${holding.ticker}` : ''}`)}</option>)}</NativeSelect><p className="text-xs text-muted-foreground">{t('recovery.associationHelp', 'This records an association only. Enter the source asset identity below; selecting a holding does not verify it or add quantity.')}</p></div></>}
          {field(`leg-${row}-round_asset_key`, 'Asset reference within the round')}
          {!selected && <>{field(`leg-${row}-valuation_amount`, 'Reported valuation', { decimal: true })}
          {field(`leg-${row}-valuation_currency`, 'Valuation currency')}</>}
          {roleFields[role].filter(([name]) => !selected || name !== 'acquisition_basis').map(([name, title]) => field(`leg-${row}-${name}`, title, { type: name.endsWith('_date') ? 'date' : 'text' }))}
        </div>
        {!selected && <details><summary className="cursor-pointer py-3 text-sm font-medium focus-visible:outline-2 focus-visible:outline-ring">{t('recovery.identity', 'Asset identity and source fees')}</summary><div className="grid gap-4 pt-2 sm:grid-cols-2 lg:grid-cols-3">{[['chain', 'Chain'], ['token_address', 'Token identity'], ['isin', 'Security ISIN'], ['provider_asset_id', 'Provider asset ID'], ['fee', 'Reported fee'], ['fee_currency', 'Fee currency']].map(([name, title]) => field(`leg-${row}-${name}`, title, { type: name.endsWith('_date') ? 'date' : 'text' }))}</div></details>}
        {!selected && rows.length > 1 && <Button type="button" variant="ghost" onClick={() => { onChange(); setRows(rows.filter((value) => value !== row)) }}>{t('recovery.removeAsset', 'Remove asset row')} {index + 1}</Button>}
      </fieldset>)}
      <div className="flex flex-wrap gap-3">{!selected && <Button type="button" variant="outline" disabled={rows.length >= 100} onClick={() => { onChange(); setRows([...rows, nextRow]); setNextRow(nextRow + 1) }}>{t('recovery.addAsset', 'Add asset row')}</Button>}<Button type="submit">{busy ? t('recovery.reading', 'Preparing review…') : t('recovery.preview', 'Preview evidence')}</Button></div>
    </fieldset>
  </form>
}
