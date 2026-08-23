import { useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { useNavigate } from 'react-router-dom'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { toast } from 'sonner'
import { AlertCircle, AlertTriangle, CheckCircle2, Download, FileText, Info, Settings2, Upload, X } from 'lucide-react'

import { assets as assetsApi, assetGroups as assetGroupsApi } from '@/lib/api'
import type { AssetImportPreview, AssetImportRowError, AssetImportSkip, AssetOrderImport } from '@/types'
import { ImportHistory } from '@/components/import-history'
import { Button } from '@/components/ui/button'
import { Label } from '@/components/ui/label'
import { useWorkspace } from '@/contexts/workspace-context'

/**
 * The Securo fields a CSV column can be mapped to; `*` marks the required ones.
 * `price` is not among them: a lot report states a total cost basis instead,
 * and the server accepts either.
 */
const MAPPABLE_FIELDS = [
  { key: 'ticker', required: true },
  { key: 'date', required: true },
  { key: 'quantity', required: true },
  { key: 'price', required: false },
  { key: 'fee', required: false },
  { key: 'kind', required: false },
  { key: 'currency', required: false },
  { key: 'notes', required: false },
  // The lot shape: a report that gives a whole lot on one line, with a total
  // cost rather than a unit price, and the sale on the same row as the buy.
  { key: 'cost_basis', required: false },
  { key: 'date_sold', required: false },
  { key: 'proceeds', required: false },
] as const

const SELECT_CLASS =
  'border border-border rounded-md px-3 py-2 text-sm bg-card focus:outline-none focus-visible:ring-ring/30 focus-visible:ring-[2px]'

/**
 * The rows a file will not turn into orders, listed by line number.
 *
 * Two kinds share this shape and must not share a colour. An *error* is a row
 * the importer could not read; a *skip* is a row it read perfectly and that
 * creates nothing — a transfer between the user's own wallets, a line already
 * on the ledger. A file of transfers rendered in amber reads as a file of
 * mistakes, so the tone is the whole point of the distinction.
 */
function RowNotice({
  rows,
  titleKey,
  reasonPrefix,
  tone,
}: {
  rows: (AssetImportRowError | AssetImportSkip)[]
  titleKey: string
  reasonPrefix: 'reason' | 'skip'
  tone: 'error' | 'neutral'
}) {
  const { t } = useTranslation()
  if (rows.length === 0) return null

  const isError = tone === 'error'
  return (
    <div
      className={`border-b border-border px-4 py-3 sm:px-5 ${isError ? 'bg-amber-500/10' : 'bg-muted/40'}`}
    >
      <p
        className={`mb-2 flex items-center gap-2 text-sm font-medium ${
          isError ? 'text-amber-700 dark:text-amber-400' : 'text-muted-foreground'
        }`}
      >
        {isError ? <AlertTriangle size={14} /> : <Info size={14} />}
        {t(titleKey, { count: rows.length })}
      </p>
      <ul className="space-y-1 text-xs text-muted-foreground">
        {rows.slice(0, 8).map((row, i) => (
          <li key={`${row.row}-${row.reason}-${i}`}>
            {t('assetImport.rowError', {
              row: row.row,
              ticker: row.ticker ?? '—',
              reason: t(`assetImport.${reasonPrefix}.${row.reason}`, row.reason),
            })}
            {row.detail ? ` (${row.detail})` : ''}
          </li>
        ))}
        {rows.length > 8 && <li>{t('assetImport.moreErrors', { count: rows.length - 8 })}</li>}
      </ul>
    </div>
  )
}

/**
 * The investments half of the import page.
 *
 * Deliberately built from the same pieces as the transaction importer: one
 * dashed drop zone that also carries the template link, then a result card
 * whose first strip picks the destination — the wallet here, the account
 * there. Two importers that look different teach the same person two habits.
 */
export function AssetImportPanel() {
  const { t } = useTranslation()
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const { canWrite } = useWorkspace()
  const fileInputRef = useRef<HTMLInputElement | null>(null)

  const [file, setFile] = useState<File | null>(null)
  const [preview, setPreview] = useState<AssetImportPreview | null>(null)
  const [mapping, setMapping] = useState<Record<string, string>>({})
  const [groupId, setGroupId] = useState<string>('')
  const [loading, setLoading] = useState(false)
  const [importing, setImporting] = useState(false)
  const [dragOver, setDragOver] = useState(false)
  const [allowUnpriced, setAllowUnpriced] = useState(false)

  const { data: wallets } = useQuery({
    queryKey: ['asset-groups'],
    queryFn: assetGroupsApi.list,
  })

  async function runPreview(
    selected: File,
    nextMapping: Record<string, string>,
    nextGroup: string,
    nextAllowUnpriced = allowUnpriced,
  ) {
    setLoading(true)
    try {
      const result = await assetsApi.previewImport(selected, {
        column_mapping: nextMapping,
        group_id: nextGroup || null,
        allow_unpriced: nextAllowUnpriced,
      })
      setPreview(result)
    } catch {
      toast.error(t('assetImport.previewError'))
      setPreview(null)
    } finally {
      setLoading(false)
    }
  }

  function handleFile(selected: File | null) {
    setFile(selected)
    setPreview(null)
    setMapping({})
    if (selected) runPreview(selected, {}, groupId)
  }

  function handleReset() {
    handleFile(null)
    if (fileInputRef.current) fileInputRef.current.value = ''
  }

  function handleDrop(e: React.DragEvent) {
    e.preventDefault()
    setDragOver(false)
    const dropped = e.dataTransfer.files?.[0]
    if (dropped) handleFile(dropped)
  }

  // Re-preview on every change, so the counts on screen always describe the
  // import that would actually run.
  function handleMappingChange(field: string, column: string) {
    const next = { ...mapping, [field]: column }
    if (!column) delete next[field]
    setMapping(next)
    if (file) runPreview(file, next, groupId)
  }

  function handleWalletChange(value: string) {
    setGroupId(value)
    if (file) runPreview(file, mapping, value)
  }

  function handleAllowUnpricedChange(value: boolean) {
    setAllowUnpriced(value)
    if (file) runPreview(file, mapping, groupId, value)
  }

  async function handleImport() {
    if (!preview || preview.orders.length === 0) return
    setImporting(true)
    try {
      const result = await assetsApi.importOrders(
        preview.orders as AssetOrderImport[],
        groupId || null,
        file?.name,
        allowUnpriced,
      )
      queryClient.invalidateQueries({ queryKey: ['assets'] })
      queryClient.invalidateQueries({ queryKey: ['asset-groups'] })
      queryClient.invalidateQueries({ queryKey: ['import-logs'] })
      toast.success(t('assetImport.imported', { count: result.imported }))
      navigate('/assets')
    } catch {
      toast.error(t('assetImport.importError'))
    } finally {
      setImporting(false)
    }
  }

  const importable = preview?.orders.length ?? 0
  const rowErrors = preview?.errors ?? []
  const rowSkips = preview?.skips ?? []
  const walletWarnings = preview?.warnings ?? []
  const needsMapping = !!preview?.parse_error

  return (
    <div className="space-y-6">
      {canWrite && (
        <div
          className={`cursor-pointer rounded-xl border-2 border-dashed bg-card transition-all ${
            dragOver ? 'border-primary bg-primary/5' : 'border-border hover:border-border'
          }`}
          onDragOver={(e) => { e.preventDefault(); setDragOver(true) }}
          onDragLeave={() => setDragOver(false)}
          onDrop={handleDrop}
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
                <p className="text-sm font-semibold text-foreground">{t('assetImport.reading')}</p>
                <p className="mt-1 text-xs text-muted-foreground">{file?.name}</p>
              </>
            ) : file && preview && !needsMapping ? (
              <>
                <div className="mb-4 flex h-12 w-12 items-center justify-center rounded-full bg-emerald-100">
                  <CheckCircle2 size={22} className="text-emerald-500" />
                </div>
                <p className="text-sm font-semibold text-foreground">{file.name}</p>
                <p className="mt-1 text-xs text-muted-foreground">
                  {t('assetImport.summaryOrders', { count: importable })}
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
                <p className="text-xs text-muted-foreground">{t('assetImport.chooseHint')}</p>
                <button
                  className="mt-2 flex items-center gap-1 text-xs text-primary transition-colors hover:text-primary/80"
                  onClick={(e) => { e.stopPropagation(); assetsApi.importTemplate() }}
                >
                  <Download size={12} />
                  {t('assetImport.downloadTemplate')}
                </button>
              </>
            )}
          </div>
        </div>
      )}

      {needsMapping && (
        <div className="overflow-hidden rounded-xl border border-border bg-card shadow-sm">
          <div className="flex items-center gap-2 border-b border-border bg-muted/30 px-5 py-4">
            <Settings2 size={14} className="text-muted-foreground" />
            <p className="text-xs font-medium text-muted-foreground">{t('assetImport.mapPrompt')}</p>
          </div>
          <div className="grid gap-3 p-5 sm:grid-cols-2">
            {MAPPABLE_FIELDS.map(({ key, required }) => (
              <div key={key} className="grid gap-1">
                <Label htmlFor={`map-${key}`} className="text-xs">
                  {t(`assetImport.field.${key}`)}{required ? ' *' : ''}
                </Label>
                <select
                  id={`map-${key}`}
                  className={SELECT_CLASS}
                  value={mapping[key] ?? ''}
                  onChange={(e) => handleMappingChange(key, e.target.value)}
                >
                  <option value="">{t('assetImport.ignoreColumn')}</option>
                  {(preview?.csv_columns ?? []).map((col) => (
                    <option key={col} value={col}>{col}</option>
                  ))}
                </select>
              </div>
            ))}
          </div>
        </div>
      )}

      {preview && !needsMapping && (
        <div className="overflow-hidden rounded-xl border border-border bg-card shadow-sm">
          <div className="border-b border-border px-4 py-4 sm:px-5">
            <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-sm">
              <span className="font-semibold text-foreground">
                {t('assetImport.summaryOrders', { count: importable })}
              </span>
              <span className="text-xs text-muted-foreground">
                {t('assetImport.summaryHoldings', {
                  created: preview.holdings_created,
                  matched: preview.holdings_matched,
                })}
              </span>
              {preview.skipped > 0 && (
                <span className="text-xs text-muted-foreground">
                  {t('assetImport.summarySkipped', { count: preview.skipped })}
                </span>
              )}
            </div>
          </div>

          {/* Destination strip, in the place the account picker holds on the
              transactions side. */}
          <div className="border-b border-border bg-muted/50 px-4 py-4 sm:px-5">
            <div className="flex flex-col gap-2 sm:flex-row sm:items-center sm:gap-4">
              <Label htmlFor="asset-import-wallet" className="shrink-0 whitespace-nowrap text-sm text-muted-foreground">
                {t('assetImport.importTo')}
              </Label>
              <select
                id="asset-import-wallet"
                className={`flex-1 ${SELECT_CLASS}`}
                value={groupId}
                onChange={(e) => handleWalletChange(e.target.value)}
              >
                <option value="">{t('assetImport.noWallet')}</option>
                {(wallets ?? []).map((w) => (
                  <option key={w.id} value={w.id}>{w.name}</option>
                ))}
              </select>
            </div>

            {/* A delisted stock, or a token an insolvency estate handed out:
                real basis, no market. Off by default because an unrecognised
                ticker is nearly always a typo. */}
            <label className="mt-3 flex items-start gap-2 text-xs text-muted-foreground">
              <input
                type="checkbox"
                className="mt-0.5"
                checked={allowUnpriced}
                onChange={(e) => handleAllowUnpricedChange(e.target.checked)}
              />
              <span>
                <span className="font-medium text-foreground">{t('assetImport.allowUnpriced')}</span>
                <br />
                {t('assetImport.allowUnpricedHint')}
              </span>
            </label>
          </div>

          {walletWarnings.length > 0 && (
            <div className="border-b border-border bg-blue-50 px-4 py-3 dark:bg-blue-950 sm:px-5">
              <p className="mb-2 flex items-center gap-2 text-sm font-medium text-blue-700 dark:text-blue-300">
                <Info size={14} />
                {t('assetImport.walletWarningTitle')}
              </p>
              <ul className="space-y-1 text-xs text-blue-600 dark:text-blue-300/80">
                {walletWarnings.map((w) => (
                  <li key={`${w.ticker}-${w.reason}`}>
                    {t(`assetImport.warning.${w.reason}`, {
                      ticker: w.ticker,
                      wallet: w.wallet ?? '—',
                      imported: w.imported_units ?? '—',
                      reported: w.reported_units ?? '—',
                    })}
                  </li>
                ))}
              </ul>
            </div>
          )}

          <RowNotice
            rows={rowErrors}
            titleKey="assetImport.rowsRefused"
            reasonPrefix="reason"
            tone="error"
          />

          {/* Not an error: the row is fine and creates nothing. Kept apart
              from the amber block so a file of transfers does not read as a
              file of mistakes. */}
          <RowNotice
            rows={rowSkips}
            titleKey="assetImport.rowsNotImported"
            reasonPrefix="skip"
            tone="neutral"
          />

          {importable > 0 && (
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead className="bg-muted/40 text-xs uppercase text-muted-foreground">
                  <tr>
                    <th className="px-3 py-2 text-left sm:px-5">{t('assetImport.field.ticker')}</th>
                    <th className="px-3 py-2 text-left">{t('assetImport.field.date')}</th>
                    <th className="px-3 py-2 text-left">{t('assetImport.field.kind')}</th>
                    <th className="px-3 py-2 text-right">{t('assetImport.field.quantity')}</th>
                    <th className="px-3 py-2 text-right">{t('assetImport.field.price')}</th>
                    <th className="px-3 py-2 text-right sm:px-5">{t('assetImport.field.fee')}</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-border">
                  {preview.orders.slice(0, 50).map((order) => (
                    <tr key={order.row}>
                      <td className="px-3 py-1.5 font-medium sm:px-5">{order.ticker}</td>
                      <td className="px-3 py-1.5">{order.date}</td>
                      <td className="px-3 py-1.5">
                        <span className={order.kind === 'sell' ? 'text-rose-500' : 'text-emerald-500'}>
                          {t(`assetImport.kind.${order.kind}`)}
                        </span>
                      </td>
                      <td className="px-3 py-1.5 text-right tabular-nums">{order.quantity}</td>
                      <td className="px-3 py-1.5 text-right tabular-nums">{order.price}</td>
                      <td className="px-3 py-1.5 text-right tabular-nums sm:px-5">{order.fee}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
              {preview.orders.length > 50 && (
                <p className="border-t border-border px-4 py-2 text-xs text-muted-foreground sm:px-5">
                  {t('assetImport.moreRows', { count: preview.orders.length - 50 })}
                </p>
              )}
            </div>
          )}

          <div className="flex items-center justify-between gap-3 border-t border-border px-4 py-4 sm:px-5">
            {importable === 0 ? (
              <span className="flex items-center gap-1.5 text-xs text-amber-600">
                <AlertCircle size={12} />
                {t('assetImport.nothingToImport')}
              </span>
            ) : (
              <span />
            )}
            <div className="flex gap-2">
              <Button variant="outline" onClick={handleReset}>
                <X size={14} className="mr-1" />
                {t('common.cancel')}
              </Button>
              <Button onClick={handleImport} disabled={importing || importable === 0} className="gap-2">
                <Upload size={14} />
                {importing ? t('assetImport.importing') : t('assetImport.confirm', { count: importable })}
              </Button>
            </div>
          </div>
        </div>
      )}

      <ImportHistory entity="asset_orders" />
    </div>
  )
}
