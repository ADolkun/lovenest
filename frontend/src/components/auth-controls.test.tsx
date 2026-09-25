/**
 * A server can switch to OIDC-only after users have already enrolled TOTP or a
 * passkey. There are no recovery codes, so if the menu hides those entries the
 * enrolled factor becomes unremovable. These tests assert the removal paths stay
 * reachable on both menu surfaces while enrollment stays hidden.
 */
import { act, screen, waitFor, within } from '@testing-library/react'
import { beforeEach, expect, it, vi } from 'vitest'
import { AppLayout } from './app-layout'
import { TwoFactorSetup } from './two-factor-setup'
import { PasskeyManagementDialog } from './passkey-management-dialog'
import { renderWithProviders, t, i18n } from '@/test/utils'

const state = vi.hoisted(() => ({
  user: {
    email: 'synthetic@example.com',
    is_2fa_enabled: true,
    is_superuser: false,
    preferences: { onboarding_completed: true },
  },
  workspace: { id: 'synthetic', name: 'Synthetic workspace', kind: 'personal', role: 'owner' },
  auth: {
    oidcConfig: vi.fn(),
    listPasskeys: vi.fn(),
    deletePasskey: vi.fn(),
    disable2fa: vi.fn(),
    setup2fa: vi.fn(),
    enable2fa: vi.fn(),
    registerPasskeyOptions: vi.fn(),
    verifyPasskeyRegistration: vi.fn(),
  },
  toastError: vi.fn(),
  blocker: vi.fn(),
}))

vi.mock('@/contexts/auth-context', () => ({
  useAuth: () => ({
    user: state.user,
    logout: vi.fn(),
    updateUser: (user: typeof state.user) => {
      state.user = user
    },
  }),
}))
vi.mock('@/contexts/workspace-context', () => ({
  useWorkspace: () => ({
    current: state.workspace,
    workspaces: [state.workspace],
    switchWorkspace: vi.fn(),
    refresh: vi.fn(),
    hasModule: () => false,
    canWrite: false,
    isLoading: false,
  }),
}))
vi.mock('@/contexts/collection-filter-context', () => ({
  useCollectionFilter: () => ({ activeAccountIds: null }),
}))
vi.mock('@/hooks/use-feature-flags', () => ({
  useFeatureFlags: () => ({ isLoading: false, agentsEnabled: false }),
}))
vi.mock('@/lib/api', async (original) => ({
  ...await original<typeof import('@/lib/api')>(),
  auth: state.auth,
  admin: {
    defaultColors: async () => ({ light: null, dark: null }),
    numberFormat: async () => ({ format: 'auto' }),
  },
  accounts: { list: async () => [] },
  workspaces: { create: vi.fn() },
  info: { get: async () => ({ features: {} }) },
}))
vi.mock('@/lib/webauthn', () => ({
  passkeyBlocker: state.blocker,
  passkeyFailure: () => 'unknown',
  startPasskeyRegistration: vi.fn(),
}))
vi.mock('sonner', () => ({ toast: { success: vi.fn(), error: state.toastError } }))
vi.mock('next-themes', () => ({ useTheme: () => ({ theme: 'light', setTheme: vi.fn() }) }))
vi.mock('@/components/collection-selector', () => ({ CollectionSelector: () => null }))
vi.mock('@/components/command-palette', () => ({ CommandPalette: () => null }))
vi.mock('@/components/global-chat-panel', () => ({ GlobalChatPanel: () => null }))
vi.mock('@/components/update-available-banner', () => ({ UpdateAvailableBanner: () => null }))
vi.mock('@/components/update-available-dialog', () => ({ UpdateAvailableDialog: () => null }))
vi.mock('@/components/backup-dialog', () => ({ BackupDialog: () => null }))

/** The sidebar switcher (desktop) and the header avatar (mobile) are separate menus. */
function openMenu(surface: string) {
  return surface === 'mobile'
    ? screen.getByRole('button', { name: t('common.userMenu') })
    : screen.getByRole('button', { name: /Synthetic workspace/ })
}

beforeEach(() => {
  vi.clearAllMocks()
  localStorage.removeItem('securo.sidebar.collapsed')
  state.user = { ...state.user, is_2fa_enabled: true }
  state.auth.oidcConfig.mockResolvedValue({
    enabled: true,
    local_auth_enabled: false,
    provider_name: 'SSO',
  })
  state.auth.listPasskeys.mockResolvedValue([
    { id: 'synthetic-key', name: 'Saved key', created_at: '2026-01-01T00:00:00Z', last_used_at: null },
  ])
  state.auth.disable2fa.mockResolvedValue({})
  state.auth.deletePasskey.mockResolvedValue(undefined)
  // An unsupported browser must not surface the registration warning in cleanup mode.
  state.blocker.mockReturnValue('unsupported')
})

