import { useMemo, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { Link } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { toast } from 'sonner'
import { Pencil, Plus, Trash2, Upload, Wallet } from 'lucide-react'

import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { DatePickerInput } from '@/components/ui/date-picker-input'
import { DeleteConfirmationDialog } from '@/components/delete-confirmation-dialog'
import {
  Dialog,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Skeleton } from '@/components/ui/skeleton'
import { assetErrorMessage, contributions as contributionsApi } from '@/lib/api'
import {
  CONTRIBUTION_KINDS,
  CONTRIBUTION_PARTIES,
  annualRows,
  draftError,
  draftFromContribution,
  draftPayload,
  emptyDraft,
  isPriorYearEntry,
  rowsByWallet,
  summariesByWallet,
  type ContributionDraft,
} from '@/lib/contributions'
import { localDateString } from '@/lib/date-utils'
import { formatCurrency } from '@/lib/format'
import type { AssetContribution, AssetGroup, ContributionSummary } from '@/types'

const YEARS_GRID = '0.7fr 1fr 1fr 1fr 1fr 1fr'
const MOVES_GRID = 'minmax(0,1fr) 1.1fr 0.9fr 0.7fr 1fr 1.1fr 3.5rem'
const SELECT_CLASS =
  'bg-card border border-border focus:outline-none focus:ring-2 focus:ring-primary px-3 py-2 rounded-lg text-foreground text-sm w-full'
const DASH = '—'

interface ContributionsTabProps {
  wallets: AssetGroup[]
  currency: string
  locale: string
  dateLocale: string
  mask: (value: string) => string
  canWrite: boolean
}

export default function ContributionsTab({
  wallets,
  currency,
  locale,
  dateLocale,
  mask,
  canWrite,
}: ContributionsTabProps) {
  const { t } = useTranslation()
  const queryClient = useQueryClient()
  const today = localDateString()

  const [editing, setEditing] = useState<AssetContribution | null>(null)
  const [dialogWalletId, setDialogWalletId] = useState<string | null>(null)
  const [deletingId, setDeletingId] = useState<string | null>(null)

  const { data: rows, isLoading } = useQuery({
    queryKey: ['contributions'],
    queryFn: () => contributionsApi.list(),
  })
  const { data: summaries } = useQuery({
    queryKey: ['contribution-summary'],
    queryFn: contributionsApi.summary,
  })

  const byWallet = useMemo(() => rowsByWallet(rows ?? []), [rows])
  const summaryOf = useMemo(() => summariesByWallet(summaries ?? []), [summaries])

  const deleteMutation = useMutation({
    mutationFn: (id: string) => contributionsApi.delete(id),
    onSuccess: () => {
      refetchContributionViews(queryClient)
      setDeletingId(null)
      toast.success(t('assets.contribDeleted'))
    },
    onError: (e) => toast.error(assetErrorMessage(e, t('common.error'))),
  })

  const money = (value: number | null | undefined) =>
    value === null || value === undefined ? DASH : mask(formatCurrency(value, currency, locale))
  const day = (value: string) => new Date(`${value}T00:00:00`).toLocaleDateString(dateLocale)

  function openCreate(groupId: string) {
    setEditing(null)
    setDialogWalletId(groupId)
  }

  function openEdit(row: AssetContribution) {
    setEditing(row)
    setDialogWalletId(row.group_id)
  }

  // A wallet with no rows has no Net Contribution, which is not the same thing
  // as zero — so it stays off this tab entirely rather than reading as empty.
  const tracked = wallets.filter((w) => (byWallet.get(w.id)?.length ?? 0) > 0 || summaryOf.has(w.id))

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <p className="text-xs text-muted-foreground">{t('assets.contribHint')}</p>
        <div className="flex items-center gap-2">
          <Button variant="outline" size="sm" asChild>
            <Link to="/import?tab=contributions">
              <Upload size={14} className="mr-1" />
              {t('assets.contribImportLink')}
            </Link>
          </Button>
          {canWrite && wallets.length > 0 && (
            <Button size="sm" onClick={() => openCreate(wallets[0].id)}>
              <Plus size={14} className="mr-1" />
              {t('assets.contribAdd')}
            </Button>
          )}
        </div>
      </div>

      {isLoading ? (
        <div className="space-y-3">
          {Array.from({ length: 2 }).map((_, i) => (
            <Skeleton key={i} className="h-32 rounded-xl" />
          ))}
        </div>
      ) : wallets.length === 0 ? (
        <EmptyCard message={t('assets.contribNoWallets')} />
      ) : tracked.length === 0 ? (
        <EmptyCard message={t('assets.contribNone')} />
      ) : (
        tracked.map((wallet) => (
          <div key={wallet.id} className="rounded-xl border border-border bg-card shadow-sm">
            <div className="flex flex-wrap items-center gap-2 border-b border-border px-4 py-3">
              <div
                className="flex h-6 w-6 shrink-0 items-center justify-center rounded-md"
                style={{ backgroundColor: `${wallet.color}20` }}
              >
                <Wallet size={13} style={{ color: wallet.color }} />
              </div>
              <span className="text-sm font-semibold text-foreground">{wallet.name}</span>
              {wallet.tax_treatment !== 'taxable' && (
                <Badge variant="secondary" className="text-[10px]">
                  {t(`assets.taxTreatment.${wallet.tax_treatment}`)}
                </Badge>
              )}
              {canWrite && (
                <Button
                  variant="ghost"
                  size="sm"
                  className="ml-auto h-7 text-xs"
                  onClick={() => openCreate(wallet.id)}
                >
                  <Plus size={13} className="mr-1" />
                  {t('assets.contribAdd')}
                </Button>
              )}
            </div>

            <SummaryStrip summary={summaryOf.get(wallet.id)} money={money} />

            <YearsTable summary={summaryOf.get(wallet.id)} money={money} />

            <MovementsTable
              rows={byWallet.get(wallet.id) ?? []}
              money={money}
              day={day}
              canWrite={canWrite}
              onEdit={openEdit}
              onDelete={setDeletingId}
            />
          </div>
        ))
      )}

      {dialogWalletId !== null && (
        <ContributionDialog
          key={editing?.id ?? dialogWalletId}
          wallets={wallets}
          walletId={dialogWalletId}
          editing={editing}
          today={today}
          onClose={() => {
            setDialogWalletId(null)
            setEditing(null)
          }}
        />
      )}

      <DeleteConfirmationDialog
        open={deletingId !== null}
        title={t('assets.contribConfirmDeleteTitle')}
        description={t('assets.contribConfirmDelete')}
        isPending={deleteMutation.isPending}
        onClose={() => setDeletingId(null)}
        onConfirm={() => deletingId && deleteMutation.mutate(deletingId)}
      />
    </div>
  )
}

