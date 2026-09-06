import { useMemo, useState, type FormEvent } from 'react'
import { Link, Navigate, useLocation, useSearchParams } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import { useQuery, useMutation } from '@tanstack/react-query'
import { isValid, parseISO } from 'date-fns'
import { toast } from 'sonner'
import { ArrowRight, Building2, Check, Copy, Download, Flag, Link2, Radar } from 'lucide-react'
import { onchain } from '@/lib/api'
import { extractApiError } from '@/lib/api-errors'
import { cn } from '@/lib/utils'
import { usePrivacyMode } from '@/hooks/use-privacy-mode'
import { useWorkspace } from '@/contexts/workspace-context'
import { PageHeader } from '@/components/page-header'
import { Alert } from '@/components/ui/alert'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import { Skeleton } from '@/components/ui/skeleton'
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip'
import type {
  TraceDirection,
  TraceEdge,
  TraceNode,
  TraceResult,
  TraceRequest,
  TraceTerminalReason,
} from '@/types'

function shorten(address: string): string {
  return address.length > 12 ? `${address.slice(0, 4)}…${address.slice(-4)}` : address
}

/** One hop of the trail: the transfers that land at this depth, plus the
 *  nodes at this depth where the walk stopped. */
interface Hop {
  depth: number
  edges: TraceEdge[]
  terminals: TraceNode[]
}

/**
 * Order the raw node/edge sets into hops.
 *
 * An edge belongs to the deeper of its two endpoints, which holds for both
 * directions: walking outwards the recipient is deeper, walking backwards the
 * sender is. Edges whose endpoints are unknown are dropped rather than shown
 * at an invented depth.
 */
function buildHops(result: TraceResult): Hop[] {
  const depthOf = new Map(result.nodes.map((node) => [node.id, node.depth]))
  const hops = new Map<number, Hop>()
  const at = (depth: number): Hop => {
    const existing = hops.get(depth)
    if (existing) return existing
    const created: Hop = { depth, edges: [], terminals: [] }
    hops.set(depth, created)
    return created
  }

  for (const edge of result.edges) {
    const source = depthOf.get(edge.source)
    const target = depthOf.get(edge.target)
    if (source === undefined || target === undefined) continue
    at(Math.max(source, target)).edges.push(edge)
  }
  for (const node of result.nodes) {
    if (node.terminal_reason) at(node.depth).terminals.push(node)
  }

  for (const hop of hops.values()) {
    hop.edges.sort((a, b) => a.occurred_at.localeCompare(b.occurred_at))
  }
  return [...hops.values()].sort((a, b) => a.depth - b.depth)
}

function CopyButton({ value, label, showLabel = false }: { value: string; label: string; showLabel?: boolean }) {
  const { t } = useTranslation()
  const [copied, setCopied] = useState(false)
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(value)
      setCopied(true)
      setTimeout(() => setCopied(false), 1500)
    } catch {
      toast.error(t('trace.copyFailed'))
    }
  }
  return (
    <Button type="button" variant={showLabel ? "outline" : "ghost"} size={showLabel ? "sm" : "icon-sm"} className={showLabel ? undefined : "text-muted-foreground"} onClick={copy} aria-label={label}>
      {copied ? <Check size={14} /> : showLabel ? <Link2 size={14} /> : <Copy size={14} />}
      {showLabel && label}
    </Button>
  )
}

function AddressRef({ node, id }: { node: TraceNode | undefined; id: string }) {
  const { t } = useTranslation()
  const { mask } = usePrivacyMode()
  const address = node?.address ?? id.split(':').slice(1).join(':')
  return (
    <span className="inline-flex min-w-0 flex-wrap items-center gap-x-1 gap-y-0.5">
      <Tooltip>
        <TooltipTrigger asChild>
          <span className="font-mono text-xs text-foreground">{shorten(address)}</span>
        </TooltipTrigger>
        <TooltipContent className="font-mono text-xs">{address}</TooltipContent>
      </Tooltip>
      <CopyButton value={address} label={t('trace.copyAddress')} />
      {node?.balance != null && (
        <span className="text-xs text-muted-foreground">
          {mask(t('trace.balance', { amount: node.balance, symbol: node.symbol }))}
        </span>
      )}
    </span>
  )
}

