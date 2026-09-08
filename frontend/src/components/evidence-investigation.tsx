import { useEffect, useRef, useState, type FormEvent, type ReactNode } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useSearchParams } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import { onchain } from '@/lib/api'
import { useWorkspace } from '@/contexts/workspace-context'
import { usePrivacyMode } from '@/hooks/use-privacy-mode'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { NativeSelect } from '@/components/ui/native-select'
import { Alert } from '@/components/ui/alert'
import type { TimelineAsset, TimelineEvent } from '@/types/timeline'
import type { BridgeCandidate, BridgeReviewRequest, InvestigationContinueRequest, InvestigationRead, InvestigationRequest } from '@/types/investigation'

type Operation = { generation: number } & (
  | { kind: 'preview'; request: InvestigationRequest }
  | { kind: 'continue'; request: InvestigationContinueRequest }
)

function BridgeReview({ eventId, workspaceId, onReviewed }: { eventId: string; workspaceId: string; onReviewed: () => void }) {
  const { t } = useTranslation()
  const { canWrite } = useWorkspace()
  const { mask } = usePrivacyMode()
  const client = useQueryClient()
  const [open, setOpen] = useState(false)
  const active = useRef(true)
  useEffect(() => { active.current = true; return () => { active.current = false } }, [])
  const query = useQuery({ queryKey: ['onchain', 'bridge-candidates', workspaceId, eventId], queryFn: ({ signal }) => onchain.bridgeCandidates(eventId, workspaceId, signal), enabled: open, retry: false })
  const review = useMutation({ mutationFn: (request: BridgeReviewRequest) => onchain.reviewBridge(request, workspaceId), retry: false, onSuccess: () => {
    if (!active.current) return
    void client.invalidateQueries({ queryKey: ['onchain', 'bridge-candidates', workspaceId, eventId] })
    void client.invalidateQueries({ queryKey: ['investment-timeline', workspaceId] })
    onReviewed()
  } })
  const confirm = (candidate: BridgeCandidate) => {
    if (!canWrite || candidate.status !== 'eligible' || !candidate.collection_id || !candidate.revision || review.isPending) return
    review.mutate({ collection_id: candidate.collection_id, expected_revision: candidate.revision, source_event_id: candidate.source_event_id, source_leg_id: candidate.source_leg_id, destination_event_id: candidate.destination_event_id, destination_leg_id: candidate.destination_leg_id, source_id: candidate.source_id, destination_source_id: candidate.destination_source_id, reviewed: true })
  }
  return <section className="space-y-3 border-t border-border pt-4" aria-label={t('investigation.bridgeTitle', 'Bridge endpoint review')}>
    <Button variant="outline" className="min-h-11" aria-expanded={open} onClick={() => setOpen(!open)}>{t('investigation.bridgeTitle', 'Bridge endpoint review')}</Button>
    {open && <div className="space-y-4"><p className="max-w-prose text-sm text-muted-foreground">{t('investigation.bridgeHelp', 'Review independently retained send and receipt evidence before connecting a bridge. A source send alone cannot establish destination execution, ownership or tax treatment.')}</p>
      {query.isPending && <p role="status" className="text-sm">{t('investigation.previewing', 'Reading retained evidence…')}</p>}
      {query.isError && <Alert variant="warning"><span>{t('investigation.bridgeError', 'Bridge evidence could not be loaded.')}</span><Button variant="outline" onClick={() => void query.refetch()}>{t('common.retry')}</Button></Alert>}
      {query.isSuccess && !query.data.length && <p className="text-sm">{t('investigation.noBridge', 'No corroborated bridge endpoint pair is available for this event. Retain the missing source evidence before reviewing a link.')}</p>}
      {query.data?.map((candidate, index) => <div key={`${candidate.source_leg_id}:${candidate.destination_leg_id}:${index}`} className="space-y-3 border-t border-border pt-3"><p className="break-all text-sm font-medium">{mask(candidate.protocol)} · {mask(candidate.message_id)} · {t(`investigation.values.${candidate.status}`, candidate.status)}</p><div className="grid min-w-0 gap-4 sm:grid-cols-2">{(['source_summary', 'destination_summary'] as const).map((side) => <div key={side} className="min-w-0 space-y-2"><h4 className="text-sm font-medium">{side === 'source_summary' ? t('investigation.bridgeSend', 'Source execution') : t('investigation.bridgeReceipt', 'Destination execution')}</h4><dl className="space-y-2 text-sm">{Object.entries(candidate[side] ?? {}).map(([name, value]) => <div key={name}><dt className="text-muted-foreground">{t(`investigation.fields.${name}`, name.replaceAll('_', ' '))}</dt><dd className="break-all">{value == null ? t('history.unknown', 'Unknown') : mask(typeof value === 'object' ? JSON.stringify(value) : String(value))}</dd></div>)}</dl></div>)}</div><ul className="list-inside list-disc text-sm">{candidate.reason_codes.map((reason) => <li key={reason}>{t(`investigation.values.${reason}`, reason.replaceAll('_', ' '))}</li>)}</ul>{!!candidate.omitted_candidates && <p className="text-sm">{t('investigation.omittedBridgeCandidates', '{{count}} additional candidates were not examined. Narrow the retained source scope before reviewing.', { count: candidate.omitted_candidates })}</p>}<Button className="min-h-11" disabled={!canWrite || candidate.status !== 'eligible' || !candidate.collection_id || !candidate.revision || review.isPending} onClick={() => confirm(candidate)}>{t('investigation.confirmBridge', 'Confirm reviewed bridge endpoints')}</Button></div>)}
      {review.isPending && <p role="status" className="text-sm">{t('investigation.savingBridge', 'Saving the evidence relationship…')}</p>}
      {review.isError && <Alert variant="warning"><span>{t('investigation.bridgeChanged', 'The bridge could not be confirmed. Reload its sources and resolve missing or conflicting evidence before retrying.')}</span><Button variant="outline" onClick={() => { review.reset(); void query.refetch() }}>{t('investigation.reloadBridge', 'Reload bridge evidence')}</Button></Alert>}
      {review.isSuccess && <p role="status" className="text-sm">{t('investigation.bridgeSaved', 'Reviewed relationship saved. Preview the trail again to inspect both endpoints.')}</p>}
    </div>}
  </section>
}

