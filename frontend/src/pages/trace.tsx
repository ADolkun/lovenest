import { useEffect, useMemo, useRef, useState, type FormEvent } from 'react'
import { Link, Navigate, useLocation, useSearchParams } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import { useQuery, useMutation } from '@tanstack/react-query'
import { isValid, parseISO } from 'date-fns'
import { toast } from 'sonner'
import { ArrowRight, Building2, Check, Copy, Download, Flag, Link2, Radar } from 'lucide-react'
import { onchain } from '@/lib/api'
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
  TraceWindow,
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
  const { t, i18n } = useTranslation()
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
          {' · '}{node.balance_observed_at
            ? t('trace.balanceObserved', { time: `${new Date(node.balance_observed_at).toLocaleString(i18n.resolvedLanguage ?? i18n.language, { timeZone: 'UTC' })} UTC` })
            : t('trace.balanceObservationUnknown')}
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

function TraceCoverage({ result }: { result: TraceResult }) {
  const { t } = useTranslation()
  const unknown = t('trace.coverage.unknown')
  const moment = (value: string | null) => value?.replace('T', ' ').replace(/Z$/, ' UTC') ?? unknown
  const windowText = (window: TraceWindow) => `${window.since ? moment(window.since) : t('trace.coverage.openStart')} → ${window.until ? moment(window.until) : t('trace.coverage.openEnd')}`
  const flag = (value: boolean | null) => value === null ? unknown : t(value ? 'trace.coverage.yes' : 'trace.coverage.no')
  const reasonText = (reason: string) => t(`trace.coverage.reasons.${reason}`, { defaultValue: reason })
  const counters = ['pages_read', 'rows_read', 'signatures_read', 'payloads_requested', 'payloads_read', 'failed_payloads', 'pending_payloads', 'missing_timestamps', 'missing_payloads', 'unsupported_payloads', 'omitted_signatures', 'omitted_transfers'] as const

  return (
    <section aria-label={t('trace.coverage.title')} className="space-y-3">
      <div className="space-y-1">
        <h3 className="font-semibold">{t('trace.coverage.title')}</h3>
        <p className="text-sm">{t(result.complete ? 'trace.coverage.complete' : 'trace.coverage.incomplete')}</p>
        <p className="break-words text-sm text-muted-foreground">{t('trace.coverage.rootWindow')}: {windowText(result.root_window)}</p>
        <p className="max-w-prose text-sm text-muted-foreground">{t('trace.coverage.boundsHint')}</p>
      </div>
      <div className="divide-y divide-border border-y border-border">
        {result.nodes.map((node) => (
          <details key={node.id} className="py-3">
            <summary className="cursor-pointer break-all text-sm font-medium">
              {node.depth === 0 ? t('trace.origin') : t('trace.hop', { n: node.depth })} · {node.address}
              <span className="ml-2 font-normal text-muted-foreground">{t('trace.coverage.details')}</span>
            </summary>
            <div className="mt-3 space-y-3 text-sm">
              <p className="break-words">{t('trace.coverage.effectiveWindow')}: {windowText(node.effective_window)}</p>
              {node.stop_reasons.length > 0 && <ul className="list-disc space-y-1 pl-5">{node.stop_reasons.map((reason) => <li key={reason}>{reasonText(reason)}</li>)}</ul>}
              <p>{t('trace.coverage.branchOmitted')}: {node.branch_omitted_transfers}</p>
              {node.unfinished_windows.length > 0 && (
                <div className="space-y-1">
                  <h4 className="font-medium">{t('trace.coverage.unfinished')}</h4>
                  <ul className="list-disc space-y-1 pl-5">{node.unfinished_windows.map((window, index) => <li key={index} className="break-words">{windowText(window)} · {reasonText(window.reason)}</li>)}</ul>
                </div>
              )}
              {(node.window_coverages.length ? node.window_coverages : node.coverage ? [node.coverage] : []).map((coverage, index) => (
                <div key={index} className="space-y-3 border-t border-border pt-3">
                  <dl className="grid gap-x-6 gap-y-3 sm:grid-cols-2 xl:grid-cols-3">
                    <div><dt className="text-muted-foreground">{t('trace.coverage.requestedWindow')}</dt><dd className="break-words">{windowText({ since: coverage.requested_since, until: coverage.requested_until })}</dd></div>
                    <div><dt className="text-muted-foreground">{t('trace.coverage.observed')}</dt><dd className="break-words">{moment(coverage.observed_oldest)} → {moment(coverage.observed_newest)}</dd></div>
                    <div><dt className="text-muted-foreground">{t('trace.coverage.examined')}</dt><dd className="break-words">{moment(coverage.examined_oldest)} → {moment(coverage.examined_newest)}</dd></div>
                    <div><dt className="text-muted-foreground">{t('trace.coverage.fetchedAt')}</dt><dd>{moment(coverage.fetched_at)}</dd></div>
                    {(['since_reached', 'until_reached', 'provider_exhausted'] as const).map((key) => <div key={key}><dt className="text-muted-foreground">{t(`trace.coverage.${key}`)}</dt><dd>{flag(coverage[key])}</dd></div>)}
                    {counters.map((key) => <div key={key}><dt className="text-muted-foreground">{t(`trace.coverage.${key}`)}</dt><dd className="tabular-nums">{coverage[key] ?? unknown}</dd></div>)}
                  </dl>
                  {coverage.stop_reasons.length > 0 && <p>{t('trace.coverage.providerStops')}: {coverage.stop_reasons.map(reasonText).join(' · ')}</p>}
                  {coverage.next_cursor && <div className="space-y-1"><p className="font-medium">{t('trace.coverage.cursor')}</p><p className="break-all font-mono">{coverage.next_cursor}</p><p className="text-muted-foreground">{t('trace.coverage.cursorHint')}</p></div>}
                </div>
              ))}
              {!node.coverage && !node.window_coverages.length && <p className="text-muted-foreground">{t('trace.coverage.unavailable')}</p>}
            </div>
          </details>
        ))}
      </div>
    </section>
  )
}