function TerminalMarker({ node }: { node: TraceNode }) {
  const { t } = useTranslation()
  const reason = node.terminal_reason as TraceTerminalReason
  // A busy address ends the bounded walk; activity alone does not identify
  // its operator or prove where this user's funds ended up.
  const prominent = reason === 'pooled'

  return (
    <div
      className={cn(
        'flex items-start gap-2.5 rounded-lg border px-3.5 py-3 text-sm',
        prominent
          ? 'border-warning/40 bg-warning/10 text-warning-foreground'
          : 'border-border bg-muted/40 text-muted-foreground',
      )}
    >
      {prominent ? (
        <Building2 size={16} className="mt-0.5 shrink-0" />
      ) : (
        <Flag size={16} className="mt-0.5 shrink-0" />
      )}
      <div className="space-y-1">
        <p className={cn('font-medium', prominent && 'text-warning-foreground')}>
          {t(`trace.terminal.${reason}`)}
        </p>
        <AddressRef node={node} id={node.id} />
        {prominent && <p className="text-xs">{t('trace.terminalDetail.pooled')}</p>}
      </div>
    </div>
  )
}

const HOP_OPTIONS = [1, 2, 3, 4, 5, 6]
const BRANCH_OPTIONS = [1, 2, 3, 4, 5]

export default function TracePage() {
  const { t } = useTranslation()
  const { hasModule, isLoading } = useWorkspace()
  const [params] = useSearchParams()
  if (isLoading) return <Skeleton className="h-32 w-full" />
  if (hasModule('assets')) {
    const destination = new URLSearchParams(params)
    destination.set('tab', 'activity')
    destination.set('activity', 'wallets')
    return <Navigate to={`/assets?${destination}`} replace />
  }
  return <div><PageHeader section={t('accounts.title')} title={t('trace.title')} /><OwnedWalletActivity /></div>
}

interface OwnedWalletActivityProps {
  connectionIds?: string[]
  addressKeys?: string[]
}

/** Keep URL timestamps in UTC when putting them into a local datetime control. */
function traceDateInput(value: string | null, endOfDay = false): string {
  if (!value || !/^\d{4}-\d{2}-\d{2}(?:T|$)/.test(value)) return ''
  const timestamp = /^\d{4}-\d{2}-\d{2}$/.test(value)
    ? `${value}T${endOfDay ? '23:59:59.999' : '00:00:00'}Z`
    : /(?:Z|[+-]\d{2}:?\d{2})$/i.test(value) ? value : `${value}Z`
  const parsed = parseISO(timestamp)
  return isValid(parsed) ? parsed.toISOString().slice(0, -1) : ''
}

export function OwnedWalletActivity(props: OwnedWalletActivityProps) {
  const { current } = useWorkspace()
  const [params] = useSearchParams()
  if (!current) return null
  // Every trace setting participates in the reset, including browser history
  // navigation. A URL only selects a saved address; it never starts a request.
  const initial = {
    selectedKey: params.has('chain') && params.has('address') ? `${params.get('chain')}:${params.get('address')}` : '',
    direction: params.get('direction') === 'in' ? 'in' as const : 'out' as const,
    maxHops: HOP_OPTIONS.includes(Number(params.get('max_hops'))) ? String(Number(params.get('max_hops'))) : '3',
    maxBranches: BRANCH_OPTIONS.includes(Number(params.get('max_branches'))) ? String(Number(params.get('max_branches'))) : '3',
    minAmount: params.get('min_amount') ?? '',
    since: traceDateInput(params.get('since')),
    until: traceDateInput(params.get('until'), true),
  }
  const key = JSON.stringify([current.id, initial, props.connectionIds, props.addressKeys])
  return <WalletActivityForm key={key} {...props} workspaceId={current.id} initial={initial} />
}

