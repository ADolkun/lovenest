import { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Link, useSearchParams } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import { usePrivacyMode } from '@/hooks/use-privacy-mode'
import { timeline, type TimelineFilters } from '@/lib/timeline-api'
import { formatExactDecimal } from '@/lib/format'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { NativeSelect } from '@/components/ui/native-select'
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { BalanceDetails } from '@/components/balance-details'
import { EvidenceInvestigation } from '@/components/evidence-investigation'
import type { AssetGroup } from '@/types'
import type { TimelineAsset, TimelineCoverage, TimelineEvent, TimelineSource, TimelineTime } from '@/types/timeline'

const selectClass = 'min-h-11 w-full min-w-0 rounded-md border border-input bg-card px-3 py-2 text-base sm:text-sm'
const linkClass = 'inline-flex min-h-11 items-center rounded-sm text-sm font-medium underline underline-offset-4 focus-visible:outline-2 focus-visible:outline-ring'
const filterKeys = ['source', 'status', 'kind', 'direction', 'since', 'until'] as const

function assetLabel(asset: Pick<TimelineAsset, 'asset_symbol' | 'chain' | 'token_address'>, unknown: string): string {
  if (asset.asset_symbol) return asset.asset_symbol
  const token = asset.token_address
  return [asset.chain, token && token.length > 20 ? `${token.slice(0, 8)}…${token.slice(-6)}` : token].filter(Boolean).join(' · ') || unknown
}

function EventTime({ time }: { time: TimelineTime }) {
  const { t } = useTranslation()
  const { mask } = usePrivacyMode()
  // Display supplied precision: never manufacture midnight or discard fractions.
  const supplied = time.time_precision === 'date' ? time.event_date ?? time.event_time_raw : time.event_at ?? time.event_time_raw ?? time.event_date
  const value = time.time_precision === 'minute' ? supplied?.replace(/(T\d{2}:\d{2}):\d{2}(?:\.\d+)?/, '$1') : supplied
  return <span className="break-words">
    {value == null ? t('timeline.unknownTime', 'Time unknown') : mask(value)}
    <span className="block text-xs font-normal text-muted-foreground">
      {t(`timeline.precision.${time.time_precision}`, time.time_precision === 'date' ? 'Date only' : `${time.time_precision} precision`)}
      {time.timezone && <> · {mask(time.timezone)}</>}
      {time.ordering !== 'exact' && <> · {t(`timeline.ordering.${time.ordering}`, time.ordering === 'within_day_unknown' ? 'Order within day unknown' : time.ordering === 'conflicting' ? 'Source times conflict' : 'Order unknown')}</>}
    </span>
  </span>
}

function Facts({ values }: { values: Record<string, unknown> }) {
  const { t } = useTranslation()
  const { mask } = usePrivacyMode()
  return <dl className="grid min-w-0 gap-x-6 gap-y-3 text-sm sm:grid-cols-2">
    {Object.entries(values).map(([name, value]) => <div className="min-w-0" key={name}>
      <dt className="text-muted-foreground">{t(`timeline.fields.${name}`, name.replaceAll('_', ' '))}</dt>
      <dd className="break-all tabular-nums">{value == null ? t('timeline.unknown', 'Unknown') : mask(typeof value === 'object' ? JSON.stringify(value) : String(value))}</dd>
    </div>)}
  </dl>
}

function Reasons({ reasons }: { reasons: string[] }) {
  const { t } = useTranslation()
  return reasons.length > 0 && <ul className="list-inside list-disc space-y-1 text-sm text-warning-foreground">
    {[...new Set(reasons)].map((reason) => <li className="break-words" key={reason}>{t(`timeline.reasons.${reason}`, reason.replaceAll('_', ' '))}</li>)}
  </ul>
}