const HOP_OPTIONS = [1, 2, 3, 4, 5, 6]
const BRANCH_OPTIONS = [1, 2, 3, 4, 5]

const TRACE_ERROR_CODES = ['upstream_rate_limited', 'trace_admission_limited', 'history_unavailable', 'trace_restart_required', 'trace_checkpoint_unavailable', 'trace_file_invalid', 'trace_file_workspace', 'trace_file_wallet'] as const

function retryDelay(value: unknown): number | null {
  return typeof value === 'number' && Number.isSafeInteger(value) && value >= 0 ? value : null
}

function traceFailure(error: unknown) {
  const localCode = error instanceof Error ? TRACE_ERROR_CODES.find((code) => code === error.message) : undefined
  if (localCode) return { code: localCode, retry_after_seconds: null }
  const detail = (error as { response?: { data?: { detail?: unknown } } } | null)?.response?.data?.detail
  if (!detail || typeof detail !== 'object' || !('code' in detail)) return null
  const code = TRACE_ERROR_CODES.find((candidate) => candidate === detail.code)
  if (!code) return null
  return { code, retry_after_seconds: retryDelay('retry_after_seconds' in detail ? detail.retry_after_seconds : null) }
}

type TraceOperation = { generation: number } & (
  | { kind: 'trace'; request: TraceRequest }
  | { kind: 'reopen'; file: File }
)