function refetchContributionViews(queryClient: ReturnType<typeof useQueryClient>) {
  queryClient.refetchQueries({ queryKey: ['contributions'] })
  queryClient.refetchQueries({ queryKey: ['contribution-summary'] })
}

function EmptyCard({ message }: { message: string }) {
  return (
    <div className="rounded-xl border border-border bg-card px-4 py-10 text-center shadow-sm">
      <p className="text-sm text-muted-foreground">{message}</p>
    </div>
  )
}

function Stat({ label, value, hint, tone }: { label: string; value: string; hint?: string; tone?: string }) {
  return (
    <div className="space-y-0.5">
      <p className="text-[10px] uppercase tracking-wider text-muted-foreground">{label}</p>
      <p className={`text-sm font-semibold tabular-nums ${tone ?? 'text-foreground'}`}>{value}</p>
      {hint && <p className="text-[10px] text-muted-foreground">{hint}</p>}
    </div>
  )
}

function SummaryStrip({
  summary,
  money,
}: {
  summary: ContributionSummary | undefined
  money: (value: number | null | undefined) => string
}) {
  const { t } = useTranslation()
  if (!summary) return null
  const ret = summary.return_net_of_contributions

  return (
    <div className="grid grid-cols-2 gap-4 border-b border-border bg-muted/30 px-4 py-3 sm:grid-cols-3 lg:grid-cols-6">
      <Stat label={t('assets.contribNet')} value={money(summary.net)} hint={t('assets.contribNetHint')} />
      <Stat label={t('assets.contribOwn')} value={money(summary.own_contributions)} />
      <Stat
        label={t('assets.contribEmployerVested')}
        value={money(summary.employer_vested)}
        hint={
          summary.employer_unvested > 0
            ? t('assets.contribUnvestedExcluded', { amount: money(summary.employer_unvested) })
            : undefined
        }
      />
      <Stat label={t('assets.contribDistributions')} value={money(summary.distributions)} />
      <Stat label={t('assets.contribCurrentValue')} value={money(summary.current_value)} />
      <Stat
        label={t('assets.contribReturnNet')}
        value={money(ret)}
        tone={ret === null ? undefined : ret >= 0 ? 'text-emerald-600' : 'text-rose-500'}
      />
    </div>
  )
}