it('collapses the desktop sidebar and persists the preference', async () => {
  const { user } = renderWithProviders(<AppLayout />)

  const toggle = screen.getByRole('button', { name: t('nav.collapseSidebar') })
  expect(toggle).toHaveAttribute('aria-expanded', 'true')

  await user.click(toggle)

  expect(localStorage.getItem('securo.sidebar.collapsed')).toBe('true')
  expect(screen.getByRole('button', { name: t('nav.expandSidebar') })).toHaveAttribute('aria-expanded', 'false')
  expect(document.querySelector('aside')).toHaveAttribute('data-collapsed', 'true')
  expect(document.querySelector('main')).toHaveClass('lg:ml-16')

  await user.click(screen.getByRole('button', { name: /Synthetic workspace/ }))
  expect(screen.getByRole('menuitem', { name: /Workspace settings/ })).toBeInTheDocument()
})

it('restores a collapsed desktop sidebar from local storage', () => {
  localStorage.setItem('securo.sidebar.collapsed', 'true')

  renderWithProviders(<AppLayout />)

  expect(screen.getByRole('button', { name: t('nav.expandSidebar') })).toHaveAttribute('aria-expanded', 'false')
  expect(document.querySelector('main')).toHaveClass('lg:ml-16')
})

it.each(['desktop', 'mobile'])(
  'keeps 2FA and passkey removal reachable from the %s menu in OIDC-only mode',
  async (surface) => {
    const { user } = renderWithProviders(<AppLayout />)
    await waitFor(() => expect(state.auth.oidcConfig).toHaveBeenCalled())

    await user.click(openMenu(surface))
    expect(
      screen.queryByRole('menuitem', { name: t('auth.changePassword') }),
    ).not.toBeInTheDocument()

    await user.click(screen.getByRole('menuitem', { name: t('auth.disable2fa') }))
    const twoFactorDialog = screen.getByRole('dialog')
    await user.type(within(twoFactorDialog).getByLabelText(t('auth.password')), 'synthetic-password')
    await user.type(within(twoFactorDialog).getByLabelText(t('auth.twoFactor')), '123456')
    await user.click(within(twoFactorDialog).getByRole('button', { name: t('auth.disable2fa') }))
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
    expect(state.auth.disable2fa).toHaveBeenCalledWith('synthetic-password', '123456')

    // The entry retires with the factor it removed, and enrollment never replaces it.
    await user.click(openMenu(surface))
    expect(screen.queryByRole('menuitem', { name: /2FA|two.factor/i })).not.toBeInTheDocument()

    await user.click(screen.getByRole('menuitem', { name: t('auth.passkeysTitle') }))
    await screen.findByText('Saved key')
    expect(screen.getByText(t('auth.passkeysCleanupDescription'))).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: t('auth.addPasskey') })).not.toBeInTheDocument()
    expect(screen.queryByText(t('auth.passkeyUnsupported'))).not.toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: t('auth.deletePasskey') }))
    await user.click(screen.getByRole('button', { name: t('common.delete') }))
    expect(await screen.findByText(t('auth.noPasskeys'))).toBeInTheDocument()
    expect(state.auth.deletePasskey).toHaveBeenCalledWith('synthetic-key')

    expect(state.auth.setup2fa).not.toHaveBeenCalled()
    expect(state.auth.enable2fa).not.toHaveBeenCalled()
    expect(state.auth.registerPasskeyOptions).not.toHaveBeenCalled()
  },
)

it.each(['desktop', 'mobile'])(
  'still offers the full local credential menu on the %s surface',
  async (surface) => {
    state.auth.oidcConfig.mockResolvedValue({ enabled: false, local_auth_enabled: true })
    state.blocker.mockReturnValue(null)
    state.user = { ...state.user, is_2fa_enabled: false }
    const { user } = renderWithProviders(<AppLayout />)
    await waitFor(() => expect(state.auth.oidcConfig).toHaveBeenCalled())

    await user.click(openMenu(surface))
    expect(screen.getByRole('menuitem', { name: t('auth.changePassword') })).toBeInTheDocument()
    expect(screen.getByRole('menuitem', { name: t('auth.twoFactorTitle') })).toBeInTheDocument()

    await user.click(screen.getByRole('menuitem', { name: t('auth.passkeysTitle') }))
    await screen.findByText('Saved key')
    expect(screen.getByRole('button', { name: t('auth.addPasskey') })).toBeInTheDocument()
  },
)