/** A download supplies only a lookup token; never trust its embedded evidence. */
async function savedTraceToken(file: File, workspaceId: string): Promise<string> {
  if (file.size > 8 * 1024 * 1024) throw new Error('trace_file_invalid')
  let saved
  try {
    const text = await new Promise<string>((resolve, reject) => {
      const reader = new FileReader()
      reader.onload = () => resolve(String(reader.result))
      reader.onerror = () => reject(reader.error)
      reader.readAsText(file)
    })
    saved = JSON.parse(text)
  } catch {
    throw new Error('trace_file_invalid')
  }
  if (!saved || saved.version !== 1 || typeof saved.workspace_id !== 'string') throw new Error('trace_file_invalid')
  if (saved.workspace_id !== workspaceId) throw new Error('trace_file_workspace')
  const token = saved.result?.continuation?.token
  if (typeof token !== 'string' || !/^[A-Za-z0-9_-]{16,256}$/.test(token)) throw new Error('trace_file_invalid')
  return token
}

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
  // A timezone offset can move an otherwise four-digit date outside the
  // range shared by the datetime input and trace API. Never retain an invisible bound.
  return isValid(parsed) && parsed.getUTCFullYear() >= 1 && parsed.getUTCFullYear() <= 9999
    ? parsed.toISOString().slice(0, -1) : ''
}

export function OwnedWalletActivity(props: OwnedWalletActivityProps) {
  const { current } = useWorkspace()
  const [params] = useSearchParams()
  // The form resets on URL/filter edits; a server cooldown still applies.
  const [retryUntil, setRetryUntil] = useState(0)
  const [now, setNow] = useState(Date.now)
  const coolingDown = now < retryUntil
  useEffect(() => {
    if (!coolingDown) return
    const timer = window.setInterval(() => setNow(Date.now()), 1000)
    return () => window.clearInterval(timer)
  }, [coolingDown])
  if (!current) return null
  // Every trace setting participates in the reset, including browser history
  // navigation. A URL only selects a saved address; it never starts a request.
  const initial = {
    selectedKey: params.has('chain') && params.has('address') ? `${params.get('chain')}:${params.get('address')}` : '',
    direction: params.get('direction') === 'in' ? 'in' as const : 'out' as const,
    maxHops: HOP_OPTIONS.includes(Number(params.get('max_hops'))) ? String(Number(params.get('max_hops'))) : '3',
    maxBranches: BRANCH_OPTIONS.includes(Number(params.get('max_branches'))) ? String(Number(params.get('max_branches'))) : '3',
    // HTML number inputs reject whitespace and a trailing decimal point.
    // Normalize the spelling as text so the displayed and submitted amounts
    // agree without rounding a precise quantity through a JS number.
    minAmount: (params.get('min_amount') ?? '').trim().replace(/^(\d+)\.(e[+-]?\d+)?$/i, '$1$2'),
    since: traceDateInput(params.get('since')),
    until: traceDateInput(params.get('until'), true),
  }
  const key = JSON.stringify([current.id, initial, props.connectionIds, props.addressKeys])
  return <WalletActivityForm key={key} {...props} workspaceId={current.id} initial={initial}
    retrySeconds={Math.max(0, Math.ceil((retryUntil - now) / 1000))}
    onRateLimited={(delay) => {
      const receivedAt = Date.now()
      setNow(receivedAt)
      setRetryUntil((previous) => Math.max(previous, receivedAt + Math.max(5, retryDelay(delay) ?? 5) * 1000))
    }} />
}