function YearsTable({
  summary,
  money,
}: {
  summary: ContributionSummary | undefined
  money: (value: number | null | undefined) => string
}) {
  const { t } = useTranslation()
  const years = annualRows(summary)
  if (years.length === 0) return null

  return (
    <div className="border-b border-border px-4 py-3">
      <p className="mb-1 text-xs font-semibold text-foreground">{t('assets.contribYears')}</p>
      <div
        className="grid items-center gap-2 py-1.5 text-[10px] font-semibold uppercase tracking-wider text-muted-foreground"
        style={{ gridTemplateColumns: YEARS_GRID }}
      >
        <div>{t('assets.contribColYear')}</div>
        <div className="text-right">{t('assets.contribColOwn')}</div>
        <div className="text-right">{t('assets.contribColEmployer')}</div>
        <div className="text-right">{t('assets.contribColGross')}</div>
        <div className="text-right">{t('assets.contribColDistributions')}</div>
        <div className="text-right">{t('assets.contribColNet')}</div>
      </div>
      {years.map((year) => (
        <div
          key={year.tax_year}
          className="grid items-center gap-2 border-t border-border/50 py-1.5 text-xs"
          style={{ gridTemplateColumns: YEARS_GRID }}
        >
          <div className="font-medium text-foreground tabular-nums">{year.tax_year}</div>
          <div className="text-right tabular-nums text-muted-foreground">{money(year.own)}</div>
          <div className="text-right tabular-nums text-muted-foreground">{money(year.employer)}</div>
          <div className="text-right font-medium tabular-nums text-foreground">{money(year.gross)}</div>
          <div className="text-right tabular-nums text-muted-foreground">{money(year.distributions)}</div>
          <div className="text-right tabular-nums text-foreground">{money(year.net)}</div>
        </div>
      ))}
      <p className="mt-1 text-[10px] text-muted-foreground">{t('assets.contribGrossHint')}</p>
    </div>
  )
}