function Coverage({ entries }: { entries: TimelineCoverage[] }) {
  const { t } = useTranslation()
  const { mask } = usePrivacyMode()
  return <section className="min-w-0 space-y-3" aria-label={t('timeline.coverage', 'Evidence coverage')}>
    <h3 className="text-base font-semibold">{t('timeline.coverage', 'Evidence coverage')}</h3>
    <p className="max-w-prose text-sm text-muted-foreground">{t('timeline.coverageHelp', 'Coverage describes retained sources and their declared intervals. Loading every available record does not establish complete history or a known opening balance.')}</p>
    {!entries.length && <p className="text-sm text-muted-foreground">{t('timeline.noCoverage', 'No source coverage is available for this scope. Earlier history and opening quantity remain unknown.')}</p>}
    <div className="divide-y divide-border border-y border-border">{entries.map((entry) => <details key={entry.coverage_id} className="py-3">
      <summary className="min-h-11 cursor-pointer break-words text-sm focus-visible:outline-2 focus-visible:outline-ring">
        <strong>{mask(entry.source)}</strong>{entry.chain && <> · {mask(entry.chain)}</>}
        <span className="mt-1 block text-muted-foreground">{t('timeline.retrieval', 'Retrieval')}: {t(`timeline.state.${entry.retrieval}`, entry.retrieval.replaceAll('_', ' '))} · {entry.gaps.length ? t('timeline.gapsRemain', 'Gaps remain') : t('timeline.boundedScope', 'Declared scope only')}</span>
      </summary>
      <div className="space-y-4 pb-2 pt-3">
        <Facts values={{ inventory: entry.inventory, retrieval: entry.retrieval, interpretation: entry.interpretation, settlement: entry.settlement, last_successful_collection: entry.last_successful_collection, requested_interval: Object.keys(entry.requested).length ? entry.requested : null, observed_interval: Object.keys(entry.observed).length ? entry.observed : null }} />
        <Reasons reasons={entry.gaps} />
        {Object.keys(entry.streams).length > 0 && <details><summary className="min-h-11 cursor-pointer text-sm">{t('timeline.streams', 'Source streams and remaining work')}</summary><pre className="max-w-full whitespace-pre-wrap break-all text-xs">{mask(JSON.stringify(entry.streams, null, 2))}</pre></details>}
        {entry.connection_id && <Link className={linkClass} to={`/accounts#connection-${encodeURIComponent(entry.connection_id)}`}>{t('timeline.openConnection', 'Open connection details')}</Link>}
      </div>
    </details>)}</div>
  </section>
}

function RequestError({ retry, restart = false, source = false }: { retry: () => void; restart?: boolean; source?: boolean }) {
  const { t } = useTranslation()
  return <div role="alert" className="space-y-3 rounded-lg border border-warning/40 bg-warning/10 p-4 text-sm">
    <p>{restart ? t('timeline.changed', 'The available evidence changed. Restart the timeline to avoid skipping or repeating events.') : source ? t('timeline.sourceFailed', 'This source could not be loaded. The event remains available; retry the supporting evidence.') : t('timeline.failed', 'Activity could not be loaded in this scope. This does not mean the account has no history.')}</p>
    <Button variant="outline" className="min-h-11" onClick={retry}>{restart ? t('timeline.restart', 'Restart timeline') : t('common.retry', 'Retry')}</Button>
  </div>
}

function SourceDetails({ workspaceId, source, scope }: { workspaceId: string; source: TimelineSource; scope: TimelineFilters }) {
  const { t } = useTranslation()
  const { mask } = usePrivacyMode()
  const query = useQuery({
    queryKey: ['investment-timeline-source', workspaceId, source.source_id, scope],
    queryFn: ({ signal }) => timeline.source(workspaceId, source.source_id, scope, signal),
    retry: false,
  })
  const data = query.data?.workspace_id === workspaceId && query.data.source.source_id === source.source_id ? query.data : undefined
  return <section className="space-y-4 border-t border-border pt-5" aria-label={t('timeline.sourceDetail', 'Original source record')}>
    <h4 className="font-semibold">{t('timeline.sourceDetail', 'Original source record')}</h4>
    {query.isPending && <p role="status" className="text-sm">{t('timeline.loadingSource', 'Loading source evidence…')}</p>}
    {(query.isError || (query.isSuccess && !data)) && <RequestError source retry={() => { void query.refetch() }} />}
    {data && <>
      <Facts values={{ source: data.source.source, source_kind: data.source.source_kind, original_type: data.source.original_type, original_event_time: data.source.time.event_time_raw, provider_status: data.source.provider_status, network_status: data.source.network_status, settlement_status: data.source.settlement_status, source_reference: data.source.source_reference, source_local_id: data.source.source_local_id, source_locator: data.source.source_locator, observed_at: data.source.observed_at, payload_digest: data.source.payload_digest, decoder_version: data.source.decoder_version }} />
      <EventTime time={data.source.time} />
      {data.source.availability === 'unavailable' && <><Reasons reasons={[data.source.unavailable_reason ?? 'source_unavailable']} /><Button variant="outline" onClick={() => { void query.refetch() }}>{t('timeline.retrySource', 'Retry source evidence')}</Button></>}
      {data.observation && <details><summary className="min-h-11 cursor-pointer text-sm">{t('timeline.sourceFields', 'Retained observation fields')}</summary><pre className="max-w-full whitespace-pre-wrap break-all text-xs">{mask(JSON.stringify(data.observation, null, 2))}</pre></details>}
      {data.applied_entry && <section className="space-y-2"><h4 className="font-medium">{t('sourceReview.currentEntry', 'Current applied acquisition')}</h4><Facts values={data.applied_entry} /></section>}
      {!!data.source_reviews?.length && <details><summary className="min-h-11 cursor-pointer text-sm">{t('sourceReview.history', 'Source review audit')}</summary><pre className="max-w-full whitespace-pre-wrap break-all text-xs">{mask(JSON.stringify(data.source_reviews, null, 2))}</pre></details>}
      {data.raw_payload?.encoding === 'json' && <details><summary className="min-h-11 cursor-pointer text-sm">{t('timeline.rawPayload', 'Retained original payload')}</summary><pre className="max-w-full whitespace-pre-wrap break-all text-xs">{mask(data.raw_payload.json)}</pre></details>}
      {data.transaction?.encoding === 'json' && <details><summary className="min-h-11 cursor-pointer text-sm">{t('timeline.transactionPayload', 'Retained transaction interpretation')}</summary><pre className="max-w-full whitespace-pre-wrap break-all text-xs">{mask(data.transaction.json)}</pre></details>}
      <Coverage entries={data.coverage} />
    </>}
  </section>
}

