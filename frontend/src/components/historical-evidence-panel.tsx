import { useEffect, useRef, useState, type FormEvent } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { Download } from 'lucide-react'
import { onchain } from '@/lib/api'
import { useWorkspace } from '@/contexts/workspace-context'
import { usePrivacyMode } from '@/hooks/use-privacy-mode'
import { Alert } from '@/components/ui/alert'
import { Button } from '@/components/ui/button'
import { Card, CardContent } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { NativeSelect } from '@/components/ui/native-select'
import { Skeleton } from '@/components/ui/skeleton'
import type { HistoryAsset, HistoryCollection, HistoryRequest } from '@/types'

const selectClass = 'h-10 rounded-md border border-input bg-background px-3 text-sm'

function assetIdentity(asset: HistoryAsset | string): string {
  return typeof asset === 'string' ? asset : asset.native ? `${asset.chain}:native` : `${asset.chain}:${asset.token_program ?? 'unknown-program'}:${asset.mint ?? 'unknown-mint'}`
}

type Operation = { generation: number } & (
  | { kind: 'collect'; request: HistoryRequest }
  | { kind: 'open'; id: string }
  | { kind: 'export'; id: string }
)

export function HistoricalEvidencePanel({ workspaceId, initial, connectionIds, addressKeys }: {
  workspaceId: string
  initial: { selectedKey: string; since: string; until: string }
  connectionIds?: string[]
  addressKeys?: string[]
}) {
  const { t } = useTranslation()
  const { canWrite } = useWorkspace()
  const { mask, privacyMode } = usePrivacyMode()
  const client = useQueryClient()
  const [selectedKey, setSelectedKey] = useState(initial.selectedKey)
  const [since, setSince] = useState(initial.since)
  const [until, setUntil] = useState(initial.until)
  const [supplied, setSupplied] = useState('')
  const [reviewed, setReviewed] = useState(false)
  const [ownershipConfirmed, setOwnershipConfirmed] = useState(false)
  const [savedId, setSavedId] = useState('')
  const [result, setResult] = useState<HistoryCollection | null>(null)
  const [assetFilter, setAssetFilter] = useState('')
  const [statusFilter, setStatusFilter] = useState('')
  const [page, setPage] = useState(0)
  const generation = useRef(0)
  useEffect(() => () => { generation.current += 1 }, [])
  const unknown = t('history.unknown', 'Unknown')
  const label = (value: string) => t(`history.values.${value}`, { defaultValue: value.replaceAll('_', ' ') })
  const moment = (value: string | number | null | undefined) => {
    if (value == null) return unknown
    const date = new Date(typeof value === 'number' ? value * 1000 : value)
    return Number.isNaN(date.getTime()) ? unknown : date.toISOString().replace('T', ' ').replace('Z', ' UTC')
  }
  const watchedQuery = useQuery({
    queryKey: ['onchain', 'addresses', workspaceId],
    queryFn: ({ signal }) => onchain.addresses(workspaceId, signal),
  })
  const watched = (watchedQuery.data ?? []).filter((entry) =>
    (connectionIds === undefined || connectionIds.includes(entry.connection_id)) &&
    (addressKeys === undefined || addressKeys.includes(`${entry.chain}:${entry.address}`)),
  )
  const selected = watched.find((entry) => `${entry.connection_id}:${entry.chain}:${entry.address}` === selectedKey || `${entry.chain}:${entry.address}` === selectedKey)
  const savedQuery = useQuery({
    queryKey: ['onchain', 'history', workspaceId, selected?.connection_id, selected?.address],
    queryFn: ({ signal }) => onchain.histories(workspaceId, selected?.connection_id, selected?.address, signal),
  })
  const savedCollections = (savedQuery.data ?? []).filter((entry) =>
    (connectionIds === undefined || connectionIds.includes(entry.request.connection_id)) &&
    (addressKeys === undefined || addressKeys.includes(`${entry.request.chain}:${entry.request.address}`)),
  )
  const mutation = useMutation({
    retry: false,
    mutationFn: async (operation: Operation) => {
      if (operation.kind === 'export') return onchain.exportHistory(operation.id, workspaceId)
      if (operation.kind === 'open') return onchain.history(operation.id, workspaceId)
      return onchain.collectHistory(operation.request, workspaceId)
    },
    onSuccess: (data, operation) => {
      if (operation.generation !== generation.current) return
      if (operation.kind === 'export') {
        // Preserve server bytes: parsing raw u64 RPC values through JS loses precision.
        const url = URL.createObjectURL(data as Blob)
        const anchor = document.createElement('a')
        anchor.href = url
        anchor.download = 'solana-historical-evidence.json'
        document.body.appendChild(anchor)
        anchor.click()
        anchor.remove()
        URL.revokeObjectURL(url)
        return
      }
      const collection = data as HistoryCollection
      setResult(collection)
      setSavedId(collection.collection_id)
      setAssetFilter('')
      setStatusFilter('')
      setPage(0)
      if (operation.kind === 'collect') void client.invalidateQueries({ queryKey: ['onchain', 'history', workspaceId] })
    },
  })
  const invalidate = () => {
    generation.current += 1
    mutation.reset()
    setResult(null)
    setSavedId('')
  }
  const suppliedAddresses = [...new Set(supplied.split(/[\s,]+/).filter(Boolean))]
  const invalidWindow = Boolean(since && until && since > until)
  const canCollect = Boolean(canWrite && ownershipConfirmed && selected?.chain === 'solana' && !watchedQuery.isError && !invalidWindow && (!suppliedAddresses.length || reviewed))
  const collect = (event: FormEvent) => {
    event.preventDefault()
    if (!canCollect || !selected || mutation.isPending) return
    mutation.mutate({ kind: 'collect', generation: ++generation.current, request: {
      connection_id: selected.connection_id, chain: selected.chain, address: selected.address, ownership_confirmed: true,
      ...(since ? { since: new Date(`${since}Z`).toISOString() } : {}),
      ...(until ? { until: new Date(`${until}Z`).toISOString() } : {}),
      ...(suppliedAddresses.length ? { supplied_accounts: suppliedAddresses.map((address) => ({ address, owner: selected.address, reviewed: true as const })) } : {}),
    } })
  }
  const continueCollection = (reobserve = false) => {
    if (!result || !canResume || mutation.isPending) return
    mutation.mutate({ kind: 'collect', generation: ++generation.current, request: {
      ...result.request, collection_id: result.collection_id, expected_revision: result.revision, reobserve,
    } })
  }
  const evidence = result?.evidence
  const canResume = Boolean(canWrite && result && watched.some((entry) =>
    entry.connection_id === result.request.connection_id && entry.chain === result.request.chain && entry.address === result.request.address,
  ))
  const transactions = Object.values(evidence?.transactions ?? {})
  const assets = [...new Set(transactions.flatMap((transaction) => transaction.versions.flatMap((version) => version.legs.map((leg) => assetIdentity(leg.asset)))))].sort()
  const filtered = transactions.filter((transaction) => {
    const version = transaction.versions.find((entry) => entry.version_id === transaction.canonical_version) ?? transaction.versions.at(-1)
    return (!assetFilter || version?.legs.some((leg) => assetIdentity(leg.asset) === assetFilter)) &&
      (!statusFilter || (transaction.canonical_version ? version?.settlement : 'unresolved') === statusFilter)
  })
  const errorCode = (mutation.error as { response?: { data?: { detail?: { code?: string } } } } | null)?.response?.data?.detail?.code
  const errorText = errorCode === 'history_busy'
    ? t('history.busy', 'Another operation is using this workspace. Wait for it to finish, then retry. Saved evidence remains available.')
    : errorCode === 'history_revision_conflict'
    ? t('history.conflict', 'Another collection updated this archive. Reopen the saved collection before continuing.')
    : errorCode === 'history_restart_required'
      ? t('history.restartRequired', 'The source, decoder or request changed. Start a new collection; the saved evidence remains available.')
      : errorCode === 'history_storage_limit'
        ? t('history.storageLimit', 'Evidence storage is full. Export the saved archive and use a smaller collection window.')
        : t('history.failed', 'This request could not finish. Retained evidence is still available; retry or reopen a saved collection.')

  return <div className="space-y-6">
    <Card><CardContent className="space-y-4 p-4 sm:p-5">
      <div className="space-y-1"><h2 className="font-semibold">{t('history.collectTitle', 'Collect owned wallet evidence')}</h2><p className="max-w-3xl text-sm text-muted-foreground">{t('history.intro', 'Collect Solana, SPL Token and Token-2022 history from your connected wallet and supported token accounts. Collection retains private evidence without changing holdings, trades or basis.')}</p></div>
      {watchedQuery.isPending && <Skeleton className="h-10 w-full" />}
      {watchedQuery.isError && <Alert variant="warning"><span>{t('history.walletError', 'Connected wallets could not be loaded.')}</span><Button variant="outline" onClick={() => void watchedQuery.refetch()}>{t('common.retry')}</Button></Alert>}
      {watchedQuery.isSuccess && !watched.length && <p className="text-sm text-muted-foreground">{t('history.noWallets', 'No connected wallets match this workspace and wallet filter. Connect a watch-only wallet in Accounts to collect evidence.')}</p>}
      <form onSubmit={collect} className="space-y-4">
        <div className="grid items-start gap-4 sm:grid-cols-3">
          <div className="min-w-0 space-y-1.5"><Label htmlFor="history-wallet">{t('history.wallet', 'Evidence wallet')}</Label><NativeSelect id="history-wallet" className={selectClass} value={selected ? `${selected.connection_id}:${selected.chain}:${selected.address}` : ''} onChange={(event) => { setSelectedKey(event.target.value); setSupplied(''); setReviewed(false); setOwnershipConfirmed(false); invalidate() }}><option value="">{t('history.chooseWallet', 'Select a connected wallet')}</option>{watched.map((entry) => <option key={`${entry.connection_id}:${entry.chain}:${entry.address}`} value={`${entry.connection_id}:${entry.chain}:${entry.address}`}>{mask(`${entry.connection_name} · ${entry.label}`)}</option>)}</NativeSelect>{selected && <p className="break-all font-mono text-xs text-muted-foreground">{mask(selected.address)}</p>}</div>
          <div className="min-w-0 space-y-1.5"><Label htmlFor="history-since">{t('history.since', 'Evidence from (UTC)')}</Label><Input id="history-since" type="datetime-local" step="any" value={since} max={until || undefined} onChange={(event) => { setSince(event.target.value); invalidate() }} /></div>
          <div className="min-w-0 space-y-1.5"><Label htmlFor="history-until">{t('history.until', 'Evidence through (UTC)')}</Label><Input id="history-until" type="datetime-local" step="any" value={until} min={since || undefined} onChange={(event) => { setUntil(event.target.value); invalidate() }} /></div>
        </div>
        <p className="text-xs text-muted-foreground">{t('history.bounds', 'Both UTC endpoints are inclusive. Unknown timestamps remain a coverage gap. Finalized evidence alone contributes to settled quantities.')}</p>
        <label className="flex items-start gap-2 text-sm"><input type="checkbox" className="mt-1" checked={ownershipConfirmed} disabled={!canWrite || !selected} onChange={(event) => setOwnershipConfirmed(event.target.checked)} /><span>{t('history.owned', 'I own the selected wallet. This assertion applies to this address only; it does not establish historical token-account ownership.')}</span></label>
        {selected && selected.chain !== 'solana' && <Alert variant="warning">{t('history.unsupported', 'Historical evidence collection is currently supported for Solana only. EVM and Bitcoin history collection is unsupported; existing native investigations remain available.')}</Alert>}
        {invalidWindow && <Alert variant="warning">{t('history.invalidWindow', 'The start must be on or before the end.')}</Alert>}
        <details className="border-t border-border pt-3"><summary className="cursor-pointer text-sm font-medium">{t('history.suppliedTitle', 'Reviewed historical token accounts')}</summary><div className="mt-3 space-y-3"><Label htmlFor="history-supplied">{t('history.supplied', 'Historical token-account addresses (separated by spaces or commas)')}</Label><Input id="history-supplied" type={privacyMode ? 'password' : 'text'} value={supplied} maxLength={10000} onChange={(event) => { setSupplied(event.target.value); setReviewed(false); invalidate() }} /><label className="flex items-start gap-2 text-sm"><input type="checkbox" className="mt-1" checked={reviewed} onChange={(event) => { setReviewed(event.target.checked); invalidate() }} /><span>{t('history.reviewed', 'I reviewed these historical token-account addresses for the selected wallet. Missing or conflicting chain ownership evidence must remain unresolved.')}</span></label></div></details>
        <div className="flex flex-wrap items-center gap-3"><Button type="submit" disabled={!canCollect || mutation.isPending}>{mutation.isPending && mutation.variables?.kind === 'collect' ? t('history.collecting', 'Collecting evidence…') : result ? t('history.new', 'Start new collection') : t('history.collect', 'Collect evidence')}</Button>{!canWrite && <span className="text-sm text-muted-foreground">{t('history.viewer', 'Viewers can open and export saved evidence. An editor can collect new evidence.')}</span>}</div>
      </form>
      <div className="space-y-2 border-t border-border pt-4"><Label htmlFor="history-saved">{t('history.saved', 'Saved collections')}</Label><div className="flex flex-col items-stretch gap-2 sm:flex-row sm:items-center"><NativeSelect id="history-saved" className={selectClass} wrapperClassName="w-full sm:max-w-lg sm:flex-1" value={savedId} onChange={(event) => setSavedId(event.target.value)}><option value="">{t('history.chooseSaved', 'Select saved evidence')}</option>{savedCollections.map((entry) => <option key={entry.collection_id} value={entry.collection_id}>{moment(entry.updated_at)} · {entry.transaction_count} {t('history.transactions', 'transactions')}</option>)}</NativeSelect><Button variant="outline" disabled={!savedId || mutation.isPending} onClick={() => mutation.mutate({ kind: 'open', id: savedId, generation: ++generation.current })}>{t('history.open', 'Open saved evidence')}</Button></div>{savedQuery.isSuccess && !savedCollections.length && <p className="text-xs text-muted-foreground">{t('history.noSaved', 'No saved evidence matches this workspace and wallet filter yet.')}</p>}{savedQuery.isError && <Alert variant="warning"><span>{t('history.savedError', 'Saved collections could not be loaded.')}</span><Button variant="outline" onClick={() => void savedQuery.refetch()}>{t('common.retry')}</Button></Alert>}<p className="max-w-3xl text-xs text-muted-foreground">{t('history.retention', 'Saved evidence remains available independently of collection progress. Opening an archive does not contact a chain provider. Archives are private and limited to 32 MiB each and 128 MiB per workspace.')}</p></div>
    </CardContent></Card>
    {mutation.isPending && <div role="status" className="space-y-2"><p className="text-sm">{t('history.loading', 'Working on this request. Previously retained evidence stays visible.')}</p>{!result && <Skeleton className="h-24 w-full" />}</div>}
    {mutation.isError && <Alert variant="warning" className="block space-y-2"><p>{errorText}</p><Button variant="outline" disabled={mutation.isPending} onClick={() => { if (mutation.variables) mutation.mutate({ ...mutation.variables, generation: ++generation.current }) }}>{t('common.retry')}</Button></Alert>}
    {evidence && result && <section aria-label={t('history.results', 'Historical evidence results')} className="space-y-5">
      <div className="flex flex-wrap items-start justify-between gap-3 border-b border-border pb-4"><div className="space-y-1"><h3 className="font-semibold">{t('history.coverage', 'Evidence coverage')}</h3><p className="break-words text-sm">{evidence.requested.since ? moment(evidence.requested.since) : t('history.openStart', 'No lower bound')} → {evidence.requested.until ? moment(evidence.requested.until) : t('history.openEnd', 'No upper bound')}</p><p className="text-xs text-muted-foreground">{t('history.scope', 'Coverage applies only to the declared accounts and interval. Exhausted owner history does not establish all historical token accounts or tax basis.')}</p></div><div className="flex flex-wrap gap-2">{evidence.resumable && <Button disabled={!canResume || mutation.isPending} onClick={() => continueCollection()}>{t('history.continue', 'Continue collection')}</Button>}<Button variant="outline" disabled={!canResume || mutation.isPending} onClick={() => continueCollection(true)}>{t('history.reobserve', 'Re-observe settlement')}</Button><Button variant="outline" disabled={mutation.isPending} onClick={() => mutation.mutate({ kind: 'export', id: result.collection_id, generation: ++generation.current })}><Download size={14} />{t('history.export', 'Export private evidence')}</Button></div></div>
      <dl className="grid grid-cols-2 gap-4 lg:grid-cols-4">{(['inventory', 'retrieval', 'interpretation', 'settlement'] as const).map((axis) => <div key={axis}><dt className="text-sm text-muted-foreground">{t(`history.axis.${axis}`, { defaultValue: `${axis[0].toUpperCase()}${axis.slice(1)}` })}</dt><dd className="font-medium">{label(evidence.coverage[axis])}</dd></div>)}</dl>
      <details className="border-y border-border py-3"><summary className="cursor-pointer text-sm font-medium">{t('history.limits', 'Collection limits and settlement anchor')}</summary><dl className="mt-3 grid gap-3 text-sm sm:grid-cols-2"><div><dt className="text-muted-foreground">{t('history.anchor', 'Finalized anchor slot')}</dt><dd>{evidence.anchor?.slot ?? unknown}</dd></div>{(['seconds', 'attempts', 'attempts_used', 'concurrency', 'pages_per_address', 'accounts', 'archive_bytes', 'payload_bytes'] as const).map((key) => <div key={key}><dt className="text-muted-foreground">{label(key)}</dt><dd>{typeof evidence.limits[key] === 'number' ? String(evidence.limits[key]) : unknown}</dd></div>)}</dl></details>
      {!!evidence.gaps.length && <Alert variant="warning"><ul className="list-disc space-y-1 pl-4">{evidence.gaps.map((gap, index) => <li key={index}>{label(gap)}</li>)}</ul></Alert>}
      <details className="border-y border-border py-3"><summary className="cursor-pointer text-sm font-medium">{t('history.accounts', 'Account inventory and retrieval')} · {Object.keys(evidence.inventory).length}</summary><div className="mt-3 divide-y divide-border">{Object.entries(evidence.inventory).map(([address, account]) => {
        const stream = evidence.streams[address]
        return <div key={address} className="space-y-2 py-3 text-sm"><p className="break-all font-mono">{mask(address)}</p><p>{label(account.kind)} · {account.discoveries.length} {t('history.discoveryRefs', 'discovery references')} · {account.ownership.length} {t('history.ownershipRefs', 'ownership observations')}</p>{stream ? <><p>{t('history.pages', 'Pages examined')}: {stream.pages_examined} · {t('history.exhausted', 'Provider exhausted')}: {stream.exhausted ? t('common.yes', 'Yes') : t('common.no', 'No')}</p><p>{t('history.observed', 'Observed bounds')}: {moment(stream.oldest_at)} → {moment(stream.newest_at)}</p><p>{t('history.unknownTimes', 'Unknown timestamps')}: {stream.unknown_timestamps} · {t('history.payloadGaps', 'Payload gaps')}: {stream.payload_gaps.length}</p>{stream.stop_reason && <p>{label(stream.stop_reason)}</p>}{stream.cursor && <p className="break-all">{t('history.cursor', 'Resume cursor')}: {mask(stream.cursor)}</p>}</> : <p>{t('history.unsearched', 'This account has not been searched.')}</p>}</div>
      })}</div></details>
      <div className="space-y-3"><h3 className="font-semibold">{t('history.reconciliation', 'Quantity reconciliation')}</h3><p className="text-sm text-muted-foreground">{t('history.equation', 'Opening + signed settled changes = closing. Missing or incompatible snapshots remain unknown; quantity equality does not certify history or basis.')}</p>{evidence.reconciliation?.length ? evidence.reconciliation.map((row, index) => <div key={index} className="space-y-1 border-b border-border py-3 text-sm"><p className="break-all">{mask(row.account)} · {mask(assetIdentity(row.asset))}</p><p className="break-words tabular-nums">{mask(row.opening ?? unknown)} + ({mask(row.settled_change ?? unknown)}) = {mask(row.closing ?? unknown)}</p><p>{t('history.discrepancy', 'Discrepancy')}: {mask(row.discrepancy ?? unknown)} · {label(row.status)}</p>{row.scope && <p>{t('history.quantityScope', 'Quantity scope')}: {label(row.scope)} · {t('history.requestedInterval', 'Requested interval')}: {label(row.requested_interval_status ?? 'unknown')}</p>}{row.opening_snapshot && row.closing_snapshot && <p>{t('history.snapshotBounds', 'Snapshot bounds')}: {moment(row.opening_snapshot.time)} ({label(row.opening_snapshot.position ?? 'unknown')}) → {moment(row.closing_snapshot.time)} ({label(row.closing_snapshot.position ?? 'unknown')})</p>}{row.reasons.length > 0 && <p>{row.reasons.map(label).join(' · ')}</p>}</div>) : <p className="text-sm">{t('history.noSnapshots', 'Compatible opening and closing snapshots are unavailable. Reconciliation remains unknown.')}</p>}</div>
      <div className="space-y-3"><h3 className="font-semibold">{t('history.transactions', 'Transactions')} · {transactions.length}</h3><div className="grid gap-3 sm:grid-cols-2"><div className="space-y-1.5"><Label htmlFor="history-asset">{t('history.assetFilter', 'Evidence asset')}</Label><NativeSelect id="history-asset" className={selectClass} value={assetFilter} onChange={(event) => { setAssetFilter(event.target.value); setPage(0) }}><option value="">{t('history.allAssets', 'All assets')}</option>{assets.map((asset) => <option key={asset} value={asset}>{mask(asset)}</option>)}</NativeSelect></div><div className="space-y-1.5"><Label htmlFor="history-status">{t('history.statusFilter', 'Settlement status')}</Label><NativeSelect id="history-status" className={selectClass} value={statusFilter} onChange={(event) => { setStatusFilter(event.target.value); setPage(0) }}><option value="">{t('history.allStatuses', 'All statuses')}</option>{['settled', 'provisional', 'unresolved'].map((status) => <option key={status} value={status}>{label(status)}</option>)}</NativeSelect></div></div>
        {!filtered.length && <p className="text-sm text-muted-foreground">{transactions.length ? t('history.noMatch', 'No transactions match these filters. Change the asset or settlement filter to see retained evidence.') : evidence.coverage.retrieval === 'complete' ? t('history.emptyComplete', 'No transactions were returned for the declared address streams and interval. Historical token-account inventory may still be unknown.') : t('history.emptyPartial', 'No transactions have been retained yet. Retrieval is incomplete; continue collection or review the coverage gaps.')}</p>}
        {filtered.slice(page * 20, (page + 1) * 20).map((transaction) => {
          const version = transaction.versions.find((entry) => entry.version_id === transaction.canonical_version) ?? transaction.versions.at(-1)
          return <details key={transaction.signature} className="border-b border-border py-3"><summary className="cursor-pointer space-y-1 text-sm"><span className="block break-all font-mono">{mask(transaction.signature)}</span><span className="block text-muted-foreground">{moment(version?.block_time)} · {label(transaction.canonical_version ? version?.settlement ?? 'unknown' : 'unresolved')} · {version?.legs.length ?? 0} {t('history.legs', 'legs')}</span></summary><div className="mt-3 space-y-3 text-sm"><p>{t('history.execution', 'Execution')}: {label(version?.execution ?? 'unknown')} · {t('history.versions', 'Retained versions')}: {transaction.versions.length} · {t('history.discoveryRefs', 'discovery references')}: {transaction.discovery_refs.length}</p>{!transaction.canonical_version && <p>{t('history.noCanonical', 'No canonical settled interpretation is established. Retained observations do not become settled movements.')}</p>}{transaction.retrieval_gap && <p>{t('history.retrievalGap', 'Transaction retrieval gap')}: {label(transaction.retrieval_gap)}</p>}{version?.gaps.map((gap, index) => <p key={index}>{label(gap)}</p>)}{!version ? <p>{t('history.noPayload', 'The transaction payload is unavailable. Its discovery reference is retained and retrieval remains incomplete.')}</p> : !version.legs.length && <p>{t('history.noLegs', 'The source payload is retained without decoded movements. No zero-valued movement is invented.')}</p>}{version?.legs.map((leg) => <div key={leg.key} className="space-y-1 border-t border-border pt-2"><p className="break-all">{mask(assetIdentity(leg.asset))} · {label(leg.role)} · {mask(leg.quantity ?? unknown)}</p>{leg.interpretation === 'unresolved' && <p>{t('history.unresolvedLeg', 'Observed amount; the transfer mechanics remain unresolved and this leg is excluded from supported quantity totals.')}</p>}{leg.non_additive && <p>{t('history.backingLeg', 'Backing observation, excluded from quantity totals to avoid counting the same value twice.')}</p>}<p className="break-all">{mask(leg.source ?? unknown)} → {mask(leg.destination ?? unknown)}</p><p>{t('history.atomic', 'Atomic units')}: {mask(leg.raw_units ?? unknown)} · {label(transaction.canonical_version ? leg.settlement : 'unresolved')}</p></div>)}<p className="break-all text-xs text-muted-foreground">{t('history.payload', 'Source payload digest')}: {mask(version?.payload_digest ?? unknown)}</p><p className="text-xs text-muted-foreground">{t('history.allLegs', 'All transaction legs remain visible when filtering by one asset. Original payloads, ownership observations and derivation references are included in the private export.')}</p></div></details>
        })}
        {filtered.length > 20 && <div className="flex flex-wrap items-center gap-3"><Button variant="outline" disabled={!page} onClick={() => setPage(page - 1)}>{t('common.previous', 'Previous')}</Button><span className="text-sm">{page + 1} / {Math.ceil(filtered.length / 20)}</span><Button variant="outline" disabled={(page + 1) * 20 >= filtered.length} onClick={() => setPage(page + 1)}>{t('common.next', 'Next')}</Button></div>}
      </div>
    </section>}
  </div>
}
