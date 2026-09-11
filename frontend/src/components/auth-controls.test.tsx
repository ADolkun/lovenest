import { act, screen, waitFor, within } from '@testing-library/react'
import { beforeEach, expect, it, vi } from 'vitest'
import { AppLayout } from './app-layout'
import { TwoFactorSetup } from './two-factor-setup'
import { PasskeyManagementDialog } from './passkey-management-dialog'
import { renderWithProviders, t, i18n } from '@/test/utils'

const state = vi.hoisted(() => ({
  user: { email: 'synthetic@example.com', is_2fa_enabled: true, preferences: { onboarding_completed: true } },
  workspace: { id: 'synthetic', name: 'Synthetic workspace', kind: 'personal', role: 'owner' },
  auth: { oidcConfig: vi.fn(), listPasskeys: vi.fn(), deletePasskey: vi.fn(), disable2fa: vi.fn(), setup2fa: vi.fn(), enable2fa: vi.fn(), registerPasskeyOptions: vi.fn() },
  toastError: vi.fn(),
  blocker: vi.fn(),
}))
vi.mock('@/contexts/auth-context', () => ({ useAuth: () => ({
  user: state.user, logout: vi.fn(), updateUser: (user: typeof state.user) => { state.user = user },
}) }))
vi.mock('@/contexts/workspace-context', () => ({ useWorkspace: () => ({
  current: state.workspace, workspaces: [state.workspace], hasModule: () => false, canWrite: false,
}) }))
vi.mock('@/contexts/collection-filter-context', () => ({ useCollectionFilter: () => ({ accountIds: null }) }))
vi.mock('@/hooks/use-feature-flags', () => ({ useFeatureFlags: () => ({ agentsEnabled: false }) }))
vi.mock('@/lib/api', async (original) => ({
  ...await original<typeof import('@/lib/api')>(),
  auth: state.auth,
  admin: { defaultColors: async () => ({ light: null, dark: null }) },
  accounts: { list: async () => [] },
}))
vi.mock('@/lib/webauthn', () => ({ passkeyBlocker: state.blocker, passkeyFailure: () => 'unknown', startPasskeyRegistration: vi.fn() }))
vi.mock('sonner', () => ({ toast: { success: vi.fn(), error: state.toastError } }))
vi.mock('next-themes', () => ({ useTheme: () => ({ theme: 'light', setTheme: vi.fn() }) }))
vi.mock('@/components/collection-selector', () => ({ CollectionSelector: () => null }))
vi.mock('@/components/command-palette', () => ({ CommandPalette: () => null }))
vi.mock('@/components/global-chat-panel', () => ({ GlobalChatPanel: () => null }))
vi.mock('@/components/update-available-banner', () => ({ UpdateAvailableBanner: () => null }))
vi.mock('@/components/update-available-dialog', () => ({ UpdateAvailableDialog: () => null }))
vi.mock('@/components/backup-dialog', () => ({ BackupDialog: () => null }))

beforeEach(() => {
  vi.clearAllMocks()
  state.user.is_2fa_enabled = true
  state.auth.oidcConfig.mockResolvedValue({ enabled: true, local_auth_enabled: false, provider_name: 'SSO' })
  state.auth.listPasskeys.mockResolvedValue([{ id: 'synthetic-key', name: 'Saved key', created_at: '2026-01-01T00:00:00Z', last_used_at: null }])
  state.auth.disable2fa.mockResolvedValue({})
  state.auth.deletePasskey.mockResolvedValue(undefined)
  state.blocker.mockReturnValue('unsupported')
})

it.each(['desktop', 'mobile'])('keeps cleanup reachable through the real %s menu and shared dialogs', async (surface) => {
  const { user } = renderWithProviders(<AppLayout />)
  await waitFor(() => expect(state.auth.oidcConfig).toHaveBeenCalledOnce())
  const trigger = surface === 'mobile'
    ? screen.getByRole('button', { name: t('common.userMenu') })
    : screen.getByRole('button', { name: /Synthetic workspace/ })
  await user.click(trigger)
  expect(screen.queryByRole('menuitem', { name: t('auth.changePassword') })).not.toBeInTheDocument()
  await user.click(screen.getByRole('menuitem', { name: t('auth.disable2fa') }))
  const dialog = screen.getByRole('dialog')
  await user.type(within(dialog).getByLabelText(t('auth.password')), 'synthetic-password')
  await user.type(within(dialog).getByLabelText(t('auth.twoFactor')), '123456')
  await user.click(within(dialog).getByRole('button', { name: t('auth.disable2fa') }))
  await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
  expect(state.auth.disable2fa).toHaveBeenCalledWith('synthetic-password', '123456')
  await user.click(trigger)
  expect(screen.queryByRole('menuitem', { name: /2FA|two.factor/i })).not.toBeInTheDocument()
  await user.click(screen.getByRole('menuitem', { name: t('auth.passkeysTitle') }))
  await screen.findByText('Saved key')
  expect(screen.queryByRole('button', { name: t('auth.addPasskey') })).not.toBeInTheDocument()
  expect(screen.queryByText(t('auth.passkeyUnsupported'))).not.toBeInTheDocument()
  await user.click(screen.getByRole('button', { name: t('auth.deletePasskey') }))
  await user.click(screen.getByRole('button', { name: t('common.delete') }))
  expect(await screen.findByText(t('auth.noPasskeys'))).toBeInTheDocument()
  expect(state.auth.deletePasskey).toHaveBeenCalledWith('synthetic-key')
  expect(state.auth.setup2fa).not.toHaveBeenCalled()
  expect(state.auth.enable2fa).not.toHaveBeenCalled()
  expect(state.auth.registerPasskeyOptions).not.toHaveBeenCalled()
})

it.each(['loading', 'enabled', 'failed'])('keeps enrollment gated through config %s', async (config) => {
  let finish!: (config: unknown) => void
  if (config === 'loading') state.auth.oidcConfig.mockImplementationOnce(() => new Promise(resolve => { finish = resolve }))
  else if (config === 'failed') state.auth.oidcConfig.mockRejectedValueOnce(new Error('unavailable'))
  else state.auth.oidcConfig.mockResolvedValueOnce({ local_auth_enabled: true })
  state.user.is_2fa_enabled = false
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

it('never offers TOTP enrollment when reopened after cleanup with local auth disabled', () => {
  state.user.is_2fa_enabled = false
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
