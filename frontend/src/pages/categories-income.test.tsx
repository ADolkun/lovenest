/**
 * Flagging a category as income on the categories page.
 *
 * The flag decides whether money received in a category counts as income or
 * offsets that category's spending, so the form has to carry it both ways.
 */
import { createContext } from 'react'
import { describe, expect, it, vi, beforeEach } from 'vitest'
import { screen, waitFor, within } from '@testing-library/react'

import CategoriesPage from '@/pages/categories'
import { renderWithProviders, t } from '@/test/utils'

const api = vi.hoisted(() => ({
  categories: {
    listIncludingHidden: vi.fn(),
    usage: vi.fn(),
    delete: vi.fn(),
    create: vi.fn(),
    update: vi.fn(),
    ruleUsage: vi.fn(),
  },
  categoryGroups: {
    listIncludingHidden: vi.fn(),
    create: vi.fn(),
    update: vi.fn(),
    delete: vi.fn(),
  },
}))

vi.mock('@/lib/api', () => ({
  categories: api.categories,
  categoryGroups: api.categoryGroups,
}))

vi.mock('@/contexts/workspace-context', () => ({
  useWorkspace: () => ({ canWrite: true }),
  WorkspaceContext: createContext<{ canWrite: boolean } | undefined>({ canWrite: true }),
}))

function category(id: string, name: string, overrides = {}) {
  return {
    id,
    user_id: 'user-1',
    name,
    icon: 'circle-help',
    color: '#6366f1',
    group_id: null,
    is_system: false,
    is_hidden: false,
    treat_as_transfer: false,
    is_ignored: false,
    is_income: false,
    ...overrides,
  }
}

const DINING = category('dining', 'Dining')
const SALARY = category('salary', 'Salary', { is_income: true })

beforeEach(() => {
  vi.clearAllMocks()
  api.categories.listIncludingHidden.mockResolvedValue([DINING, SALARY])
  api.categoryGroups.listIncludingHidden.mockResolvedValue([])
  api.categories.update.mockImplementation(async (id: string, data: object) => ({
    ...(id === SALARY.id ? SALARY : DINING),
    ...data,
  }))
})

async function rowFor(name: string) {
  return (await screen.findByText(name)).closest('div')!.parentElement!
}

async function openEdit(user: ReturnType<typeof renderWithProviders>['user'], name: string) {
  await user.click(within(await rowFor(name)).getByTitle(t('common.edit')))
  return screen.findByRole('checkbox', { name: new RegExp(t('categories.isIncome')) })
}

describe('income categories', () => {
  it('badges only the categories flagged as income', async () => {
    renderWithProviders(<CategoriesPage />)

    expect(within(await rowFor('Salary')).getByText(t('categories.isIncomeBadge'))).toBeInTheDocument()
    expect(within(await rowFor('Dining')).queryByText(t('categories.isIncomeBadge'))).not.toBeInTheDocument()
  })

  it('saves the flag when it is switched on', async () => {
    const { user } = renderWithProviders(<CategoriesPage />)

    const toggle = await openEdit(user, 'Dining')
    expect(toggle).not.toBeChecked()
    await user.click(toggle)
    await user.click(screen.getByRole('button', { name: t('common.save') }))

    await waitFor(() =>
      expect(api.categories.update).toHaveBeenCalledWith(
        'dining',
        expect.objectContaining({ is_income: true }),
      ),
    )
  })

  it('loads the saved flag and clears it when switched off', async () => {
    const { user } = renderWithProviders(<CategoriesPage />)

    const toggle = await openEdit(user, 'Salary')
    expect(toggle).toBeChecked()
    await user.click(toggle)
    await user.click(screen.getByRole('button', { name: t('common.save') }))

    await waitFor(() =>
      expect(api.categories.update).toHaveBeenCalledWith(
        'salary',
        expect.objectContaining({ is_income: false }),
      ),
    )
  })
})