function WalletActivityForm({
  workspaceId, initial, connectionIds, addressKeys,
}: OwnedWalletActivityProps & {
  workspaceId: string
  initial: { selectedKey: string; direction: TraceDirection; maxHops: string; maxBranches: string; minAmount: string; since: string; until: string }
}) {
  const { t, i18n } = useTranslation()
  const location = useLocation()
  const [selectedKey, setSelectedKey] = useState(initial.selectedKey)
  const [direction, setDirection] = useState(initial.direction)
  const [maxHops, setMaxHops] = useState(initial.maxHops)
  const [maxBranches, setMaxBranches] = useState(initial.maxBranches)
  const [minAmount, setMinAmount] = useState(initial.minAmount)
  const [since, setSince] = useState(initial.since)
  const [until, setUntil] = useState(initial.until)
  const { mask } = usePrivacyMode()
  const formatUtc = (value: string) => `${new Date(value).toLocaleString(i18n.resolvedLanguage ?? i18n.language, { dateStyle: 'medium', timeStyle: 'medium', timeZone: 'UTC' })} UTC`

  const chainsQuery = useQuery({
    queryKey: ['onchain', 'chains', workspaceId],
    queryFn: ({ signal }) => onchain.chains(workspaceId, signal),
    staleTime: Infinity,
  })
  const watchedQuery = useQuery({
    queryKey: ['onchain', 'addresses', workspaceId],
    queryFn: ({ signal }) => onchain.addresses(workspaceId, signal),
    staleTime: 0,
  })

  const traceMutation = useMutation({
    mutationFn: async (request: TraceRequest) => ({
      result: await onchain.trace(request, workspaceId), request, retrieved_at: new Date().toISOString(),
    }),
  })

  const chains = chainsQuery.data ?? []
  const watched = (watchedQuery.data ?? []).filter((entry) =>
    (connectionIds === undefined || connectionIds.includes(entry.connection_id)) &&
    (addressKeys === undefined || addressKeys.includes(`${entry.chain}:${entry.address}`)),
  )
  const selected = watched.find((entry) => `${entry.chain}:${entry.address}` === selectedKey)
  const selectedChain = chains.find((entry) => entry.key === selected?.chain)
  const invalidWindow = Boolean(since && until && Date.parse(`${since}Z`) > Date.parse(`${until}Z`))
  const invalidAmount = Boolean(minAmount.trim() && (!/^(?:\d+(?:\.\d*)?|\.\d+)(?:e[+-]?\d+)?$/i.test(minAmount.trim()) || !Number.isFinite(Number(minAmount))))
  const canSubmit = Boolean(selected && selectedChain?.traceable && !invalidWindow && !invalidAmount && !watchedQuery.isError && !chainsQuery.isError)

  const result = selected && !watchedQuery.isError ? traceMutation.data?.result : undefined
  const hops = useMemo(() => (result ? buildHops(result) : []), [result])
  const nodeById = useMemo(
    () => new Map((result?.nodes ?? []).map((node) => [node.id, node])),
    [result],
  )

  const submit = (event: FormEvent) => {
    event.preventDefault()
    if (!canSubmit || !selected) return
    traceMutation.mutate({
      chain: selected.chain,
      address: selected.address,
      direction,
      max_hops: Number(maxHops),
      max_branches: Number(maxBranches),
      ...(minAmount.trim() ? { min_amount: minAmount.trim() } : {}),
      ...(since ? { since: new Date(`${since}Z`).toISOString() } : {}),
      ...(until ? { until: new Date(`${until}Z`).toISOString() } : {}),
    })
  }

  const traceLink = new URL(location.pathname, window.location.origin)
  traceLink.search = location.search
  for (const name of ['chain', 'address', 'direction', 'max_hops', 'max_branches', 'min_amount', 'since', 'until']) traceLink.searchParams.delete(name)
  for (const [name, value] of Object.entries(traceMutation.data?.request ?? {})) traceLink.searchParams.set(name, String(value))

  const download = () => {
    if (!traceMutation.data) return
    try {
      const snapshot = {
        source: 'Lovenest native-coin trace',
        coverage: 'Bounded native-coin transfers only. Tokens, fees, swaps, bridges, exchange activity, claims and cost basis are not reconstructed. Downstream movements after funds mix cannot be attributed solely to this wallet. Keep terminal reasons and truncation with this result.',
        ...traceMutation.data,
      }
      const url = URL.createObjectURL(new Blob([JSON.stringify(snapshot, null, 2)], { type: 'application/json' }))
      const anchor = document.createElement('a')
      anchor.href = url
      anchor.download = `wallet-trace-${traceMutation.data.request.chain}-${traceMutation.data.retrieved_at.slice(0, 10)}.json`
      document.body.appendChild(anchor)
      anchor.click()
      anchor.remove()
      URL.revokeObjectURL(url)
    } catch {
      toast.error(t('trace.downloadFailed'))
    }
  }

  return (
    <div className="space-y-6">
      <Card>
        <CardContent className="p-4 sm:p-5">
          <form onSubmit={submit} onChange={() => traceMutation.reset()} className="space-y-4">
            <div className="flex flex-wrap items-start justify-between gap-3">
              <div className="min-w-0 space-y-1"><h2 className="text-base font-semibold">{t('trace.exploreTitle')}</h2><p className="max-w-2xl text-sm text-muted-foreground">{t('trace.intro')}</p></div>
              <Button asChild variant="outline" size="sm"><Link to="/accounts">{t('trace.manageWallets')}</Link></Button>
            </div>
            <p className="text-xs leading-relaxed text-muted-foreground">{t('trace.nativeOnly')}</p>

            {chainsQuery.isError && <Alert variant="warning">{t('trace.chainsUnavailable')}</Alert>}
            {watchedQuery.isError && (
              <Alert variant="warning">
                {t('trace.addressesUnavailable')}
                <Button type="button" variant="outline" size="sm" onClick={() => void watchedQuery.refetch()}>{t('common.retry')}</Button>
              </Alert>
            )}
            {(watchedQuery.isPending || chainsQuery.isPending) && <Skeleton className="h-10 w-full" />}
            {watchedQuery.isSuccess && watched.length === 0 && <p className="text-sm text-muted-foreground">{t('trace.noWallets')}</p>}

            {watched.length > 0 && (
              <>
                <div className="grid items-start gap-4 sm:grid-cols-2 xl:grid-cols-4">
                  <div className="min-w-0 space-y-1.5">
                    <Label htmlFor="trace-wallet">{t('trace.wallet')}</Label>
                    <Select value={selected ? selectedKey : ''} onValueChange={(value) => { setSelectedKey(value); traceMutation.reset() }}>
                      <SelectTrigger id="trace-wallet" className="w-full"><SelectValue placeholder={t('trace.walletPlaceholder')} /></SelectTrigger>
                      <SelectContent>
                        {watched.map((entry) => (
                          <SelectItem key={`${entry.connection_id}:${entry.chain}:${entry.address}`} value={`${entry.chain}:${entry.address}`}>
                            {entry.connection_name} · {entry.label}
                          </SelectItem>
                        ))}
                      </SelectContent>
                    </Select>
                    {selectedKey && !selected && <p className="text-xs text-muted-foreground">{t('trace.unknownWallet')}</p>}
                    {selectedChain && !selectedChain.traceable && <p className="text-xs text-muted-foreground">{t('trace.chainNotTraceable')}</p>}
                    {selected && <p className="break-all font-mono text-xs text-muted-foreground">{selected.address}</p>}
                  </div>
                  <div className="min-w-0 space-y-1.5">
                    <Label htmlFor="trace-direction">{t('trace.direction')}</Label>
                    <Select value={direction} onValueChange={(value) => { setDirection(value as TraceDirection); traceMutation.reset() }}>
                      <SelectTrigger id="trace-direction" className="w-full"><SelectValue /></SelectTrigger>
                      <SelectContent>
                        <SelectItem value="out">{t('trace.directionOut')}</SelectItem>
                        <SelectItem value="in">{t('trace.directionIn')}</SelectItem>
                      </SelectContent>
                    </Select>
                  </div>
                  <div className="min-w-0 space-y-1.5">
                    <Label htmlFor="trace-since">{t('trace.since')}</Label>
                    <Input id="trace-since" type="datetime-local" step="any" className="min-w-0" value={since} max={until || undefined} onChange={(event) => setSince(event.target.value)} />
                  </div>
                  <div className="min-w-0 space-y-1.5">
                    <Label htmlFor="trace-until">{t('trace.until')}</Label>
                    <Input id="trace-until" type="datetime-local" step="any" className="min-w-0" value={until} min={since || undefined} onChange={(event) => setUntil(event.target.value)} />
                  </div>
                </div>
                {invalidWindow && <Alert variant="warning">{t('trace.invalidWindow')}</Alert>}
                {invalidAmount && <Alert variant="warning">{t('trace.invalidAmount')}</Alert>}
                <details className="border-t border-border pt-3" open={maxHops !== '3' || maxBranches !== '3' || Boolean(minAmount)}>
                  <summary className="cursor-pointer text-sm font-medium">{t('trace.advanced')}</summary>
                  <div className="mt-4 grid gap-4 sm:grid-cols-3">
                    <div className="min-w-0 space-y-1.5">
                      <Label htmlFor="trace-min-amount">{t('trace.minAmount')}{selectedChain ? ` (${selectedChain.symbol})` : ''}</Label>
                      <Input id="trace-min-amount" type="number" min="0" step="any" inputMode="decimal" value={minAmount} onChange={(event) => setMinAmount(event.target.value)} placeholder={t('trace.minAmountPlaceholder')} />
                    </div>
                    <div className="min-w-0 space-y-1.5">
                      <Label htmlFor="trace-max-hops">{t('trace.maxHops')}</Label>
                      <Select value={maxHops} onValueChange={(value) => { setMaxHops(value); traceMutation.reset() }}>
                        <SelectTrigger id="trace-max-hops" className="w-full"><SelectValue /></SelectTrigger>
                        <SelectContent>{HOP_OPTIONS.map((value) => <SelectItem key={value} value={String(value)}>{value}</SelectItem>)}</SelectContent>
                      </Select>
                      <p className="text-xs text-muted-foreground">{t('trace.maxHopsHelp')}</p>
                    </div>
                    <div className="min-w-0 space-y-1.5">
                      <Label htmlFor="trace-max-branches">{t('trace.maxBranches')}</Label>
                      <Select value={maxBranches} onValueChange={(value) => { setMaxBranches(value); traceMutation.reset() }}>
                        <SelectTrigger id="trace-max-branches" className="w-full"><SelectValue /></SelectTrigger>
                        <SelectContent>{BRANCH_OPTIONS.map((value) => <SelectItem key={value} value={String(value)}>{value}</SelectItem>)}</SelectContent>
                      </Select>
                      <p className="text-xs text-muted-foreground">{t('trace.maxBranchesHelp')}</p>
                    </div>
                  </div>
                </details>
                <div className="flex flex-wrap items-center gap-3 border-t border-border pt-4">
                  <Button type="submit" disabled={!canSubmit || traceMutation.isPending}>
                    <Radar size={16} />
                    {traceMutation.isPending ? t('trace.tracing') : t('trace.submit')}
                  </Button>
                  <span className="text-xs text-muted-foreground">{t('trace.utcHint')}</span>
                </div>
              </>
            )}
          </form>
        </CardContent>
      </Card>

      <details className="text-sm">
        <summary className="cursor-pointer font-medium">{t('trace.researchTitle')}</summary>
        <div className="mt-3 max-w-3xl space-y-2 text-sm leading-relaxed text-muted-foreground">
          <p>{t('trace.researchIntro')}</p>
          <ol className="list-decimal space-y-1.5 pl-5">
            <li>{t('trace.researchReceipt')}</li>
            <li>{t('trace.researchMatch')}</li>
            <li>{t('trace.researchRecords')}</li>
          </ol>
        </div>
      </details>

      {traceMutation.isPending && (
        <div className="space-y-3">
          <Skeleton className="h-6 w-40" />
          <Skeleton className="h-16 w-full" />
          <Skeleton className="h-16 w-full" />
          <Skeleton className="h-16 w-full" />
        </div>
      )}

      {result && !traceMutation.isPending && (
        <div className="space-y-5">
          <div className="flex flex-wrap items-start justify-between gap-3 border-b border-border pb-4">
            <div className="space-y-2">
              <h2 className="font-semibold">{t('trace.resultsTitle')}</h2>
              <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
                <Badge variant="secondary">{result.direction === 'out' ? t('trace.directionOut') : t('trace.directionIn')}</Badge>
                <span className="text-sm text-muted-foreground">{t('trace.rootLabel')}</span>
                <AddressRef node={nodeById.get(result.root)} id={result.root} />
              </div>
              <p className="text-xs text-muted-foreground">{t('trace.resultCounts', { transfers: result.edges.length, addresses: result.nodes.length })} · {t('trace.retrievedAt', { time: formatUtc(traceMutation.data!.retrieved_at) })}</p>
            </div>
            <div className="flex flex-wrap gap-2">
              <CopyButton value={traceLink.href} label={t('trace.copyLink')} showLabel />
              <Button type="button" variant="outline" size="sm" onClick={download}><Download size={14} />{t('trace.download')}</Button>
            </div>
          </div>

          {result.truncated && <Alert variant="warning">{t('trace.truncated')}</Alert>}

          {hops.length === 0 && (
            <p className="text-sm text-muted-foreground">{t('trace.empty')}</p>
          )}

          {hops.map((hop) => (
            <details key={hop.depth} open className="group border-b border-border pb-4 last:border-0">
              <summary className="cursor-pointer text-sm font-semibold">
                {hop.depth === 0 ? t('trace.origin') : t('trace.hop', { n: hop.depth })}
                <span className="ml-2 font-normal text-muted-foreground">· {t('trace.hopTransfers', { count: hop.edges.length })}</span>
              </summary>
              <div className="mt-3 space-y-3">
                {hop.edges.map((edge) => (
                  <div key={`${edge.reference}:${edge.source}:${edge.target}`} className="space-y-2 rounded-lg border border-border bg-card p-3 sm:p-4">
                    <div className="flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1">
                      <span className="text-sm font-semibold tabular-nums text-foreground">{mask(t('trace.amount', { amount: edge.amount, symbol: edge.symbol }))}</span>
                      <time dateTime={edge.occurred_at} className="text-xs text-muted-foreground">{formatUtc(edge.occurred_at)}</time>
                    </div>
                    <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
                      <AddressRef node={nodeById.get(edge.source)} id={edge.source} />
                      <ArrowRight size={14} className="shrink-0 text-muted-foreground" />
                      <AddressRef node={nodeById.get(edge.target)} id={edge.target} />
                    </div>
                    <div className="flex min-w-0 items-center gap-1.5 border-t border-border pt-2 text-xs text-muted-foreground">
                      <span className="shrink-0">{t('trace.transactionReference')}</span>
                      <span className="min-w-0 truncate font-mono" title={edge.reference}>{edge.reference}</span>
                      <CopyButton value={edge.reference} label={t('trace.copyReference')} />
                    </div>
                  </div>
                ))}
                {hop.terminals.map((node) => <TerminalMarker key={node.id} node={node} />)}
              </div>
            </details>
          ))}
        </div>
      )}

      {traceMutation.isError && (
        <Alert variant="warning">
          {extractApiError(traceMutation.error, t('trace.failed'))}
        </Alert>
      )}

      {!result && !traceMutation.isPending && !traceMutation.isError && (
        <div className="flex items-center gap-2 text-sm text-muted-foreground">
          <Radar size={16} />
          {t('trace.idle')}
        </div>
      )}
    </div>
  )
}
