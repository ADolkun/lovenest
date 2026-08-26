import { useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { useNavigate } from 'react-router-dom'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { toast } from 'sonner'
import { AlertTriangle, CheckCircle2, Copy, FileText, Info, Upload, X } from 'lucide-react'

import { Button } from '@/components/ui/button'
import { Label } from '@/components/ui/label'
import { assetErrorMessage, assetGroups as assetGroupsApi, contributions as contributionsApi } from '@/lib/api'
import { useWorkspace } from '@/contexts/workspace-context'
import type { ContributionImportPreview } from '@/types'

const SELECT_CLASS =
  'border border-border rounded-md px-3 py-2 text-sm bg-card focus:outline-none focus-visible:ring-ring/30 focus-visible:ring-[2px]'
const ROWS_GRID = '3rem 1fr 0.7fr 1.1fr 0.9fr 1fr 1.1fr'
const DASH = '—'

/** The names inside "…(A, B). Choose which one this wallet is." — the refusal
    is the only place a multi-account file's accounts are reported, because the
    preview it would have come with is exactly what was refused. */
function accountsNamedIn(error: unknown): string[] {
  const detail = (error as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail
  if (typeof detail !== 'string') return []
  const listed = /\(([^)]+)\)/.exec(detail)
  return listed ? listed[1].split(',').map((name) => name.trim()).filter(Boolean) : []
}

/**
 * The contributions half of the import page (ADR 0001's 2026-08-23 amendment:
 * views live on `/assets`, files are uploaded here). A brokerage history file
 * names its own wallet nowhere, so the destination is picked before the file
 * is read — which is also what makes duplicate detection possible.
 */
export function ContributionImportPanel() {
  const { t } = useTranslation()
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const { canWrite } = useWorkspace()
  const fileInputRef = useRef<HTMLInputElement | null>(null)

  const [file, setFile] = useState<File | null>(null)
  const [preview, setPreview] = useState<ContributionImportPreview | null>(null)
  const [groupId, setGroupId] = useState('')
  const [dateFormat, setDateFormat] = useState('')
  // One export routinely covers several accounts while an import writes into
  // one wallet, so the backend refuses to guess and the choice is made here.
  const [account, setAccount] = useState('')
  const [accountChoices, setAccountChoices] = useState<string[]>([])
  const [loading, setLoading] = useState(false)
  const [importing, setImporting] = useState(false)
  const [dragOver, setDragOver] = useState(false)

  const { data: wallets } = useQuery({
    queryKey: ['asset-groups'],
    queryFn: assetGroupsApi.list,
  })

  async function runPreview(
    selected: File,
    nextGroup: string,
    nextDateFormat: string,
    nextAccount: string,
  ) {
    if (!nextGroup) {
      setPreview(null)
      return
    }
    setLoading(true)
    try {
      const result = await contributionsApi.previewImport(
        selected,
        nextGroup,
        nextDateFormat || undefined,
        nextAccount || undefined,
      )
      setPreview(result)
      setAccountChoices(result.accounts)
    } catch (e) {
      // A multi-account file is refused until one is named, and the refusal
      // lists them — so the message is the instruction, not just an error.
      toast.error(assetErrorMessage(e, t('contribImport.previewError')))
      setPreview(null)
      setAccountChoices(accountsNamedIn(e))
    } finally {
      setLoading(false)
    }
  }

  function handleFile(selected: File | null) {
    setFile(selected)
    setPreview(null)
    setAccount('')
    setAccountChoices([])
    if (selected) runPreview(selected, groupId, dateFormat, '')
  }

  function handleReset() {
    handleFile(null)
    if (fileInputRef.current) fileInputRef.current.value = ''
  }

  function handleWalletChange(value: string) {
    setGroupId(value)
    if (file) runPreview(file, value, dateFormat, account)
  }

  function handleAccountChange(value: string) {
    setAccount(value)
    if (file) runPreview(file, groupId, dateFormat, value)
  }

  function handleDateFormatChange(value: string) {
    setDateFormat(value)
    if (file) runPreview(file, groupId, value, account)
  }

  async function handleImport() {
    if (!file || !groupId) return
    setImporting(true)
    try {
      const result = await contributionsApi.import(
        file,
        groupId,
        dateFormat || undefined,
        account || undefined,
      )
      queryClient.invalidateQueries({ queryKey: ['contributions'] })
      queryClient.invalidateQueries({ queryKey: ['contribution-summary'] })
      queryClient.invalidateQueries({ queryKey: ['import-logs'] })
      toast.success(t('contribImport.imported', { count: result.created }))
      navigate('/assets?tab=contributions')
    } catch (e) {
      toast.error(assetErrorMessage(e, t('contribImport.importError')))
    } finally {
      setImporting(false)
    }
  }

  const matched = preview?.matched ?? []
  const skipped = preview?.skipped ?? []
  const warnings = preview?.warnings ?? []
  const duplicates = matched.filter((row) => row.duplicate).length

  return (
    <div className="space-y-6">
      {canWrite && (
        <>
          <div className="flex flex-col gap-2 sm:flex-row sm:items-center sm:gap-4">
            <Label htmlFor="contrib-import-wallet" className="shrink-0 whitespace-nowrap text-sm text-muted-foreground">
              {t('contribImport.importTo')}
            </Label>
            <select
              id="contrib-import-wallet"
              className={SELECT_CLASS}
              value={groupId}
              onChange={(e) => handleWalletChange(e.target.value)}
            >
              <option value="">{t('contribImport.pickWallet')}</option>
              {(wallets ?? []).map((w) => (
                <option key={w.id} value={w.id}>{w.name}</option>
              ))}
            </select>

            <Label htmlFor="contrib-import-date-format" className="shrink-0 whitespace-nowrap text-sm text-muted-foreground">
              {t('import.dateFormat')}
            </Label>
            <select
              id="contrib-import-date-format"
              className={SELECT_CLASS}
              value={dateFormat}
              onChange={(e) => handleDateFormatChange(e.target.value)}
            >
              <option value="">{t('import.dateFormatAuto')}</option>
              <option value="DD/MM/YYYY">DD/MM/YYYY</option>
              <option value="MM/DD/YYYY">MM/DD/YYYY</option>
              <option value="YYYY-MM-DD">YYYY-MM-DD</option>
            </select>
          </div>

          {accountChoices.length > 1 && (
            <div className="flex flex-col gap-2 sm:flex-row sm:items-center sm:gap-4">
              <Label htmlFor="contrib-import-account" className="shrink-0 whitespace-nowrap text-sm text-muted-foreground">
                {t('contribImport.account')}
              </Label>
              <select
                id="contrib-import-account"
                className={SELECT_CLASS}
                value={account}
                onChange={(e) => handleAccountChange(e.target.value)}
              >
                <option value="">{t('contribImport.pickAccount')}</option>
                {accountChoices.map((name) => (
                  <option key={name} value={name}>{name}</option>
                ))}
              </select>
              <p className="text-[11px] text-muted-foreground">{t('contribImport.accountHint')}</p>
            </div>
          )}

          <div
            className={`cursor-pointer rounded-xl border-2 border-dashed bg-card transition-all ${
              dragOver ? 'border-primary bg-primary/5' : 'border-border hover:border-border'
            }`}
            onDragOver={(e) => { e.preventDefault(); setDragOver(true) }}
            onDragLeave={() => setDragOver(false)}
            onDrop={(e) => {
              e.preventDefault()
              setDragOver(false)
              const dropped = e.dataTransfer.files?.[0]
              if (dropped) handleFile(dropped)
            }}
            onClick={() => !loading && fileInputRef.current?.click()}
          >
            <input
              ref={fileInputRef}
              type="file"
              accept=".csv,text/csv"
              className="hidden"
              onChange={(e) => handleFile(e.target.files?.[0] ?? null)}
            />
            <div className="flex flex-col items-center justify-center px-6 py-12 text-center">
              {loading ? (
                <>
                  <div className="mb-4 flex h-12 w-12 animate-pulse items-center justify-center rounded-full bg-primary/10">
                    <FileText size={22} className="text-primary" />
                  </div>
                  <p className="text-sm font-semibold text-foreground">{t('contribImport.reading')}</p>
                  <p className="mt-1 text-xs text-muted-foreground">{file?.name}</p>
                </>
              ) : file && preview ? (
                <>
                  <div className="mb-4 flex h-12 w-12 items-center justify-center rounded-full bg-emerald-100">
                    <CheckCircle2 size={22} className="text-emerald-500" />
                  </div>
                  <p className="text-sm font-semibold text-foreground">{file.name}</p>
                  <p className="mt-1 text-xs text-muted-foreground">
                    {t('contribImport.summaryRows', { matched: matched.length, total: preview.total_rows })}
                  </p>
                  <button
                    className="mt-3 flex items-center gap-1 text-xs text-muted-foreground transition-colors hover:text-rose-500"
                    onClick={(e) => { e.stopPropagation(); handleReset() }}
                  >
                    <X size={12} /> {t('import.removeFile')}
                  </button>
                </>
              ) : (
                <>
                  <div className="mb-4 flex h-12 w-12 items-center justify-center rounded-full bg-muted">
                    <Upload size={22} className="text-muted-foreground" />
                  </div>
                  <p className="mb-1 text-sm font-semibold text-foreground">{t('import.dragOrClick')}</p>
                  <p className="text-xs text-muted-foreground">
                    {groupId ? t('contribImport.chooseHint') : t('contribImport.pickWalletFirst')}
                  </p>
                </>
              )}
            </div>
          </div>
        </>
      )}

      {preview && (
        <div className="overflow-hidden rounded-xl border border-border bg-card shadow-sm">
          <div className="flex flex-wrap items-center gap-x-4 gap-y-1 border-b border-border px-4 py-4 text-sm sm:px-5">
            <span className="font-semibold text-foreground">
              {t('contribImport.summaryMatched', { count: matched.length })}
            </span>
            {duplicates > 0 && (
              <span className="flex items-center gap-1 text-xs text-muted-foreground">
                <Copy size={12} />
                {t('contribImport.summaryDuplicates', { count: duplicates })}
              </span>
            )}
            {skipped.length > 0 && (
              <span className="text-xs text-muted-foreground">
                {t('contribImport.summarySkipped', { count: skipped.length })}
              </span>
            )}
          </div>

          {warnings.length > 0 && (
            <div className="border-b border-border bg-amber-500/10 px-4 py-3 sm:px-5">
              <p className="mb-2 flex items-center gap-2 text-sm font-medium text-amber-700 dark:text-amber-400">
                <AlertTriangle size={14} />
                {t('contribImport.warningsTitle', { count: warnings.length })}
              </p>
              <ul className="space-y-1 text-xs text-muted-foreground">
                {warnings.map((warning, i) => <li key={i}>{warning}</li>)}
              </ul>
            </div>
          )}

          {/* A skip is a row read perfectly that creates nothing, so it is
              neutral here — amber is reserved for what went wrong. */}
          {skipped.length > 0 && (
            <div className="border-b border-border bg-muted/40 px-4 py-3 sm:px-5">
              <p className="mb-2 flex items-center gap-2 text-sm font-medium text-muted-foreground">
                <Info size={14} />
                {t('contribImport.skippedTitle', { count: skipped.length })}
              </p>
              <ul className="space-y-1 text-xs text-muted-foreground">
                {skipped.slice(0, 8).map((row, i) => (
                  <li key={`${row.row_number}-${i}`}>
                    {t('contribImport.skipRow', { row: row.row_number, action: row.action, reason: row.reason })}
                  </li>
                ))}
                {skipped.length > 8 && (
                  <li>{t('contribImport.moreRows', { count: skipped.length - 8 })}</li>
                )}
              </ul>
            </div>
          )}

          <div className="px-4 py-3 sm:px-5">
            <div
              className="grid items-center gap-2 py-1.5 text-[10px] font-semibold uppercase tracking-wider text-muted-foreground"
              style={{ gridTemplateColumns: ROWS_GRID }}
            >
              <div>{t('contribImport.colRow')}</div>
              <div>{t('contribImport.colDate')}</div>
              <div className="text-right">{t('contribImport.colTaxYear')}</div>
              <div>{t('contribImport.colKind')}</div>
              <div>{t('contribImport.colParty')}</div>
              <div className="text-right">{t('contribImport.colAmount')}</div>
              <div>{t('contribImport.colAction')}</div>
            </div>
            {matched.slice(0, 50).map((row) => (
              <div
                key={row.row_number}
                className="grid items-center gap-2 border-t border-border/50 py-1.5 text-xs"
                style={{ gridTemplateColumns: ROWS_GRID }}
              >
                <div className="tabular-nums text-muted-foreground">{row.row_number}</div>
                <div className="text-foreground">{row.date ?? DASH}</div>
                <div className="text-right tabular-nums text-muted-foreground">{row.tax_year ?? DASH}</div>
                <div className="text-muted-foreground">
                  {row.kind ? t(`assets.contribKind_${row.kind}`) : DASH}
                </div>
                <div className="text-muted-foreground">{t(`assets.contribParty_${row.party}`)}</div>
                <div className="text-right tabular-nums text-foreground">{row.amount ?? DASH}</div>
                <div className="flex items-center gap-1 text-muted-foreground">
                  <span className="truncate">{row.action}</span>
                  {row.duplicate && (
                    <span className="shrink-0 rounded bg-muted px-1 text-[10px] text-muted-foreground">
                      {t('contribImport.duplicateBadge')}
                    </span>
                  )}
                </div>
              </div>
            ))}
            {matched.length > 50 && (
              <p className="pt-2 text-xs text-muted-foreground">
                {t('contribImport.moreRows', { count: matched.length - 50 })}
              </p>
            )}
            {matched.length === 0 && (
              <p className="py-4 text-xs italic text-muted-foreground">{t('contribImport.noMatches')}</p>
            )}
          </div>

          {canWrite && (
            <div className="flex justify-end border-t border-border px-4 py-4 sm:px-5">
              <Button onClick={handleImport} disabled={importing || matched.length === 0}>
                {importing ? t('common.loading') : t('contribImport.confirm', { count: matched.length })}
              </Button>
            </div>
          )}
        </div>
      )}
    </div>
  )
}

export default ContributionImportPanel