it.each(['loading', 'enabled', 'failed'])('keeps enrollment gated through config %s', async (config) => {
  let finish!: (config: unknown) => void
  if (config === 'loading') state.auth.oidcConfig.mockImplementationOnce(() => new Promise(resolve => { finish = resolve }))
  else if (config === 'failed') state.auth.oidcConfig.mockRejectedValueOnce(new Error('unavailable'))
  else state.auth.oidcConfig.mockResolvedValueOnce({ local_auth_enabled: true })
  state.user = { ...state.user, is_2fa_enabled: false }
  const { user } = renderWithProviders(<AppLayout />)
  await act(async () => {})
  await user.click(screen.getByRole('button', { name: t('common.userMenu') }))
  const changePassword = screen.queryByRole('menuitem', { name: t('auth.changePassword') })
  expect(!!changePassword).toBe(config !== 'loading')
  expect(!!screen.queryByRole('menuitem', { name: t('auth.twoFactorTitle') })).toBe(config !== 'loading')
  await user.click(screen.getByRole('menuitem', { name: t('auth.passkeysTitle') }))
  await screen.findByText('Saved key')
  expect(!!screen.queryByRole('button', { name: t('auth.addPasskey') })).toBe(config !== 'loading')
  if (config === 'loading') await act(async () => { finish({ local_auth_enabled: false }) })
})

it('never offers TOTP enrollment once the enrolled factor is gone', () => {
  state.user = { ...state.user, is_2fa_enabled: false }
  renderWithProviders(<TwoFactorSetup open localAuthEnabled={false} onClose={vi.fn()} />)
  expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
  expect(state.auth.setup2fa).not.toHaveBeenCalled()
})

it.each([
  [{ response: { status: 400, data: { detail: 'Invalid password' } } }, 'auth.currentPasswordWrong'],
  [{ response: { status: 400, data: { detail: 'Invalid 2FA code' } } }, 'auth.invalid2faCode'],
  [{ response: { status: 503 } }, 'auth.serverError'],
  [{ response: { status: 400, data: { detail: '2FA is not set up' } } }, 'common.error'],
])('preserves credentials and explains a failed TOTP removal', async (error, key) => {
  state.auth.disable2fa.mockRejectedValueOnce(error)
  const close = vi.fn()
  const { user } = renderWithProviders(<TwoFactorSetup open localAuthEnabled={false} onClose={close} />)
  await user.type(screen.getByLabelText(t('auth.password')), 'synthetic-password')
  await user.type(screen.getByLabelText(t('auth.twoFactor')), '123456')
  await user.click(screen.getByRole('button', { name: t('auth.disable2fa') }))
  expect(await screen.findByRole('alert')).toHaveTextContent(t(key))
  expect(state.user.is_2fa_enabled).toBe(true)
  expect(close).not.toHaveBeenCalled()
})

it('retries passkey list errors and keeps the key after a failed deletion in cleanup mode', async () => {
  state.auth.listPasskeys.mockRejectedValueOnce(new Error('unavailable'))
  state.auth.deletePasskey.mockRejectedValueOnce(new Error('refused'))
  const { user } = renderWithProviders(<PasskeyManagementDialog open localAuthEnabled={false} onClose={vi.fn()} />)
  expect(await screen.findByRole('alert')).toHaveTextContent(t('auth.passkeyLoadError'))
  expect(screen.queryByText(t('auth.noPasskeys'))).not.toBeInTheDocument()
  await user.click(screen.getByRole('button', { name: t('common.retry') }))
  await screen.findByText('Saved key')
  await user.click(screen.getByRole('button', { name: t('auth.deletePasskey') }))
  await user.click(screen.getByRole('button', { name: t('common.delete') }))
  await waitFor(() => expect(state.toastError).toHaveBeenCalledWith(t('auth.passkeyDeleteError')))
  expect(screen.getByText('Saved key')).toBeInTheDocument()
})

it('ships readable auth guidance in every supported locale', async () => {
  const locales = ['en', 'pt-BR', 'pt-PT', 'de', 'es', 'fr', 'it', 'nl', 'pl', 'ru', 'uk', 'el', 'hi', 'ja', 'sk']
  await i18n.loadLanguages(locales)
  for (const locale of locales) {
    for (const key of ['localAuthDisabled', 'passkeyEmailRequired', 'passkeysCleanupDescription']) {
      expect(i18n.getResource(locale, 'translation', `auth.${key}`)).toEqual(expect.any(String))
      expect(i18n.getFixedT(locale)(`auth.${key}`)).not.toBe(`auth.${key}`)
    }
  }
})