function MovementsTable({
  rows,
  money,
  day,
  canWrite,
  onEdit,
  onDelete,
}: {
  rows: AssetContribution[]
  money: (value: number | null | undefined) => string
  day: (value: string) => string
  canWrite: boolean
  onEdit: (row: AssetContribution) => void
  onDelete: (id: string) => void
}) {
  const { t } = useTranslation()
  if (rows.length === 0) {
    return <p className="px-4 py-4 text-xs italic text-muted-foreground">{t('assets.contribNoRows')}</p>
  }

  return (
    <div className="px-4 py-3">
      <div
        className="grid items-center gap-2 py-1.5 text-[10px] font-semibold uppercase tracking-wider text-muted-foreground"
        style={{ gridTemplateColumns: MOVES_GRID }}
      >
        <div>{t('assets.contribColDate')}</div>
        <div>{t('assets.contribColKind')}</div>
        <div>{t('assets.contribColParty')}</div>
        <div className="text-right">{t('assets.contribColTaxYear')}</div>
        <div>{t('assets.contribColVesting')}</div>
        <div className="text-right">{t('assets.contribColAmount')}</div>
        <div />
      </div>
      {rows.map((row) => {
        const unvested = !row.is_vested
        return (
          <div
            key={row.id}
            className="grid items-center gap-2 border-t border-border/50 py-1.5 text-xs"
            style={{ gridTemplateColumns: MOVES_GRID }}
          >
            <div className="text-muted-foreground">{day(row.date)}</div>
            <div className="font-medium text-foreground">{t(`assets.contribKind_${row.kind}`)}</div>
            <div className="text-muted-foreground">{t(`assets.contribParty_${row.party}`)}</div>
            <div className="text-right tabular-nums text-muted-foreground">
              {row.tax_year}
              {row.tax_year !== Number(row.date.slice(0, 4)) && (
                <span className="ml-1 text-[10px] text-amber-600 dark:text-amber-400">*</span>
              )}
            </div>
            <div className="text-[10px] text-muted-foreground">
              {row.vested_on ? (
                unvested ? (
                  <Badge variant="secondary" className="text-[10px]">
                    {t('assets.contribVestsOn', { date: day(row.vested_on) })}
                  </Badge>
                ) : (
                  day(row.vested_on)
                )
              ) : (
                DASH
              )}
            </div>
            <div
              className={`text-right tabular-nums ${
                row.kind === 'distribution' ? 'text-rose-500' : 'text-foreground'
              }`}
            >
              {row.kind === 'distribution' ? '-' : ''}
              {money(row.amount)}
            </div>
            <div className="flex justify-end gap-1">
              {canWrite && (
                <>
                  <button
                    onClick={() => onEdit(row)}
                    className="rounded-lg p-1 text-muted-foreground transition-colors hover:bg-muted hover:text-foreground"
                    title={t('common.edit')}
                  >
                    <Pencil size={12} />
                  </button>
                  <button
                    onClick={() => onDelete(row.id)}
                    className="rounded-lg p-1 text-muted-foreground transition-colors hover:bg-rose-50 hover:text-rose-600"
                    title={t('common.delete')}
                  >
                    <Trash2 size={12} />
                  </button>
                </>
              )}
            </div>
          </div>
        )
      })}
      <p className="mt-1 text-[10px] text-muted-foreground">{t('assets.contribPriorYearLegend')}</p>
    </div>
  )
}

