import { act, screen } from '@testing-library/react'
import { beforeEach, expect, it, vi } from 'vitest'
import { AssetImportPanel } from './asset-import-panel'
import { renderWithProviders } from '@/test/utils'

const { previewImport } = vi.hoisted(() => ({ previewImport: vi.fn() }))
vi.mock('@/lib/api', () => ({ assets: { previewImport }, assetGroups: { list: async () => [{ id: 'wallet-a', name: 'Synthetic wallet' }] }, assetErrorMessage: (_: unknown, fallback: string) => fallback }))
vi.mock('@/contexts/workspace-context', () => ({ useWorkspace: () => ({ canWrite: true }) }))
vi.mock('@/components/import-history', () => ({ ImportHistory: () => null }))

const preview = (ticker: string) => ({ orders: [{ row: 2, ticker, date: '2026-01-01', kind: 'buy', quantity: '12', price: '7', fee: '0' }], errors: [], skips: [], warnings: [], csv_columns: [], parse_error: null, holdings_created: 1, holdings_matched: 0, skipped: 0 })
beforeEach(() => { previewImport.mockReset() })

it('keeps the newest file preview when requests complete out of order', async () => {
  let first!: (result: unknown) => void
  previewImport.mockImplementationOnce(() => new Promise((resolve) => { first = resolve })).mockResolvedValueOnce(preview('NEW'))
  const { user, container } = renderWithProviders(<AssetImportPanel />)
  const upload = container.querySelector<HTMLInputElement>('input[type=file]')!
  await user.upload(upload, new File(['first'], 'first.csv', { type: 'text/csv' }))
  await user.upload(upload, new File(['second'], 'second.csv', { type: 'text/csv' }))
  await screen.findByText('NEW')
  await act(async () => { first(preview('OLD')) })
  expect(screen.queryByText('OLD')).not.toBeInTheDocument()
  expect(screen.getByText('NEW')).toBeInTheDocument()
})

it('does not restore a removed file after its preview completes', async () => {
  let finish!: (result: unknown) => void
  previewImport.mockImplementation(() => new Promise((resolve) => { finish = resolve }))
  const { user, container } = renderWithProviders(<AssetImportPanel />)
  await user.upload(container.querySelector<HTMLInputElement>('input[type=file]')!, new File(['first'], 'first.csv', { type: 'text/csv' }))
  await user.click(screen.getByRole('button', { name: 'Remove file' }))
  await act(async () => { finish(preview('REMOVED')) })
  expect(screen.queryByText('REMOVED')).not.toBeInTheDocument()
})
