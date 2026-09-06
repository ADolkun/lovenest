import { useMemo, useState, type FormEvent } from 'react'
import { Link, Navigate, useSearchParams } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import { useQuery, useMutation } from '@tanstack/react-query'
import { format } from 'date-fns'
import { toast } from 'sonner'
import { ArrowRight, Building2, Check, Copy, Flag, Radar } from 'lucide-react'
import { onchain } from '@/lib/api'
import { extractApiError } from '@/lib/api-errors'
import { resolveDateFnsLocale } from '@/lib/date-fns-locale'
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

function CopyButton({ value, label }: { value: string; label: string }) {
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
    <Button type="button" variant="ghost" size="icon" className="size-6 text-muted-foreground" onClick={copy} aria-label={label}>
      {copied ? <Check size={12} /> : <Copy size={12} />}
    </Button>
  )
}

function AddressRef({ node, id }: { node: TraceNode | undefined; id: string }) {
  const { t } = useTranslation()
  const { mask } = usePrivacyMode()
  const address = node?.address ?? id.split(':').slice(1).join(':')
  return (
    <span className="inline-flex items-center gap-1">
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

export function OwnedWalletActivity(props: OwnedWalletActivityProps) {
  const { current } = useWorkspace()
  const [params] = useSearchParams()
  if (!current) return null
  const initialAddress = params.has('chain') && params.has('address')
    ? `${params.get('chain')}:${params.get('address')}`
    : ''
  // React Query resets queries on a workspace switch, but mutation results
  // and local selection need to be discarded as well, including late replies.
  const key = JSON.stringify([current.id, initialAddress, props.connectionIds, props.addressKeys])
  return <WalletActivityForm key={key} {...props} workspaceId={current.id} initialAddress={initialAddress} />
}

function WalletActivityForm({
  workspaceId, initialAddress, connectionIds, addressKeys,
}: OwnedWalletActivityProps & { workspaceId: string; initialAddress: string }) {
  const { t, i18n } = useTranslation()
  const dateFnsLocale = resolveDateFnsLocale(i18n.resolvedLanguage ?? i18n.language)
  const [selectedKey, setSelectedKey] = useState(initialAddress)
  const [direction, setDirection] = useState<TraceDirection>('out')
  const [maxHops, setMaxHops] = useState('3')
  const [maxBranches, setMaxBranches] = useState('3')
  const [minAmount, setMinAmount] = useState('')
  const [since, setSince] = useState('')
  const [until, setUntil] = useState('')
  const { mask } = usePrivacyMode()

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
    mutationFn: (payload: Parameters<typeof onchain.trace>[0]) => onchain.trace(payload, workspaceId),
  })

  const chains = chainsQuery.data ?? []
  const watched = (watchedQuery.data ?? []).filter((entry) =>
    (connectionIds === undefined || connectionIds.includes(entry.connection_id)) &&
    (addressKeys === undefined || addressKeys.includes(`${entry.chain}:${entry.address}`)),
  )
  const selected = watched.find((entry) => `${entry.chain}:${entry.address}` === selectedKey)
  const selectedChain = chains.find((entry) => entry.key === selected?.chain)
  const invalidWindow = Boolean(since && until && since > until)
  const canSubmit = Boolean(selected && selectedChain?.traceable && !invalidWindow && !watchedQuery.isError && !chainsQuery.isError)

  const result = selected && !watchedQuery.isError ? traceMutation.data : undefined
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
      ...(since ? { since: `${since}T00:00:00Z` } : {}),
      ...(until ? { until: `${until}T23:59:59.999Z` } : {}),
    })
  }

  return (
    <div>
      <Card className="mb-6">
        <CardContent>
          <form onSubmit={submit} onChange={() => traceMutation.reset()} className="space-y-4">
            <div className="flex flex-wrap items-start justify-between gap-3">
              <p className="max-w-2xl text-sm text-muted-foreground">{t('trace.intro')}</p>
              <Button asChild variant="outline" size="sm"><Link to="/accounts">{t('trace.manageWallets')}</Link></Button>
            </div>
            <Alert>{t('trace.nativeOnly')}</Alert>

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
                <div className="grid gap-4 sm:grid-cols-2">
                  <div className="space-y-1.5">
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
                  <div className="space-y-1.5">
                    <Label htmlFor="trace-direction">{t('trace.direction')}</Label>
                    <Select value={direction} onValueChange={(value) => { setDirection(value as TraceDirection); traceMutation.reset() }}>
                      <SelectTrigger id="trace-direction" className="w-full"><SelectValue /></SelectTrigger>
                      <SelectContent>
                        <SelectItem value="out">{t('trace.directionOut')}</SelectItem>
                        <SelectItem value="in">{t('trace.directionIn')}</SelectItem>
                      </SelectContent>
                    </Select>
                  </div>
                  <div className="space-y-1.5">
                    <Label htmlFor="trace-since">{t('trace.since')}</Label>
                    <Input id="trace-since" type="date" value={since} max={until || undefined} onChange={(event) => setSince(event.target.value)} />
                  </div>
                  <div className="space-y-1.5">
                    <Label htmlFor="trace-until">{t('trace.until')}</Label>
                    <Input id="trace-until" type="date" value={until} min={since || undefined} onChange={(event) => setUntil(event.target.value)} />
                  </div>
                </div>
                {invalidWindow && <Alert variant="warning">{t('trace.invalidWindow')}</Alert>}
                <details className="rounded-lg border border-border p-3">
                  <summary className="cursor-pointer text-sm font-medium">{t('trace.advanced')}</summary>
                  <div className="mt-4 grid gap-4 sm:grid-cols-3">
                    <div className="space-y-1.5">
                      <Label htmlFor="trace-min-amount">{t('trace.minAmount')}</Label>
                      <Input id="trace-min-amount" type="number" min="0" step="any" inputMode="decimal" value={minAmount} onChange={(event) => setMinAmount(event.target.value)} placeholder={t('trace.minAmountPlaceholder')} />
                    </div>
                    <div className="space-y-1.5">
                      <Label htmlFor="trace-max-hops">{t('trace.maxHops')}</Label>
                      <Select value={maxHops} onValueChange={(value) => { setMaxHops(value); traceMutation.reset() }}>
                        <SelectTrigger id="trace-max-hops" className="w-full"><SelectValue /></SelectTrigger>
                        <SelectContent>{HOP_OPTIONS.map((value) => <SelectItem key={value} value={String(value)}>{value}</SelectItem>)}</SelectContent>
                      </Select>
                      <p className="text-xs text-muted-foreground">{t('trace.maxHopsHelp')}</p>
                    </div>
                    <div className="space-y-1.5">
                      <Label htmlFor="trace-max-branches">{t('trace.maxBranches')}</Label>
                      <Select value={maxBranches} onValueChange={(value) => { setMaxBranches(value); traceMutation.reset() }}>
                        <SelectTrigger id="trace-max-branches" className="w-full"><SelectValue /></SelectTrigger>
                        <SelectContent>{BRANCH_OPTIONS.map((value) => <SelectItem key={value} value={String(value)}>{value}</SelectItem>)}</SelectContent>
                      </Select>
                      <p className="text-xs text-muted-foreground">{t('trace.maxBranchesHelp')}</p>
                    </div>
                  </div>
                </details>
                <Button type="submit" disabled={!canSubmit || traceMutation.isPending}>
                  {traceMutation.isPending ? t('trace.tracing') : t('trace.submit')}
                </Button>
              </>
            )}
          </form>
        </CardContent>
      </Card>

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
          <div className="flex flex-wrap items-center gap-2">
            <Badge variant="secondary">
              {result.direction === 'out' ? t('trace.directionOut') : t('trace.directionIn')}
            </Badge>
            <span className="text-sm text-muted-foreground">
              {t('trace.rootLabel')}
            </span>
            <AddressRef node={nodeById.get(result.root)} id={result.root} />
          </div>

          {result.truncated && <Alert variant="warning">{t('trace.truncated')}</Alert>}

          {hops.length === 0 && (
            <p className="text-sm text-muted-foreground">{t('trace.empty')}</p>
          )}

          {hops.map((hop) => (
            <div key={hop.depth} className="space-y-2">
              <h2 className="text-xs font-semibold uppercase tracking-[0.12em] text-muted-foreground">
                {hop.depth === 0 ? t('trace.origin') : t('trace.hop', { n: hop.depth })}
              </h2>

              {hop.edges.map((edge) => (
                <div
                  key={`${edge.reference}:${edge.source}:${edge.target}`}
                  className="flex flex-wrap items-center gap-x-3 gap-y-1 rounded-lg border border-border px-3.5 py-2.5"
                >
                  <AddressRef node={nodeById.get(edge.source)} id={edge.source} />
                  <ArrowRight size={14} className="shrink-0 text-muted-foreground" />
                  <AddressRef node={nodeById.get(edge.target)} id={edge.target} />
                  <span className="ml-auto text-sm font-medium tabular-nums text-foreground">
                    {mask(t('trace.amount', { amount: edge.amount, symbol: edge.symbol }))}
                  </span>
                  <span className="text-xs text-muted-foreground">
                    {format(new Date(edge.occurred_at), 'MMM d, yyyy HH:mm', {
                      locale: dateFnsLocale,
                    })}
                  </span>
                  <span className="flex w-full items-center gap-1 text-xs text-muted-foreground">
                    {t('trace.transactionReference')}
                    <span className="min-w-0 truncate font-mono" title={edge.reference}>{shorten(edge.reference)}</span>
                    <CopyButton value={edge.reference} label={t('trace.copyReference')} />
                  </span>
                </div>
              ))}

              {hop.terminals.map((node) => (
                <TerminalMarker key={node.id} node={node} />
              ))}
            </div>
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