export function EvidenceInvestigation({ event, workspaceId, renderEvent, labelAsset }: {
  event: TimelineEvent
  workspaceId: string
  renderEvent: (event: TimelineEvent) => ReactNode
  labelAsset: (asset: Pick<TimelineAsset, 'asset_symbol' | 'chain' | 'token_address'>) => string
}) {
  const { t } = useTranslation()
  const { canWrite } = useWorkspace()
  const { mask } = usePrivacyMode()
  const client = useQueryClient()
  const [params, setParams] = useSearchParams()
  const [legId, setLegId] = useState(event.legs[0]?.leg_id ?? '')
  const [direction, setDirection] = useState<'in' | 'out'>('out')
  const [since, setSince] = useState('')
  const [until, setUntil] = useState('')
  const [hops, setHops] = useState('3')
  const [branches, setBranches] = useState('3')
  const [minimum, setMinimum] = useState('')
  const [result, setResult] = useState<InvestigationRead | null>(null)
  const generation = useRef(0)
  useEffect(() => () => { generation.current += 1 }, [])
  const label = (value: string) => t(`investigation.values.${value}`, value.replaceAll('_', ' '))
  const leg = event.legs.find((item) => item.leg_id === legId)
  const coin = (key: string) => {
    const match = [event, ...(result?.events ?? [])].flatMap((item) => item.legs).find((item) => item.canonical_asset_key === key)
    return match ? labelAsset(match) : t('timeline.unknownAsset', 'Asset identity unresolved')
  }
  const invalidWindow = Boolean(since && until && since > until)
  const invalidMinimum = minimum !== '' && !/^\d+(\.\d+)?$/.test(minimum)
  const mutation = useMutation({
    retry: false,
    mutationFn: (operation: Operation) => operation.kind === 'preview'
      ? onchain.previewInvestigation(operation.request, workspaceId)
      : onchain.continueInvestigation(operation.request, workspaceId),
    onSuccess: (data, operation) => {
      if (operation.generation !== generation.current || data.workspace_id !== workspaceId) return
      setResult(data)
      if (operation.kind === 'continue') {
        void client.invalidateQueries({ queryKey: ['onchain', 'history', workspaceId] })
        void client.invalidateQueries({ queryKey: ['investment-timeline', workspaceId] })
      }
    },
  })
  const invalidate = () => {
    generation.current += 1
    mutation.reset()
    setResult(null)
  }
  const preview = (e: FormEvent) => {
    e.preventDefault()
    if (!leg || invalidWindow || invalidMinimum || mutation.isPending) return
    mutation.mutate({ kind: 'preview', generation: ++generation.current, request: {
      event_id: event.event_id, leg_id: legId, direction, max_hops: Number(hops), max_branches: Number(branches),
      minimums: minimum ? { [leg.canonical_asset_key]: minimum } : {},
      ...(since ? { since: new Date(`${since}Z`).toISOString() } : {}),
      ...(until ? { until: new Date(`${until}Z`).toISOString() } : {}),
    } })
  }
  const continueFrontier = (key: string) => {
    if (!canWrite || !result?.collection_id || !result.revision || mutation.isPending) return
    mutation.mutate({ kind: 'continue', generation: ++generation.current, request: {
      ...result.request, collection_id: result.collection_id, expected_revision: result.revision, frontier_key: key,
    } })
  }
  const selectEvent = (id?: string) => {
    const next = new URLSearchParams(params)
    if (id) next.set('investigation_event', id)
    else next.delete('investigation_event')
    setParams(next)
  }
  const selectedEvent = result?.events.find((item) => item.event_id === params.get('investigation_event'))
  const errorCode = (mutation.error as { response?: { data?: { detail?: { code?: string } } } } | null)?.response?.data?.detail?.code
  const error = errorCode === 'history_revision_conflict' || errorCode === 'history_restart_required'
    ? t('investigation.changed', 'The saved evidence or source configuration changed. Preview the trail again before continuing.')
    : t('investigation.failed', 'This investigation request could not finish. Retained evidence remains visible. Retry the request or preview again.')

  return <section className="min-w-0 space-y-4 border-t border-border pt-6" aria-label={t('investigation.title', 'Follow evidence')}>
    <div className="space-y-2"><h3 className="font-semibold">{t('investigation.title', 'Follow evidence')}</h3><p className="max-w-prose text-sm text-muted-foreground">{t('investigation.help', 'Inspect retained acquisition, transfer and conversion evidence in either direction. Previewing uses saved sources. Collecting a selected continuation contacts its chain provider and retains evidence without assigning ownership or basis.')}</p></div>
    <form className="space-y-4" onSubmit={preview}>
      <div className="grid min-w-0 gap-3 sm:grid-cols-2">
        <div className="min-w-0 space-y-1.5"><Label htmlFor="investigation-leg">{t('investigation.leg', 'Starting movement')}</Label><NativeSelect id="investigation-leg" className="min-h-11 text-base sm:text-sm" value={legId} onChange={(e) => { setLegId(e.target.value); setMinimum(''); invalidate() }}>{event.legs.map((item, index) => <option key={item.leg_id} value={item.leg_id}>{mask(`${labelAsset(item)} · ${item.quantity ?? t('history.unknown', 'Unknown')} · ${label(item.direction)} · ${t('investigation.legNumber', 'Leg {{number}}', { number: index + 1 })}`)}</option>)}</NativeSelect></div>
        <div className="space-y-1.5"><Label htmlFor="investigation-direction">{t('investigation.direction', 'Follow direction')}</Label><NativeSelect id="investigation-direction" className="min-h-11 text-base sm:text-sm" value={direction} onChange={(e) => { setDirection(e.target.value as 'in' | 'out'); invalidate() }}><option value="out">{t('investigation.forward', 'Forward to recipients')}</option><option value="in">{t('investigation.backward', 'Backward to sources')}</option></NativeSelect></div>
      </div>
      <details><summary className="min-h-11 cursor-pointer text-sm">{t('investigation.bounds', 'Investigation bounds')}</summary><div className="grid min-w-0 gap-3 py-3 sm:grid-cols-2">
        <div className="space-y-1.5"><Label htmlFor="investigation-since">{t('investigation.since', 'Trail from (UTC)')}</Label><Input id="investigation-since" type="datetime-local" step="any" value={since} max={until || undefined} onChange={(e) => { setSince(e.target.value); invalidate() }} /></div>
        <div className="space-y-1.5"><Label htmlFor="investigation-until">{t('investigation.until', 'Trail through (UTC)')}</Label><Input id="investigation-until" type="datetime-local" step="any" value={until} min={since || undefined} onChange={(e) => { setUntil(e.target.value); invalidate() }} /></div>
        <div className="space-y-1.5"><Label htmlFor="investigation-hops">{t('trace.maxHops', 'Maximum hops')}</Label><NativeSelect id="investigation-hops" value={hops} onChange={(e) => { setHops(e.target.value); invalidate() }}>{[1, 2, 3, 4, 5, 6].map((value) => <option key={value}>{value}</option>)}</NativeSelect></div>
        <div className="space-y-1.5"><Label htmlFor="investigation-branches">{t('trace.maxBranches', 'Maximum branches')}</Label><NativeSelect id="investigation-branches" value={branches} onChange={(e) => { setBranches(e.target.value); invalidate() }}>{[1, 2, 3, 4, 5].map((value) => <option key={value}>{value}</option>)}</NativeSelect></div>
        <div className="min-w-0 space-y-1.5 sm:col-span-2"><Label htmlFor="investigation-minimum">{t('investigation.minimum', 'Minimum quantity in the starting asset')}</Label><Input id="investigation-minimum" inputMode="decimal" value={minimum} maxLength={100} onChange={(e) => { setMinimum(e.target.value); invalidate() }} /><p className="break-all text-xs text-muted-foreground">{leg && mask(labelAsset(leg))} · {t('investigation.units', 'This minimum applies only to this asset. Converted assets keep their own units.')}</p></div>
      </div></details>
      {(invalidWindow || invalidMinimum) && <p role="alert" className="text-sm text-warning-foreground">{invalidWindow ? t('history.invalidWindow', 'The start must be on or before the end.') : t('investigation.invalidMinimum', 'Enter a nonnegative decimal quantity.')}</p>}
      <Button className="min-h-11" type="submit" disabled={!leg || invalidWindow || invalidMinimum || mutation.isPending}>{t('investigation.preview', 'Preview saved trail')}</Button>
    </form>
    <BridgeReview eventId={event.event_id} workspaceId={workspaceId} onReviewed={invalidate} />
    {mutation.isPending && <p role="status" className="text-sm">{mutation.variables?.kind === 'continue' ? t('investigation.collecting', 'Collecting the selected continuation…') : t('investigation.previewing', 'Reading retained evidence…')}</p>}
    {mutation.isError && <Alert variant="warning" className="block space-y-3"><p>{error}</p><Button variant="outline" className="min-h-11" onClick={() => { if (mutation.variables) mutation.mutate({ ...mutation.variables, generation: ++generation.current }) }}>{t('common.retry')}</Button></Alert>}
    {result && <div className="min-w-0 space-y-5" aria-label={t('investigation.result', 'Evidence trail')}>
      <p className="max-w-prose text-sm text-muted-foreground">{t('investigation.scope', 'This is a bounded evidence trail. Missing acquisition, incomplete history and unresolved tax treatment remain unknown. External or pooled movements do not establish your share or continuing ownership.')}</p>
      {result.evidence && <section className="space-y-3" aria-label={t('investigation.continuationCoverage', 'Continuation coverage')}><h4 className="text-sm font-medium">{t('investigation.continuationCoverage', 'Continuation coverage')}</h4><dl className="grid gap-3 text-sm sm:grid-cols-2">{Object.entries(result.evidence.coverage ?? {}).map(([axis, value]) => <div key={axis}><dt className="text-muted-foreground">{label(axis)}</dt><dd>{label(value)}</dd></div>)}<div><dt className="text-muted-foreground">{t('investigation.attempts', 'Provider attempts in this request')}</dt><dd>{String(result.evidence.limits?.attempts_used ?? t('history.unknown', 'Unknown'))} / {String(result.evidence.limits?.attempts ?? t('history.unknown', 'Unknown'))}</dd></div></dl>{!!result.evidence.gaps?.length && <Alert variant="warning"><ul className="list-inside list-disc text-sm">{result.evidence.gaps.map((gap, index) => <li key={index}>{label(gap)}</li>)}</ul></Alert>}</section>}
      {!!result.boundaries.length && <Alert variant="warning"><ul className="list-inside list-disc space-y-1 text-sm">{result.boundaries.map((boundary, index) => <li key={`${boundary.code}:${index}`}>{label(boundary.code)}</li>)}</ul></Alert>}
      {!result.steps.length && <p className="text-sm">{t('investigation.empty', 'No connected steps are supported within these bounds. Inspect the starting event and coverage gaps.')}</p>}
      <ol className="divide-y divide-border">{result.steps.map((step, index) => <li className="space-y-2 py-3" key={`${step.event_id}:${step.leg_id}:${index}`}>
        <p className="break-all text-sm">{t('investigation.hop', 'Hop')} {step.depth} · {mask(coin(step.asset_key))} · {label(step.via)}</p>
        <p className="break-words text-xs text-muted-foreground">{t('investigation.window', 'Effective UTC window')}: {mask(step.effective_window.since ?? t('history.openStart', 'No lower bound'))} → {mask(step.effective_window.until ?? t('history.openEnd', 'No upper bound'))}</p>
        <Button variant="outline" className="min-h-11" disabled={!result.events.some((item) => item.event_id === step.event_id)} onClick={() => selectEvent(step.event_id)}>{t('investigation.inspect', 'Inspect event and all legs')}</Button>
      </li>)}</ol>
      {!!result.frontier.length && <section className="space-y-3" aria-label={t('investigation.continuations', 'Available continuations')}><h4 className="font-medium">{t('investigation.continuations', 'Available continuations')}</h4>{result.frontier.map((item) => <div key={item.key} className="space-y-2 border-t border-border py-3"><p className="break-all text-sm">{mask(item.chain)} · {mask(item.address)} · {mask(coin(item.asset_key))}</p><p className="break-words text-xs text-muted-foreground">{mask(item.since ?? t('history.openStart', 'No lower bound'))} → {mask(item.until ?? t('history.openEnd', 'No upper bound'))}</p><Button variant="outline" className="min-h-11" disabled={!canWrite || !result.collection_id || !result.revision || item.resumable === false || mutation.isPending} onClick={() => continueFrontier(item.key)}>{t('investigation.collect', 'Collect selected continuation')}</Button>{item.resumable === false && <p className="text-xs text-muted-foreground">{t('investigation.exhausted', 'No remaining pages in this declared continuation. Coverage gaps may remain.')}</p>}</div>)}{!canWrite && <p className="text-sm text-muted-foreground">{t('history.viewer', 'Viewers can open and export saved evidence. An editor can collect new evidence.')}</p>}</section>}
      {selectedEvent && <section className="min-w-0 space-y-4 border-t border-border pt-4"><Button variant="outline" className="min-h-11" onClick={() => selectEvent()}>{t('investigation.back', 'Back to trail')}</Button>{renderEvent(selectedEvent)}</section>}
    </div>}
  </section>
}
