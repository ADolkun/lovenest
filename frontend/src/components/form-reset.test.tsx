import { act, fireEvent, screen, waitFor, within } from '@testing-library/react'
import { expect, it, vi } from 'vitest'
import { TransferDialog } from './transfer-dialog'
import { TransactionSplitsSection } from './transaction-splits-section'
import { renderWithProviders } from '@/test/utils'
import type { Account, Group, TransactionSplitsInput } from '@/types'

const api = vi.hoisted(() => ({ list: vi.fn(), get: vi.fn() }))
vi.mock('@/lib/api', () => ({ groups: api }))
vi.mock('@/contexts/auth-context', () => ({ useAuth: () => ({ user: null }) }))

it('preserves a transfer draft on rerender and resets all amounts and accounts on reopen', () => {
  const accounts = [
    { id: 'a', name: 'Source', type: 'checking', currency: 'USD' },
    { id: 'b', name: 'Destination', type: 'checking', currency: 'EUR' },
  ] as Account[]
  const props = { accounts, onClose: vi.fn(), onSave: vi.fn(), loading: false, defaultFromAccountId: 'a' }
  const { rerender } = renderWithProviders(<TransferDialog {...props} open />)
  fireEvent.change(screen.getAllByRole('combobox')[1], { target: { value: 'b' } })
  const amounts = screen.getAllByRole('spinbutton')
  fireEvent.change(amounts[0], { target: { value: '23' } })
  fireEvent.change(amounts[1], { target: { value: '21' } })
  rerender(<TransferDialog {...props} open />)
  expect(screen.getAllByRole('spinbutton')[0]).toHaveValue(23)
  expect(screen.getAllByRole('spinbutton')[1]).toHaveValue(21)
  rerender(<TransferDialog {...props} open={false} />)
  rerender(<TransferDialog {...props} open />)
  expect(screen.getAllByRole('combobox')[0]).toHaveValue('a')
  expect(screen.getAllByRole('combobox')[1]).toHaveValue('')
  expect(screen.getAllByRole('spinbutton')).toHaveLength(1)
  expect(screen.getByRole('spinbutton')).toHaveValue(null)
})

it('hydrates seeded zero-valued splits after loading and preserves edits when members refresh', async () => {
  const group = { id: 'g', name: 'Shared', members: [{ id: 'm1', name: 'Alex' }, { id: 'm2', name: 'Sam' }] } as Group
  api.list.mockResolvedValue([group])
  let resolveGroup!: (group: Group) => void
  api.get.mockImplementation(() => new Promise<Group>((resolve) => { resolveGroup = resolve }))
  const value: TransactionSplitsInput = {
    share_type: 'exact',
    splits: [{ group_member_id: 'm1', share_amount: 0 }, { group_member_id: 'm2', share_amount: 10 }],
  }
  const onChange = vi.fn()
  const { queryClient } = renderWithProviders(<TransactionSplitsSection amount={10} currency="USD" value={value} onChange={onChange} />)
  await waitFor(() => expect(api.get).toHaveBeenCalledWith('g'))
  await act(async () => resolveGroup(group))
  await waitFor(() => expect(onChange).toHaveBeenLastCalledWith(value))
  const alex = screen.getByRole('checkbox', { name: 'Alex' }).closest('div')!
  expect(within(alex).getByRole('spinbutton')).toHaveValue(0)
  fireEvent.change(within(alex).getByRole('spinbutton'), { target: { value: '3' } })
  act(() => queryClient.setQueryData(['groups', 'g'], { ...group, members: [...group.members, { id: 'm3', name: 'Lee' }] }))
  expect(within(screen.getByRole('checkbox', { name: 'Alex' }).closest('div')!).getByRole('spinbutton')).toHaveValue(3)
  expect(await screen.findByRole('checkbox', { name: 'Lee' })).not.toBeChecked()
  expect(onChange).toHaveBeenLastCalledWith({ ...value, splits: [{ group_member_id: 'm1', share_amount: 3 }, { group_member_id: 'm2', share_amount: 10 }] })
})