function InvestigationEventDetails({ event, workspaceId, wallets }: {
  event: TimelineEvent; workspaceId: string; wallets: AssetGroup[]
}) {
  const [sourceId, setSourceId] = useState<string | null>(null)
  // Reached events can cross the starting wallet/collection; source reads remain workspace-authorized.
  return <EventDetails event={event} workspaceId={workspaceId} scope={{}} sourceId={sourceId} selectSource={setSourceId} nativeTraceEnabled={false} wallets={wallets} />
}

function EventDetails({ event, workspaceId, scope, sourceId, selectSource, nativeTraceEnabled, wallets }: {
  event: TimelineEvent; workspaceId: string; scope: TimelineFilters; sourceId: string | null; selectSource: (id: string) => void; nativeTraceEnabled: boolean; wallets: AssetGroup[]
}) {
  const { t } = useTranslation()
  const { mask } = usePrivacyMode()
  const label = (value: string) => t(`timeline.values.${value}`, value.replaceAll('_', ' '))
  const selectedSource = event.sources.find((source) => source.source_id === sourceId)
  return <div className="min-w-0 space-y-7">
    <section className="space-y-3">
      <div className="flex flex-wrap gap-x-4 gap-y-2 text-sm"><strong>{label(event.kind)}</strong><span>{label(event.status)}</span><span>{t('timeline.linkage', 'Linkage')}: {label(event.linkage)}</span></div>
      <EventTime time={event.time} />
      <p className="break-words text-sm">{event.accounts.map((account) => mask(account.group_name ?? t('timeline.unmapped', 'Unmapped account'))).join(' · ')}</p>
      <Reasons reasons={[...event.reason_codes, ...event.conflicting_fields.map((field) => `conflicting_${field}`)]} />
    </section>
    <Coverage entries={event.coverage} />

    <section className="space-y-3" aria-label={t('timeline.legs', 'Movement and fee legs')}>
      <h3 className="font-semibold">{t('timeline.legs', 'Movement and fee legs')}</h3>
      <p className="text-sm text-muted-foreground">{t('timeline.allLegs', 'All supplied event legs are shown, including other assets and fees. Values describe each source; they are not added into a new balance.')}</p>
      {!event.legs.length && <p className="text-sm">{t('timeline.noLegs', 'No interpreted movement legs are available. Inspect the retained source and interpretation gaps.')}</p>}
      <div className="divide-y divide-border">{event.legs.map((leg) => <article key={leg.leg_id} className="space-y-3 py-4">
        <h4 className="break-words text-sm font-medium">{label(leg.classification)} · {label(leg.direction)} · {leg.quantity == null ? t('timeline.unknown', 'Unknown') : mask(formatExactDecimal(leg.quantity))} {mask(assetLabel(leg, t('timeline.unknownAsset', 'Asset identity unresolved')))}</h4>
        <p className="text-sm text-muted-foreground">{label(leg.settlement_status)}{leg.non_additive && <> · {t('timeline.nonAdditive', 'Supporting measure; not an additional movement')}</>}{!leg.is_current && <> · {t('timeline.superseded', 'Superseded source interpretation')}</>}</p>
        {leg.applied_entry && <section className="space-y-2"><h4 className="font-medium">{t('sourceReview.currentEntry', 'Current applied acquisition')}</h4><Facts values={{ applied_quantity: leg.applied_entry.quantity, applied_unit_price: leg.applied_entry.price, additional_ledger_fee: leg.applied_entry.fee, date: leg.applied_entry.date, tax_basis_complete: false }} /><p className="text-muted-foreground">{t('sourceReview.originalRetained', 'Reported amounts below remain the original source facts. Open the source for the correction audit.')}</p></section>}
        <Facts values={{ account: event.accounts.find((account) => account.group_id === leg.group_id)?.group_name ?? leg.group_id, canonical_asset_identity: leg.canonical_asset_key, execution_status: leg.execution_status, interpretation: leg.interpretation, quantity_role: leg.quantity_role, reported_subtotal: leg.subtotal, reported_total: leg.total, execution_currency: leg.execution_currency, fee: leg.fee, fee_currency: leg.fee_currency, fee_semantics: leg.fee_semantics, fee_payer: leg.fee_payer, valuation_amount: leg.valuation_amount, valuation_currency: leg.valuation_currency, acquisition_basis: leg.acquisition_basis }} />
        <details><summary className="min-h-11 cursor-pointer text-sm">{t('timeline.legIdentity', 'Endpoints, exact units and derivation')}</summary><div className="pt-3"><Facts values={{ chain: leg.chain, token_address: leg.token_address, token_program: leg.token_program, raw_units: leg.raw_units, ...(leg.sender_debit_raw_units != null || leg.receiver_credit_raw_units != null || leg.withheld_fee_raw_units != null ? { sender_debit_raw_units: leg.sender_debit_raw_units, receiver_credit_raw_units: leg.receiver_credit_raw_units, withheld_fee_raw_units: leg.withheld_fee_raw_units } : {}), decimals: leg.decimals, source_address: leg.source_address, destination_address: leg.destination_address, source_owner: leg.source_owner, destination_owner: leg.destination_owner, transaction_ref: leg.transaction_ref, leg_ref: leg.leg_ref, unit_price: leg.unit_price, unit_price_origin: leg.unit_price_origin, external_funding_amount: leg.external_funding_amount, external_funding_currency: leg.external_funding_currency, derivation: leg.derivation }} /></div></details>
        <Reasons reasons={leg.reason_codes} />
        <div className="flex flex-wrap gap-2">{leg.source_ids.map((id) => <Button variant="outline" className="min-h-11" key={id} onClick={() => selectSource(id)}>{t('timeline.openSource', 'Open source')} · {mask(event.sources.find((source) => source.source_id === id)?.source ?? id)}</Button>)}</div>
      </article>)}</div>
    </section>

    <section className="space-y-3" aria-label={t('timeline.sources', 'Supporting sources')}>
      <h3 className="font-semibold">{t('timeline.sources', 'Supporting sources')}</h3>
      <div className="divide-y divide-border">{event.sources.map((source) => <article key={source.source_id} className="space-y-3 py-4">
        <h4 className="break-words text-sm font-medium">{mask(source.source)} · {label(source.source_kind)}</h4>
        <Facts values={{ original_type: source.original_type, provider_status: source.provider_status, network_status: source.network_status, settlement_status: source.settlement_status, observed_at: source.observed_at }} />
        <EventTime time={source.time} />
        {!source.is_current && <p className="text-sm text-muted-foreground">{t('timeline.superseded', 'Superseded source interpretation')}</p>}
        {source.availability === 'unavailable' && <Reasons reasons={[source.unavailable_reason ?? 'source_unavailable']} />}
        <Button variant={sourceId === source.source_id ? 'secondary' : 'outline'} className="min-h-11" aria-pressed={sourceId === source.source_id} onClick={() => selectSource(source.source_id)}>{t('timeline.openSource', 'Open source')} · {mask(source.source)}</Button>
      </article>)}</div>
      {sourceId && (selectedSource ? <SourceDetails key={`${workspaceId}:${sourceId}`} workspaceId={workspaceId} source={selectedSource} scope={scope} /> : <p role="alert" className="text-sm">{t('timeline.sourceOutside', 'This source is not part of the selected event. Choose one of its supporting sources.')}</p>)}
    </section>

    <section className="space-y-3" aria-label={t('timeline.relationships', 'Relationships and review')}>
      <h3 className="font-semibold">{t('timeline.relationships', 'Relationships and review')}</h3>
      {event.linkage === 'candidate' && <p className="text-sm text-muted-foreground">{t('timeline.candidateHelp', 'Candidate records remain separate. A possible relationship does not confirm shared quantities, ownership or carried basis.')}</p>}
      {event.relationships.map((relation, index) => <div key={`${relation.kind}:${relation.review_id ?? relation.leg_id}:${index}`} className="space-y-2 border-t border-border pt-3 text-sm">
        <p>{label(relation.kind)} · {label(relation.state)}{relation.quantity !== null && <> · {t('timeline.allocated', 'Linked quantity')}: {mask(formatExactDecimal(relation.quantity))}</>}</p>
        <Reasons reasons={[...relation.reason_codes, ...relation.conflicting_fields.map((field) => `conflicting_${field}`)]} />
        {relation.review_url?.startsWith('/') && !relation.review_url.startsWith('//') && <Link className={linkClass} to={relation.review_url}>{t('timeline.reviewRelationship', 'Open relationship review')}</Link>}
      </div>)}
      {!!event.mechanics?.length && <details><summary className="min-h-11 cursor-pointer text-sm">{t('investigation.mechanics', 'Supplied conversion and bridge evidence')}</summary><div className="space-y-4 pt-3">{event.mechanics.map((mechanic, index) => <Facts key={index} values={mechanic} />)}</div></details>}
    </section>

    <section className="space-y-3" aria-label={t('timeline.acquisition', 'Acquisition evidence')}>
      <h3 className="font-semibold">{t('timeline.acquisition', 'Acquisition evidence')}: {label(event.basis.state)}</h3>
      <Facts values={{ acquisition_cost: event.basis.acquisition_cost, known_acquisition_cost: event.basis.known_acquisition_cost, unknown_basis_quantity: event.basis.unknown_basis_quantity, tax_treatment: event.tax_treatment }} />
      <Reasons reasons={event.basis.reason_codes} />
      <p className="max-w-prose text-sm text-muted-foreground">{t('timeline.taxHelp', 'Quantity coverage, valuation, ownership and acquisition evidence are separate facts. Movement or a reported incident does not establish tax treatment or a deductible loss.')}</p>
      {event.incidents.length > 0 && <details><summary className="min-h-11 cursor-pointer text-sm">{t('timeline.incidents', 'Reported incidents and their source status')}</summary><pre className="whitespace-pre-wrap break-all text-xs">{mask(JSON.stringify(event.incidents, null, 2))}</pre></details>}
      {event.recovery.length > 0 && <details><summary className="min-h-11 cursor-pointer text-sm">{t('timeline.recovery', 'Recovery evidence relationships')}</summary><pre className="whitespace-pre-wrap break-all text-xs">{mask(JSON.stringify(event.recovery, null, 2))}</pre></details>}
      {event.transfers.length > 0 && <details><summary className="min-h-11 cursor-pointer text-sm">{t('timeline.transferLineage', 'Supplied transfer and acquisition lineage')}</summary><pre className="whitespace-pre-wrap break-all text-xs">{mask(JSON.stringify(event.transfers, null, 2))}</pre></details>}
    </section>
    <div className="flex flex-wrap items-center gap-4">
      {nativeTraceEnabled && (event.native_trace_url?.startsWith('/assets?')
        ? <Link className={linkClass} to={event.native_trace_url}>{t('timeline.inspectNative', 'Inspect native transfers')}</Link>
        : event.accounts.filter((account, index, accounts) => account.group_id && accounts.findIndex((other) => other.group_id === account.group_id) === index).map((account) => <Link key={account.group_id} className={linkClass} to={`/assets?${new URLSearchParams({ tab: 'activity', activity: 'wallets', wallet: account.group_id! })}`}>{t('timeline.chooseNative', 'Choose saved address')} · {mask(account.group_name ?? t('timeline.unmapped', 'Unmapped account'))}</Link>))}
      <BalanceDetails workspaceId={workspaceId} wallets={wallets.filter((wallet) => event.accounts.some((account) => account.group_id === wallet.id))} />
    </div>
    {nativeTraceEnabled && <p className="text-sm text-muted-foreground">{t('timeline.nativeHelp', 'Native investigation starts only when submitted. Its bounds and terminal reasons describe research coverage; downstream pooled activity does not prove continuing ownership.')}</p>}
    {nativeTraceEnabled && <EvidenceInvestigation key={`${workspaceId}:${event.event_id}`} event={event} workspaceId={workspaceId} labelAsset={(asset) => assetLabel(asset, t('timeline.unknownAsset', 'Asset identity unresolved'))} renderEvent={(selected) => <InvestigationEventDetails key={selected.event_id} event={selected} workspaceId={workspaceId} wallets={wallets} />} />}
  </div>
}