function WalletActivityForm({
  workspaceId, initial, connectionIds, addressKeys, retrySeconds, onRateLimited,
}: OwnedWalletActivityProps & {
  workspaceId: string
  initial: { selectedKey: string; direction: TraceDirection; maxHops: string; maxBranches: string; minAmount: string; since: string; until: string }
  retrySeconds: number
  onRateLimited: (delay: number | null) => void
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
  const [saved, setSaved] = useState<TraceResult | null>(null)
  const generation = useRef(0)
  const [expiryNow, setExpiryNow] = useState(Date.now)
  useEffect(() => () => { generation.current += 1 }, [])
  const expiresAt = saved?.continuation.expires_at
  useEffect(() => {
    if (!expiresAt) return
    const remaining = Date.parse(expiresAt) - Date.now()
    const timer = window.setTimeout(() => setExpiryNow(Date.now()), Math.max(0, Math.min(remaining, 2_147_483_647)))
    return () => window.clearTimeout(timer)
  }, [expiresAt])
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

  const chains = chainsQuery.data ?? []
  const watched = (watchedQuery.data ?? []).filter((entry) =>
    (connectionIds === undefined || connectionIds.includes(entry.connection_id)) &&
    (addressKeys === undefined || addressKeys.includes(`${entry.chain}:${entry.address}`)),
  )
  const traceMutation = useMutation({
    retry: false,
    mutationFn: async (operation: TraceOperation) => {
      let result: TraceResult
      if (operation.kind === 'reopen') {
        const token = await savedTraceToken(operation.file, workspaceId)
        if (operation.generation !== generation.current) return null
        result = await onchain.checkpoint(token, workspaceId)
      } else {
        result = await onchain.trace(operation.request, workspaceId)
      }
      if (result.workspace_id !== workspaceId) throw new Error('trace_file_workspace')
      return result
    },
    onSuccess: (result, operation) => {
      if (!result || operation.generation !== generation.current) return
      if (!watched.some((entry) => entry.chain === result.request.chain && entry.address === result.request.address)) throw new Error('trace_file_wallet')
      setSaved(result)
      if (operation.kind === 'reopen') {
        const request = result.request
        setSelectedKey(`${request.chain}:${request.address}`)
        setDirection(request.direction ?? 'out')
        setMaxHops(String(request.max_hops ?? 3))
        setMaxBranches(String(request.max_branches ?? 3))
        setMinAmount(request.min_amount == null ? '' : String(request.min_amount))
        setSince(traceDateInput(request.since ?? null))
        setUntil(traceDateInput(request.until ?? null, true))
      }
      // Reopening an old observation must not start a fresh provider cooldown.
      if (operation.kind === 'trace' && result.interruption?.code === 'upstream_rate_limited') onRateLimited(result.interruption.retry_after_seconds)
    },
    onError: (error, operation) => {
      if (operation.generation !== generation.current) return
      const failure = traceFailure(error)
      if (failure?.code === 'upstream_rate_limited' || failure?.code === 'trace_admission_limited') {
        onRateLimited(failure.retry_after_seconds)
      }
    },
  })
  const failure = traceFailure(traceMutation.error)
  const selected = watched.find((entry) => `${entry.chain}:${entry.address}` === selectedKey)
  const selectedChain = chains.find((entry) => entry.key === selected?.chain)
  const invalidWindow = Boolean(since && until && Date.parse(`${since}Z`) > Date.parse(`${until}Z`))
  const invalidAmount = Boolean(minAmount.trim() && (!/^(?:\d+(?:\.\d*)?|\.\d+)(?:e[+-]?\d+)?$/i.test(minAmount.trim()) || !Number.isFinite(Number(minAmount))))
  const canSubmit = Boolean(selected && selectedChain?.traceable && !invalidWindow && !invalidAmount && !watchedQuery.isError && !chainsQuery.isError)

  const result = selected && !watchedQuery.isError && selected.chain === saved?.request.chain && selected.address === saved.request.address ? saved : undefined
  const expired = Boolean(expiresAt && Date.parse(expiresAt) <= expiryNow)
  const canContinue = Boolean(result?.continuation.status === 'available' && result.continuation.token && !expired && failure?.code !== 'trace_restart_required')
  const retryable = failure?.code === 'upstream_rate_limited' || failure?.code === 'trace_admission_limited' || result?.interruption?.code === 'upstream_rate_limited'
  const hops = useMemo(() => (result ? buildHops(result) : []), [result])
  const nodeById = useMemo(
    () => new Map((result?.nodes ?? []).map((node) => [node.id, node])),
    [result],
  )

  const submit = (event: FormEvent) => {
    event.preventDefault()
    if (!canSubmit || !selected || traceMutation.isPending || retrySeconds > 0) return
    traceMutation.mutate({ kind: 'trace', generation: ++generation.current, request: {
      chain: selected.chain,
      address: selected.address,
      direction,
      max_hops: Number(maxHops),
      max_branches: Number(maxBranches),
      ...(minAmount.trim() ? { min_amount: minAmount.trim() } : {}),
      ...(since ? { since: new Date(`${since}Z`).toISOString() } : {}),
      ...(until ? { until: new Date(`${until}Z`).toISOString() } : {}),
    } })
  }

  const invalidate = () => {
    generation.current += 1
    setSaved(null)
    traceMutation.reset()
  }
  const continueTrace = () => {
    if (!canSubmit || !canContinue || !result?.continuation.token || traceMutation.isPending || retrySeconds > 0) return
    if (expiresAt && Date.parse(expiresAt) <= Date.now()) { setExpiryNow(Date.now()); return }
    traceMutation.mutate({ kind: 'trace', generation: ++generation.current, request: { ...result.request, continuation_token: result.continuation.token } })
  }

  const traceLink = new URL(location.pathname, window.location.origin)
  traceLink.search = location.search
  for (const name of ['chain', 'address', 'direction', 'max_hops', 'max_branches', 'min_amount', 'since', 'until', 'continuation_token']) traceLink.searchParams.delete(name)
  for (const [name, value] of Object.entries(result?.request ?? {})) {
    if (name !== 'continuation_token' && value != null) traceLink.searchParams.set(name, String(value))
  }

  const download = () => {
    if (!result) return
    try {
      const snapshot = {
        source: 'Lovenest native-coin trace',
        version: 1,
        workspace_id: workspaceId,
        coverage: 'Bounded native-coin transfers only. Tokens, fees, swaps, bridges, exchange activity, claims and cost basis are not reconstructed. Downstream movements after funds mix cannot be attributed solely to this wallet. Keep terminal reasons and truncation with this result.',
        request: result.request,
        retrieved_at: result.retrieved_at,
        result,
      }
      const url = URL.createObjectURL(new Blob([JSON.stringify(snapshot, null, 2)], { type: 'application/json' }))
      const anchor = document.createElement('a')
      anchor.href = url
      anchor.download = `wallet-trace-${result.request.chain}-${result.retrieved_at.slice(0, 10)}.json`
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
          <form onSubmit={submit} className="space-y-4">
            <div className="flex flex-wrap items-start justify-between gap-3">
              <div className="min-w-0 space-y-1"><h2 className="text-base font-semibold">{t('trace.exploreTitle')}</h2><p className="max-w-2xl text-sm text-muted-foreground">{t('trace.intro')}</p></div>
              <Button asChild variant="outline" size="sm"><Link to="/accounts">{t('trace.manageWallets')}</Link></Button>
            </div>
            <p className="text-xs leading-relaxed text-muted-foreground">{t('trace.nativeOnly')}</p>
            <p className="text-xs leading-relaxed text-muted-foreground">{t('trace.requestCostHint')}</p>

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
                    <Select value={selected ? selectedKey : ''} onValueChange={(value) => { setSelectedKey(value); invalidate() }}>
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
                    <Select value={direction} onValueChange={(value) => { setDirection(value as TraceDirection); invalidate() }}>
                      <SelectTrigger id="trace-direction" className="w-full"><SelectValue /></SelectTrigger>
                      <SelectContent>
                        <SelectItem value="out">{t('trace.directionOut')}</SelectItem>
                        <SelectItem value="in">{t('trace.directionIn')}</SelectItem>
                      </SelectContent>
                    </Select>
                  </div>
                  <div className="min-w-0 space-y-1.5">
                    <Label htmlFor="trace-since">{t('trace.since')}</Label>
                    <Input id="trace-since" type="datetime-local" step="any" aria-describedby="trace-window-hint" className="min-w-0" value={since} max={until || undefined} onChange={(event) => { setSince(event.target.value); invalidate() }} />
                  </div>
                  <div className="min-w-0 space-y-1.5">
                    <Label htmlFor="trace-until">{t('trace.until')}</Label>
                    <Input id="trace-until" type="datetime-local" step="any" aria-describedby="trace-window-hint" className="min-w-0" value={until} min={since || undefined} onChange={(event) => { setUntil(event.target.value); invalidate() }} />
                  </div>
                </div>
                <p id="trace-window-hint" className="max-w-prose text-sm text-muted-foreground">{t(direction === 'out' ? 'trace.rootWindowOut' : 'trace.rootWindowIn')}</p>
                {invalidWindow && <Alert variant="warning">{t('trace.invalidWindow')}</Alert>}
                {invalidAmount && <Alert variant="warning">{t('trace.invalidAmount')}</Alert>}
                <details className="border-t border-border pt-3" open={maxHops !== '3' || maxBranches !== '3' || Boolean(minAmount)}>
                  <summary className="cursor-pointer text-sm font-medium">{t('trace.advanced')}</summary>
                  <div className="mt-4 grid gap-4 sm:grid-cols-3">
                    <div className="min-w-0 space-y-1.5">
                      <Label htmlFor="trace-min-amount">{t('trace.minAmount')}{selectedChain ? ` (${selectedChain.symbol})` : ''}</Label>
                      <Input id="trace-min-amount" type="number" min="0" step="any" inputMode="decimal" value={minAmount} onChange={(event) => { setMinAmount(event.target.value); invalidate() }} placeholder={t('trace.minAmountPlaceholder')} />
                    </div>
                    <div className="min-w-0 space-y-1.5">
                      <Label htmlFor="trace-max-hops">{t('trace.maxHops')}</Label>
                      <Select value={maxHops} onValueChange={(value) => { setMaxHops(value); invalidate() }}>
                        <SelectTrigger id="trace-max-hops" className="w-full"><SelectValue /></SelectTrigger>
                        <SelectContent>{HOP_OPTIONS.map((value) => <SelectItem key={value} value={String(value)}>{value}</SelectItem>)}</SelectContent>
                      </Select>
                      <p className="text-xs text-muted-foreground">{t('trace.maxHopsHelp')}</p>
                    </div>
                    <div className="min-w-0 space-y-1.5">
                      <Label htmlFor="trace-max-branches">{t('trace.maxBranches')}</Label>
                      <Select value={maxBranches} onValueChange={(value) => { setMaxBranches(value); invalidate() }}>
                        <SelectTrigger id="trace-max-branches" className="w-full"><SelectValue /></SelectTrigger>
                        <SelectContent>{BRANCH_OPTIONS.map((value) => <SelectItem key={value} value={String(value)}>{value}</SelectItem>)}</SelectContent>
                      </Select>
                      <p className="text-xs text-muted-foreground">{t('trace.maxBranchesHelp')}</p>
                    </div>
                  </div>
                </details>
                <div className="flex flex-wrap items-center gap-3 border-t border-border pt-4">
                  {result?.continuation.status === 'available' && <Button type="button" onClick={continueTrace} disabled={!canSubmit || !canContinue || traceMutation.isPending || retrySeconds > 0}>{t('trace.continue')}</Button>}
                  <Button type="submit" variant={result ? 'outline' : 'default'} disabled={!canSubmit || traceMutation.isPending || retrySeconds > 0}>
                    <Radar size={16} />
                    {traceMutation.isPending ? t(traceMutation.variables?.kind === 'reopen' ? 'trace.reopening' : 'trace.tracing') : result || failure?.code === 'trace_restart_required' ? t('trace.restart') : retryable ? t('common.retry') : t('trace.submit')}
                  </Button>
                  {retrySeconds > 0 && <span role="status" className="text-xs text-muted-foreground">{t('trace.retryIn', { seconds: retrySeconds })}</span>}
                  <span className="text-xs text-muted-foreground">{t('trace.utcHint')}</span>
                </div>
              </>
            )}
          </form>
          <div className="mt-4 space-y-2 border-t border-border pt-4">
            <Label htmlFor="trace-reopen">{t('trace.reopen')}</Label>
            <Input id="trace-reopen" type="file" accept="application/json,.json" className="max-w-sm" aria-describedby="trace-reopen-hint" disabled={traceMutation.isPending || !watched.length || chainsQuery.isError || watchedQuery.isError} onChange={(event) => {
              const file = event.target.files?.[0]
              event.target.value = ''
              if (file) traceMutation.mutate({ kind: 'reopen', file, generation: ++generation.current })
            }} />
            <p id="trace-reopen-hint" className="max-w-prose text-xs text-muted-foreground">{t('trace.reopenHint')}</p>
          </div>
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

      {traceMutation.isPending && !result && (
        <div className="space-y-3">
          <Skeleton className="h-6 w-40" />
          <Skeleton className="h-16 w-full" />
          <Skeleton className="h-16 w-full" />
          <Skeleton className="h-16 w-full" />
        </div>
      )}

      {result && (
        <div className="space-y-5">
          <div className="flex flex-wrap items-start justify-between gap-3 border-b border-border pb-4">
            <div className="space-y-2">
              <h2 className="font-semibold">{t('trace.resultsTitle')}</h2>
              <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
                <Badge variant="secondary">{result.direction === 'out' ? t('trace.directionOut') : t('trace.directionIn')}</Badge>
                <span className="text-sm text-muted-foreground">{t('trace.rootLabel')}</span>
                <AddressRef node={nodeById.get(result.root)} id={result.root} />
              </div>
              <p className="text-xs text-muted-foreground">{t('trace.resultCounts', { transfers: result.edges.length, addresses: result.nodes.length })} · {t('trace.retrievedAt', { time: formatUtc(result.retrieved_at) })}</p>
              <p className="max-w-prose text-xs text-muted-foreground">{t('trace.savedBalances')}</p>
            </div>
            <div className="flex flex-wrap gap-2">
              <CopyButton value={traceLink.href} label={t('trace.copyLink')} showLabel />
              <Button type="button" variant="outline" size="sm" onClick={download}><Download size={14} />{t('trace.download')}</Button>
            </div>
          </div>

          <div className="space-y-1 text-sm" aria-live="polite">
            {traceMutation.isPending && <p>{t('trace.retainingResult')}</p>}
            <p>{t(expired || failure?.code === 'trace_restart_required' ? 'trace.checkpointExpired' : `trace.continuation.${result.continuation.status}`)}</p>
            {expiresAt && <p className="text-muted-foreground">{t('trace.expiresAt', { time: formatUtc(expiresAt) })}</p>}
          </div>

          {(result.truncated || result.interruption) && (
            <Alert variant="warning" className="block space-y-2">
              {result.interruption && <p>{result.interruption.phase === 'balances'
                ? t(result.interruption.code === 'deadline_exceeded' ? 'trace.balanceDeadline' : 'trace.balanceRateLimited')
                : t(result.interruption.code === 'deadline_exceeded' ? 'trace.deadline' : 'trace.error.upstream_rate_limited')}</p>}
              {result.truncated && <p>{t('trace.truncated')}</p>}
              {result.interruption?.code === 'upstream_rate_limited' && <p>{t('trace.rateLimitHelp')}</p>}
            </Alert>
          )}

          {result.root_window && <TraceCoverage result={result} />}

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
        <Alert variant="warning" className="block space-y-2">
          <p>{failure ? t(`trace.error.${failure.code}`) : t('trace.failed')}</p>
          {failure?.code === 'upstream_rate_limited' && <p>{t('trace.rateLimitHelp')}</p>}
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
