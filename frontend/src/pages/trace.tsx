import { useMemo, useState, type FormEvent } from 'react'
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

function AddressRef({ node, id }: { node: TraceNode | undefined; id: string }) {
  const { t } = useTranslation()
  const { mask } = usePrivacyMode()
  const [copied, setCopied] = useState(false)
  const address = node?.address ?? id.split(':').slice(1).join(':')

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(address)
      setCopied(true)
      setTimeout(() => setCopied(false), 1500)
    } catch {
      toast.error(t('trace.copyFailed'))
    }
  }

  return (
    <span className="inline-flex items-center gap-1">
      <Tooltip>
        <TooltipTrigger asChild>
          <span className="font-mono text-xs text-foreground">{shorten(address)}</span>
        </TooltipTrigger>
        <TooltipContent className="font-mono text-xs">{address}</TooltipContent>
      </Tooltip>
      <Button
        type="button"
        variant="ghost"
        size="icon"
        className="size-6 text-muted-foreground"
        onClick={copy}
        aria-label={t('trace.copyAddress')}
      >
        {copied ? <Check size={12} /> : <Copy size={12} />}
      </Button>
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
  // Reaching a pooled address is the actionable outcome of a trace, not an
  // error: it names the party who can be asked whose account received the
  // funds. It is the one ending worth pulling the eye to.
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
  const { t, i18n } = useTranslation()
  const dateFnsLocale = resolveDateFnsLocale(i18n.resolvedLanguage ?? i18n.language)

  const [chain, setChain] = useState('')
  const [address, setAddress] = useState('')
  const [direction, setDirection] = useState<TraceDirection>('out')
  const [maxHops, setMaxHops] = useState('3')
  const [maxBranches, setMaxBranches] = useState('3')
  const [minAmount, setMinAmount] = useState('')
  const { mask } = usePrivacyMode()

  const chainsQuery = useQuery({
    queryKey: ['onchain', 'chains'],
    queryFn: onchain.chains,
    staleTime: Infinity,
  })
  const watchedQuery = useQuery({
    queryKey: ['onchain', 'addresses'],
    queryFn: onchain.addresses,
  })

  const traceMutation = useMutation({
    mutationFn: onchain.trace,
    onError: (error) => toast.error(extractApiError(error, t('trace.failed'))),
  })

  const chains = chainsQuery.data ?? []
  const chainsUnavailable = chainsQuery.isError
  const watched = watchedQuery.data ?? []
  const selectedChain = chains.find((c) => c.key === chain)
  const canSubmit = Boolean(chain && address.trim()) && selectedChain?.traceable !== false

  const result = traceMutation.data
  const hops = useMemo(() => (result ? buildHops(result) : []), [result])
  const nodeById = useMemo(
    () => new Map((result?.nodes ?? []).map((node) => [node.id, node])),
    [result],
  )

  const submit = (event: FormEvent) => {
    event.preventDefault()
    if (!canSubmit) return
    traceMutation.mutate({
      chain,
      address: address.trim(),
      direction,
      max_hops: Number(maxHops),
      max_branches: Number(maxBranches),
      ...(minAmount.trim() ? { min_amount: minAmount.trim() } : {}),
    })
  }

  return (
    <div>
      <PageHeader section={t('trace.section')} title={t('trace.title')} />

      <Card className="mb-6">
        <CardContent>
          <form onSubmit={submit} className="space-y-4">
            <p className="text-sm text-muted-foreground">{t('trace.intro')}</p>

            {chainsUnavailable && (
              <Alert variant="warning">{t('trace.chainsUnavailable')}</Alert>
            )}

            {watched.length > 0 && (
              <div className="space-y-1.5">
                <Label>{t('trace.quickPick')}</Label>
                <div className="flex flex-wrap gap-2">
                  {watched.map((entry) => (
                    <Button
                      key={`${entry.chain}:${entry.address}`}
                      type="button"
                      variant="outline"
                      size="sm"
                      onClick={() => {
                        setChain(entry.chain)
                        setAddress(entry.address)
                      }}
                    >
                      {entry.label}
                    </Button>
                  ))}
                </div>
              </div>
            )}

            <div className="grid gap-4 sm:grid-cols-2">
              <div className="space-y-1.5">
                <Label htmlFor="trace-chain">{t('trace.chain')}</Label>
                <Select value={chain} onValueChange={setChain}>
                  <SelectTrigger id="trace-chain" className="w-full">
                    <SelectValue placeholder={t('trace.chainPlaceholder')} />
                  </SelectTrigger>
                  <SelectContent>
                    {chains.map((option) => (
                      <SelectItem
                        key={option.key}
                        value={option.key}
                        disabled={!option.traceable}
                      >
                        <span className="flex items-center gap-2">
                          {option.display_name}
                          {!option.traceable && (
                            <span className="text-xs text-muted-foreground">
                              {t('trace.chainNotTraceable')}
                            </span>
                          )}
                        </span>
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
                {chains.some((option) => !option.traceable) && (
                  <p className="text-xs text-muted-foreground">
                    {t('trace.chainNotTraceableHelp')}
                  </p>
                )}
              </div>

              <div className="space-y-1.5">
                <Label htmlFor="trace-address">{t('trace.address')}</Label>
                <Input
                  id="trace-address"
                  className="font-mono"
                  value={address}
                  onChange={(event) => setAddress(event.target.value)}
                  placeholder={t('trace.addressPlaceholder')}
                  spellCheck={false}
                  autoComplete="off"
                />
              </div>

              <div className="space-y-1.5">
                <Label htmlFor="trace-direction">{t('trace.direction')}</Label>
                <Select
                  value={direction}
                  onValueChange={(value) => setDirection(value as TraceDirection)}
                >
                  <SelectTrigger id="trace-direction" className="w-full">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="out">{t('trace.directionOut')}</SelectItem>
                    <SelectItem value="in">{t('trace.directionIn')}</SelectItem>
                  </SelectContent>
                </Select>
              </div>

              <div className="space-y-1.5">
                <Label htmlFor="trace-min-amount">{t('trace.minAmount')}</Label>
                <Input
                  id="trace-min-amount"
                  inputMode="decimal"
                  value={minAmount}
                  onChange={(event) => setMinAmount(event.target.value)}
                  placeholder={t('trace.minAmountPlaceholder')}
                />
              </div>

              <div className="space-y-1.5">
                <Label htmlFor="trace-max-hops">{t('trace.maxHops')}</Label>
                <Select value={maxHops} onValueChange={setMaxHops}>
                  <SelectTrigger id="trace-max-hops" className="w-full">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    {HOP_OPTIONS.map((value) => (
                      <SelectItem key={value} value={String(value)}>
                        {value}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
                <p className="text-xs text-muted-foreground">{t('trace.maxHopsHelp')}</p>
              </div>

              <div className="space-y-1.5">
                <Label htmlFor="trace-max-branches">{t('trace.maxBranches')}</Label>
                <Select value={maxBranches} onValueChange={setMaxBranches}>
                  <SelectTrigger id="trace-max-branches" className="w-full">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    {BRANCH_OPTIONS.map((value) => (
                      <SelectItem key={value} value={String(value)}>
                        {value}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
                <p className="text-xs text-muted-foreground">{t('trace.maxBranchesHelp')}</p>
              </div>
            </div>

            <Button type="submit" disabled={!canSubmit || traceMutation.isPending}>
              {traceMutation.isPending ? t('trace.tracing') : t('trace.submit')}
            </Button>
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