export function OwnedCoinTimeline({ workspaceId, collectionId, groupId, wallets, nativeTraceEnabled }: {
  workspaceId: string; collectionId: string | null; groupId: string | null; wallets: AssetGroup[]; nativeTraceEnabled: boolean
}) {
  const { t } = useTranslation()
  const { mask } = usePrivacyMode()
  const [params, setParams] = useSearchParams()
  const currentParams = useRef(params)
  useLayoutEffect(() => {
    currentParams.current = params
  }, [params, workspaceId, collectionId, groupId])
  const writeParams = useCallback((edit: (next: URLSearchParams) => void, replace = false) => {
    // Router callbacks capture a render snapshot, not a queued state update.
    // Preserve pending edits synchronously; committed navigation also resets it.
    const next = new URLSearchParams(currentParams.current)
    edit(next)
    if (next.toString() === currentParams.current.toString()) return
    currentParams.current = next
    setParams(next, { replace })
  }, [setParams])
  const focusTarget = useRef<HTMLElement | null>(null)
  const label = (value: string) => t(`timeline.values.${value}`, value.replaceAll('_', ' '))
  const scope: TimelineFilters = {
    ...(collectionId ? { collection_id: collectionId } : {}),
    ...(groupId ? { group_id: groupId } : {}),
    ...(params.get('canonical_asset_key') ? { canonical_asset_key: params.get('canonical_asset_key')! } : params.get('asset_id') ? { asset_id: params.get('asset_id')! } : {}),
  }
  const since = params.get('timeline_since') ?? ''
  const until = params.get('timeline_until') ?? ''
  const validDate = (value: string) => !value || (/^\d{4}-\d{2}-\d{2}$/.test(value) && Number.isFinite(Date.parse(`${value}T00:00:00Z`)))
  const invalidWindow = !validDate(since) || !validDate(until) || (!!since && !!until && until < since)
  const offset = Math.max(0, Number(params.get('timeline_offset')) || 0)
  const revision = params.get('timeline_revision') ?? undefined
  const missingRevision = offset > 0 && !revision
  const filters: TimelineFilters = {
    ...scope, limit: 25, offset,
    ...Object.fromEntries(['source', 'status', 'kind', 'direction'].flatMap((key) => params.get(`timeline_${key}`) ? [[key, params.get(`timeline_${key}`)!]] : [])),
    ...(since && !invalidWindow ? { since: `${since}T00:00:00Z` } : {}),
    ...(until && !invalidWindow ? { until: `${until}T23:59:59.999999Z` } : {}),
    ...(offset > 0 && revision ? { expected_revision: revision } : {}),
  }
  const query = useQuery({ queryKey: ['investment-timeline', workspaceId, filters], queryFn: ({ signal }) => timeline.list(workspaceId, filters, signal), enabled: !invalidWindow && !missingRevision, retry: false })
  const data = query.data?.workspace_id === workspaceId ? query.data : undefined
  const eventId = params.get('event')
  const detail = useQuery({ queryKey: ['investment-timeline-event', workspaceId, eventId, scope], queryFn: ({ signal }) => timeline.event(workspaceId, eventId!, scope, signal), enabled: !!eventId, retry: false })
  const event = detail.data
  useEffect(() => {
    if (!eventId || !event || event.event_id === eventId) return
    writeParams((next) => {
      if (next.get('event') === eventId) next.set('event', event.event_id)
    }, true)
  }, [eventId, event, writeParams])
  const changedScope = (query.error as { response?: { status?: number } } | null)?.response?.status === 409
  const resolvedAssets = data?.assets.filter((asset) => !!scope.asset_id && asset.asset_ids.includes(scope.asset_id))
  const resolvedKey = resolvedAssets?.length === 1 ? resolvedAssets[0].canonical_asset_key : null
  useEffect(() => {
    if (!resolvedKey || !scope.asset_id) return
    writeParams((next) => {
      if (next.get('asset_id') !== scope.asset_id) return
      next.set('canonical_asset_key', resolvedKey)
      next.delete('asset_id')
    }, true)
  }, [resolvedKey, scope.asset_id, writeParams])

  const update = (changes: Record<string, string | null>, page = false) => {
    writeParams((next) => {
      for (const key of ['event', 'event_source', 'investigation_event', ...(!page ? ['timeline_offset', 'timeline_revision'] : [])]) next.delete(key)
      for (const [key, value] of Object.entries(changes)) { if (value) next.set(key, value); else next.delete(key) }
    })
  }
  const select = (key: 'event' | 'event_source', id: string) => {
    if (key === 'event') focusTarget.current = document.activeElement instanceof HTMLElement ? document.activeElement : null
    writeParams((next) => {
      next.set(key, id)
      if (key === 'event') { next.delete('event_source'); next.delete('investigation_event') }
    })
  }
  const close = () => writeParams((next) => { next.delete('event'); next.delete('event_source'); next.delete('investigation_event') }, true)

  return <section className="min-w-0 space-y-6" aria-label={t('timeline.title', 'Evidence timeline')}>
    <div className="space-y-2"><h2 className="text-lg font-semibold">{t('timeline.title', 'Evidence timeline')}</h2><p className="max-w-prose text-sm text-muted-foreground">{t('timeline.intro', 'Follow retained acquisitions, movements and source evidence across your accounts. Sold, zero-balance and archived assets remain available here.')}</p></div>
    <div className="grid min-w-0 gap-3 sm:grid-cols-2 lg:grid-cols-4">
      <div className="min-w-0 space-y-1.5 sm:col-span-2"><Label htmlFor="timeline-asset">{t('timeline.coin', 'Coin / canonical asset')}</Label><NativeSelect id="timeline-asset" className={selectClass} value={scope.canonical_asset_key ?? ''} onChange={(e) => update({ canonical_asset_key: e.target.value, asset_id: null })}>
        <option value="">{scope.asset_id ? t('timeline.resolvingHolding', 'Selected holding · resolving identity') : t('timeline.allAssets', 'All retained assets')}</option>
        {scope.canonical_asset_key && !data?.assets.some((asset) => asset.canonical_asset_key === scope.canonical_asset_key) && <option value={scope.canonical_asset_key}>{t('timeline.selectedAsset', 'Selected canonical asset')}</option>}
        {data?.assets.map((asset) => <option key={asset.canonical_asset_key} value={asset.canonical_asset_key}>{mask([assetLabel(asset, t('timeline.unknownAsset', 'Asset identity unresolved')), asset.asset_symbol ? asset.chain : null, asset.token_program, asset.asset_symbol ? asset.token_address : null].filter(Boolean).join(' · '))}{asset.identity_status !== 'canonical' ? ` · ${label(asset.identity_status)} identity` : ''}</option>)}
      </NativeSelect></div>
      {(['kind', 'status', 'direction', 'source'] as const).map((key) => {
        const choices = key === 'kind' ? ['acquisition', 'disposal', 'transfer', 'fee', 'income', 'reward', 'swap', 'bridge', 'wrap', 'unwrap', 'unknown', 'unclassified'] : key === 'status' ? ['settled', 'provisional', 'pending', 'failed', 'unknown', 'confirmed', 'candidate', 'conflicting', 'unresolved'] : key === 'direction' ? ['in', 'out', 'unknown'] : [...new Set(data?.coverage.map((entry) => entry.source) ?? [])]
        const selected = params.get(`timeline_${key}`) ?? ''
        if (selected && !choices.includes(selected)) choices.push(selected)
        return <div key={key} className="min-w-0 space-y-1.5"><Label htmlFor={`timeline-${key}`}>{t(`timeline.filter.${key}`, key[0].toUpperCase() + key.slice(1))}</Label><NativeSelect id={`timeline-${key}`} className={selectClass} value={selected} onChange={(e) => update({ [`timeline_${key}`]: e.target.value })}><option value="">{t('timeline.all', 'All')}</option>{choices.map((value) => <option key={value} value={value}>{key === 'source' ? mask(value) : label(value)}</option>)}</NativeSelect></div>
      })}
      {(['since', 'until'] as const).map((key) => <div key={key} className="space-y-1.5"><Label htmlFor={`timeline-${key}`}>{key === 'since' ? t('timeline.from', 'From (UTC)') : t('timeline.through', 'Through (UTC)')}</Label><Input className="min-h-11 min-w-0 text-base sm:text-sm" id={`timeline-${key}`} type="date" value={key === 'since' ? since : until} onChange={(e) => update({ [`timeline_${key}`]: e.target.value })} /></div>)}
    </div>
    <div className="flex flex-wrap items-center gap-3"><Button variant="outline" className="min-h-11" onClick={() => update(Object.fromEntries(['canonical_asset_key', 'asset_id', ...filterKeys.map((key) => `timeline_${key}`)].map((key) => [key, null])))}>{t('timeline.clear', 'Clear timeline filters')}</Button><p className="text-xs text-muted-foreground">{t('timeline.orderHelp', 'Newest reported dates first. Tied and undated records retain their ordering uncertainty.')}</p></div>
    {invalidWindow && <p role="alert" className="text-sm text-warning-foreground">{t('timeline.invalidDates', 'Use valid dates, with the end on or after the start.')}</p>}
    {missingRevision || changedScope ? <RequestError restart retry={() => update({ timeline_offset: null, timeline_revision: null })} /> : query.isError || (query.isSuccess && !data) ? <RequestError retry={() => { void query.refetch() }} /> : null}
    {query.isPending && !invalidWindow && !missingRevision && <p role="status" className="text-sm">{t('timeline.loading', 'Loading retained activity…')}</p>}
    {data && !invalidWindow && !changedScope && <>
      {data.errors.length > 0 && <div role="alert" className="space-y-2 text-sm text-warning-foreground"><p>{t('timeline.partialRequest', 'Some evidence could not be included. Available events remain visible; coverage is incomplete.')}</p><Button variant="outline" onClick={() => { void query.refetch() }}>{t('common.retry', 'Retry')}</Button></div>}
      <Coverage entries={data.coverage} />
      {!data.events.length ? <p role="status" className="border-y border-border py-8 text-sm text-muted-foreground">{t('timeline.empty', 'No events match this available scope. This does not establish an empty account or the absence of earlier ownership.')}</p> : <div className="divide-y divide-border border-y border-border">{data.events.map((row) => <Button key={row.event_id} variant="ghost" className="h-auto min-h-20 w-full justify-start whitespace-normal rounded-none px-2 py-4 text-left" onClick={() => select('event', row.event_id)}>
        <span className="grid min-w-0 flex-1 gap-3 md:grid-cols-[minmax(0,1fr)_minmax(0,2fr)_minmax(0,1fr)]">
          <span className="text-sm font-normal"><EventTime time={row.time} /></span>
          <span className="min-w-0 space-y-1"><strong className="block">{label(row.kind)} · {label(row.status)}</strong><span className="block break-words text-sm font-normal">{row.accounts.map((account) => mask(account.group_name ?? t('timeline.unmapped', 'Unmapped account'))).join(' · ')}</span><span className="block break-words text-xs font-normal text-muted-foreground">{row.sources.map((source) => mask(source.source)).join(' · ')} · {t('timeline.linkage', 'Linkage')}: {label(row.linkage)}</span></span>
          <span className="space-y-1 md:text-right">{row.legs.slice(0, 3).map((leg) => <span key={leg.leg_id} className="block break-all text-sm font-normal tabular-nums">{label(leg.classification)} · {label(leg.direction)} · {leg.quantity == null ? t('timeline.unknown', 'Unknown') : mask(formatExactDecimal(leg.quantity))} {mask(assetLabel(leg, t('timeline.unknownAsset', 'Asset identity unresolved')))}</span>)}{row.legs.length > 3 && <span className="block text-xs font-normal text-muted-foreground">{t('timeline.moreLegs', '{{count}} more legs in details', { count: row.legs.length - 3 })}</span>}{!row.legs.length && <span className="text-xs font-normal text-muted-foreground">{t('timeline.interpretationMissing', 'Movement interpretation unavailable')}</span>}</span>
        </span>
      </Button>)}</div>}
      <div className="flex flex-wrap items-center justify-between gap-3"><Button variant="outline" className="min-h-11" disabled={data.offset === 0} onClick={() => update({ timeline_offset: String(Math.max(0, data.offset - data.limit)), timeline_revision: data.revision }, true)}>{t('common.previous', 'Previous')}</Button><p className="text-sm tabular-nums">{t('timeline.page', 'Page {{page}} · {{total}} available events', { page: Math.floor(data.offset / data.limit) + 1, total: data.total })}</p><Button variant="outline" className="min-h-11" disabled={!data.has_more} onClick={() => update({ timeline_offset: String(data.offset + data.limit), timeline_revision: data.revision }, true)}>{t('common.next', 'Next')}</Button></div>
      <p role="status" className="text-sm text-muted-foreground">{data.all_available_records_loaded ? t('timeline.allLoaded', 'All available records loaded for these filters. History completeness remains unresolved.') : t('timeline.moreAvailable', 'More available records may remain. This is a bounded view of retained history.')}</p>
    </>}
    <Dialog open={!!eventId} onOpenChange={(open) => { if (!open) close() }}><DialogContent className="min-w-0 sm:max-w-3xl" onCloseAutoFocus={(e) => { if (focusTarget.current?.isConnected) { e.preventDefault(); focusTarget.current.focus() } }}>
      <DialogHeader className="pr-6 text-left"><DialogTitle>{t('timeline.eventDetail', 'Event evidence')}</DialogTitle><DialogDescription>{t('timeline.detailHelp', 'Inspect supplied event facts and original sources separately. Imported observations are read-only here.')}</DialogDescription></DialogHeader>
      {detail.isPending && <p role="status">{t('timeline.loadingEvent', 'Loading event evidence…')}</p>}
      {(detail.isError || (detail.isSuccess && !event)) && <RequestError retry={() => { void detail.refetch() }} />}
      {event && <EventDetails event={event} workspaceId={workspaceId} scope={scope} sourceId={params.get('event_source')} selectSource={(id) => select('event_source', id)} nativeTraceEnabled={nativeTraceEnabled} wallets={wallets} />}
    </DialogContent></Dialog>
  </section>
}