function ContributionDialog({
  wallets,
  walletId,
  editing,
  today,
  onClose,
}: {
  wallets: AssetGroup[]
  walletId: string
  editing: AssetContribution | null
  today: string
  onClose: () => void
}) {
  const { t } = useTranslation()
  const queryClient = useQueryClient()
  // Remounted per row by its `key`, so the initial draft is the whole reset.
  const [draft, setDraft] = useState<ContributionDraft>(() =>
    editing ? draftFromContribution(editing) : emptyDraft(walletId, today),
  )

  const set = <K extends keyof ContributionDraft>(key: K, value: ContributionDraft[K]) =>
    setDraft((prev) => ({ ...prev, [key]: value }))

  // The server refuses employer distributions and vesting on the user's own
  // money; switching the party here clears what it would have rejected.
  function setParty(party: ContributionDraft['party']) {
    setDraft((prev) => ({
      ...prev,
      party,
      kind: party === 'employer' ? 'contribution' : prev.kind,
      vestedOn: party === 'employer' ? prev.vestedOn : '',
    }))
  }

  function setKind(kind: ContributionDraft['kind']) {
    setDraft((prev) => ({
      ...prev,
      kind,
      party: kind === 'distribution' ? 'self' : prev.party,
      vestedOn: kind === 'distribution' ? '' : prev.vestedOn,
    }))
  }

  const saveMutation = useMutation({
    mutationFn: () => {
      const payload = draftPayload(draft)
      return editing
        ? contributionsApi.update(editing.id, payload)
        : contributionsApi.create(payload)
    },
    onSuccess: () => {
      refetchContributionViews(queryClient)
      onClose()
      toast.success(t('assets.contribSaved'))
    },
    onError: (e) => toast.error(assetErrorMessage(e, t('common.error'))),
  })

  const error = draftError(draft)

  return (
    <Dialog open onOpenChange={(o) => { if (!o) onClose() }}>
      <DialogContent className="max-w-md">
        <DialogHeader>
          <DialogTitle>{editing ? t('assets.contribEdit') : t('assets.contribAdd')}</DialogTitle>
        </DialogHeader>
        <div className="space-y-4">
          <div className="space-y-2">
            <Label>{t('assets.contribKind')}</Label>
            <div className="grid grid-cols-2 gap-2">
              {CONTRIBUTION_KINDS.map((k) => (
                <button
                  key={k}
                  type="button"
                  className={`rounded-lg border px-3 py-2 text-sm font-medium transition-all ${draft.kind === k ? 'border-primary bg-primary/10 text-primary' : 'border-border text-muted-foreground hover:border-primary/50'}`}
                  onClick={() => setKind(k)}
                >
                  {t(`assets.contribKind_${k}`)}
                </button>
              ))}
            </div>
          </div>

          <div className="space-y-2">
            <Label>{t('assets.contribWallet')}</Label>
            <select
              className={SELECT_CLASS}
              value={draft.groupId}
              onChange={(e) => set('groupId', e.target.value)}
            >
              {wallets.map((w) => (
                <option key={w.id} value={w.id}>{w.name}</option>
              ))}
            </select>
          </div>

          <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
            <div className="space-y-2">
              <Label>{t('assets.contribAmount')}</Label>
              <Input
                type="number"
                step="any"
                min="0"
                value={draft.amount}
                onChange={(e) => set('amount', e.target.value)}
              />
            </div>
            <div className="space-y-2">
              <Label>{t('assets.contribParty')}</Label>
              <select
                className={SELECT_CLASS}
                value={draft.party}
                onChange={(e) => setParty(e.target.value as ContributionDraft['party'])}
              >
                {CONTRIBUTION_PARTIES.map((p) => (
                  <option key={p} value={p} disabled={p === 'employer' && draft.kind === 'distribution'}>
                    {t(`assets.contribParty_${p}`)}
                  </option>
                ))}
              </select>
            </div>
          </div>

          <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
            <div className="space-y-2">
              <Label>{t('assets.contribDate')}</Label>
              <DatePickerInput value={draft.date} onChange={(v) => set('date', v)} />
            </div>
            <div className="space-y-2">
              <Label>{t('assets.contribTaxYear')}</Label>
              <Input
                type="number"
                step="1"
                min="1900"
                max="2200"
                value={draft.taxYear}
                onChange={(e) => set('taxYear', e.target.value)}
              />
            </div>
          </div>
          <p className="text-[11px] text-muted-foreground">{t('assets.contribTaxYearHint')}</p>
          {isPriorYearEntry(draft) && (
            <p className="text-[11px] text-amber-600 dark:text-amber-400">
              {t('assets.contribPriorYearNotice', {
                year: draft.taxYear,
                dateYear: draft.date.slice(0, 4),
              })}
            </p>
          )}

          <div className="space-y-2">
            <Label>{t('assets.contribVestedOn')}</Label>
            <DatePickerInput
              value={draft.vestedOn}
              onChange={(v) => set('vestedOn', v)}
              disabled={draft.party !== 'employer'}
            />
            <p className="text-[11px] text-muted-foreground">{t('assets.contribVestedOnHint')}</p>
          </div>

          <div className="space-y-2">
            <Label>{t('assets.contribNotes')}</Label>
            <Input value={draft.notes} onChange={(e) => set('notes', e.target.value)} />
          </div>

          {error && <p className="text-[11px] text-rose-500">{t(error)}</p>}
        </div>
        <DialogFooter>
          <Button variant="outline" onClick={onClose}>{t('common.cancel')}</Button>
          <Button onClick={() => saveMutation.mutate()} disabled={!!error || saveMutation.isPending}>
            {t('common.save')}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
