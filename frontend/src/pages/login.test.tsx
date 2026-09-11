import { beforeEach, describe, expect, it, vi } from 'vitest'
import { act, screen, waitFor } from '@testing-library/react'
import type { AxiosError } from 'axios'

import LoginPage from '@/pages/login'
import { renderWithProviders, t } from '@/test/utils'

const navigate = vi.hoisted(() => vi.fn())
vi.mock('react-router-dom', async (importOriginal) => ({
  ...(await importOriginal<typeof import('react-router-dom')>()),
  useNavigate: () => navigate,
}))

const authContext = vi.hoisted(() => ({
  login: vi.fn(),
  verify2fa: vi.fn(),
  loginWithToken: vi.fn(),
  token: null as string | null,
}))
vi.mock('@/contexts/auth-context', () => ({ useAuth: () => authContext }))

const api = vi.hoisted(() => ({
  setup: { status: vi.fn() },
  auth: { oidcConfig: vi.fn(), passkeyAuthenticationOptions: vi.fn(), verifyPasskeyAuthentication: vi.fn() },
  admin: { registrationStatus: vi.fn(), defaultColors: vi.fn() },
}))
vi.mock('@/lib/api', () => ({
  setup: api.setup,
  auth: api.auth,
  admin: api.admin,
}))

const webauthn = vi.hoisted(() => ({
  isPasskeySupported: vi.fn(),
  isConditionalPasskeySupported: vi.fn(),
  startConditionalPasskeyAuthentication: vi.fn(),
  startPasskeyAuthentication: vi.fn(),
}))
vi.mock('@/lib/webauthn', () => ({
  ...webauthn,
  passkeyFailure: () => 'unknown',
}))

vi.mock('next-themes', () => ({ useTheme: () => ({ resolvedTheme: 'dark' }) }))

/** Build the axios-shaped rejection the page branches on. */
function httpError(status?: number): AxiosError {
  return (status === undefined
    ? { isAxiosError: true, response: undefined }
    : { isAxiosError: true, response: { status } }) as AxiosError
}

beforeEach(() => {
  vi.clearAllMocks()
  webauthn.isPasskeySupported.mockReturnValue(false)
  webauthn.isConditionalPasskeySupported.mockResolvedValue(false)
  api.auth.passkeyAuthenticationOptions.mockResolvedValue({ challenge_id: 'challenge', options: {} })
  api.auth.verifyPasskeyAuthentication.mockResolvedValue({ access_token: 'synthetic-token' })
  webauthn.startPasskeyAuthentication.mockResolvedValue({ id: 'synthetic-key' })
  authContext.token = null
  api.setup.status.mockResolvedValue({ has_users: true })
  api.auth.oidcConfig.mockResolvedValue({
    enabled: false,
    provider_name: 'OIDC',
    local_auth_enabled: true,
  })
  api.admin.registrationStatus.mockResolvedValue({ enabled: true })
  api.admin.defaultColors.mockResolvedValue({ light: null, dark: null })
})

async function renderLogin() {
  const rendered = renderWithProviders(<LoginPage />, { route: '/login' })
  await screen.findByLabelText(t('auth.email'))
  return rendered
}

describe('LoginPage', () => {
  it('renders the credentials form', async () => {
    await renderLogin()

    expect(screen.getByLabelText(t('auth.email'))).toBeInTheDocument()
    expect(screen.getByLabelText(t('auth.password'))).toBeInTheDocument()
    expect(
      screen.getByRole('button', { name: t('auth.login') }),
    ).toBeInTheDocument()
  })

  it('keeps OIDC primary while retaining the local fallback', async () => {
    api.auth.oidcConfig.mockResolvedValue({
      enabled: true,
      provider_name: 'Company SSO',
      local_auth_enabled: true,
    })
    const { user } = renderWithProviders(<LoginPage />, { route: '/login' })

    expect(await screen.findByRole('button', { name: 'Continue with Company SSO' })).toBeInTheDocument()
    expect(screen.queryByLabelText(t('auth.email'))).not.toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: t('auth.useLocalAccount') }))
    expect(await screen.findByLabelText(t('auth.email'))).toHaveAttribute(
      'autocomplete',
      'username webauthn',
    )
  })

  it('signs in and lands on the dashboard', async () => {
    authContext.login.mockResolvedValue({ requires_2fa: false })
    const { user } = await renderLogin()

    await user.type(screen.getByLabelText(t('auth.email')), 'tassio@example.com')
    await user.type(screen.getByLabelText(t('auth.password')), 'secret')
    await user.click(screen.getByRole('button', { name: t('auth.login') }))

    await waitFor(() =>
      expect(authContext.login).toHaveBeenCalledWith(
        'tassio@example.com',
        'secret',
      ),
    )
    await waitFor(() => expect(navigate).toHaveBeenCalledWith('/'))
  })

  it('asks for the second factor instead of navigating when 2FA is on', async () => {
    authContext.login.mockResolvedValue({
      requires_2fa: true,
      temp_token: 'temp',
      available_methods: ['totp'],
    })
    const { user } = await renderLogin()

    await user.type(screen.getByLabelText(t('auth.email')), 'tassio@example.com')
    await user.type(screen.getByLabelText(t('auth.password')), 'secret')
    await user.click(screen.getByRole('button', { name: t('auth.login') }))

    expect(await screen.findByText(t('auth.twoFactorTitle'))).toBeInTheDocument()
    expect(navigate).not.toHaveBeenCalledWith('/')
  })

  it('tells the user their credentials were rejected', async () => {
    authContext.login.mockRejectedValue(httpError(401))
    const { user } = await renderLogin()

    await user.type(screen.getByLabelText(t('auth.email')), 'a@b.com')
    await user.type(screen.getByLabelText(t('auth.password')), 'wrong')
    await user.click(screen.getByRole('button', { name: t('auth.login') }))

    expect(
      await screen.findByText(t('auth.invalidCredentials')),
    ).toBeInTheDocument()
  })

  it('distinguishes an outage from a wrong password', async () => {
    // Issue #318: collapsing every failure into "invalid credentials" made a
    // stopped backend look like the user's own mistake.
    authContext.login.mockRejectedValue(httpError())
    const { user } = await renderLogin()

    await user.type(screen.getByLabelText(t('auth.email')), 'a@b.com')
    await user.type(screen.getByLabelText(t('auth.password')), 'right')
    await user.click(screen.getByRole('button', { name: t('auth.login') }))

    expect(await screen.findByText(t('auth.serverError'))).toBeInTheDocument()
    expect(
      screen.queryByText(t('auth.invalidCredentials')),
    ).not.toBeInTheDocument()
  })

  it('reports a 5xx as an outage too', async () => {
    authContext.login.mockRejectedValue(httpError(502))
    const { user } = await renderLogin()

    await user.type(screen.getByLabelText(t('auth.email')), 'a@b.com')
    await user.type(screen.getByLabelText(t('auth.password')), 'right')
    await user.click(screen.getByRole('button', { name: t('auth.login') }))

    expect(await screen.findByText(t('auth.serverError'))).toBeInTheDocument()
  })

  it('names rate limiting rather than blaming the password', async () => {
    authContext.login.mockRejectedValue(httpError(429))
    const { user } = await renderLogin()

    await user.type(screen.getByLabelText(t('auth.email')), 'a@b.com')
    await user.type(screen.getByLabelText(t('auth.password')), 'right')
    await user.click(screen.getByRole('button', { name: t('auth.login') }))

    expect(await screen.findByText(t('auth.tooManyAttempts'))).toBeInTheDocument()
  })

  it('sends an already-signed-in visitor away from the login screen', async () => {
    authContext.token = 'jwt'

    renderWithProviders(<LoginPage />, { route: '/login' })

    await waitFor(() =>
      expect(navigate).toHaveBeenCalledWith('/', { replace: true }),
    )
  })

  it('redirects a fresh install to the setup wizard', async () => {
    // No users yet: sending someone to a login form they cannot pass is a
    // dead end.
    api.setup.status.mockResolvedValue({ has_users: false })

    renderWithProviders(<LoginPage />, { route: '/login' })

    await waitFor(() =>
      expect(navigate).toHaveBeenCalledWith('/setup', { replace: true }),
    )
  })

  it('offers registration when the server allows it', async () => {
    await renderLogin()

    expect(
      await screen.findByRole('link', { name: t('auth.register') }),
    ).toBeInTheDocument()
  })

  it('survives the optional config calls failing', async () => {
    // A reverse proxy that blocks /api/admin must not blank the login form.
    api.admin.registrationStatus.mockRejectedValue(new Error('403'))
    api.admin.defaultColors.mockRejectedValue(new Error('403'))
    api.auth.oidcConfig.mockRejectedValue(new Error('500'))

    await renderLogin()

    expect(screen.getByLabelText(t('auth.email'))).toBeInTheDocument()
    expect(
      screen.getByRole('button', { name: t('auth.login') }),
    ).toBeInTheDocument()
  })
})

it.each([
  [403, 'LOCAL_AUTH_DISABLED', 'auth.localAuthDisabled'],
  [403, 'UNRELATED_REFUSAL', 'auth.invalidCredentials'],
  [401, 'LOCAL_AUTH_DISABLED', 'auth.invalidCredentials'],
])('reports only the exact disabled-local-auth refusal (%s %s)', async (status, detail, key) => {
  api.auth.oidcConfig.mockRejectedValue(new Error('config unavailable'))
  authContext.login.mockRejectedValue({ response: { status, data: { detail } } })
  const { user } = await renderLogin()
  await user.type(screen.getByLabelText(t('auth.email')), 'synthetic@example.com')
  await user.type(screen.getByLabelText(t('auth.password')), 'synthetic-password')
  await user.click(screen.getByRole('button', { name: t('auth.login') }))
  expect(await screen.findByText(t(key))).toBeInTheDocument()
  expect(screen.getByText(t('auth.authConfigUnavailable'))).toBeInTheDocument()
})

it('reveals and focuses the existing email, requires a valid email, and accepts Enter for explicit passkeys', async () => {
  webauthn.isPasskeySupported.mockReturnValue(true)
  webauthn.isConditionalPasskeySupported.mockResolvedValue(true)
  api.auth.oidcConfig.mockResolvedValue({ enabled: true, provider_name: 'SSO', local_auth_enabled: true })
  const { user } = renderWithProviders(<LoginPage />)
  const passkey = await screen.findByRole('button', { name: t('auth.loginWithPasskey') })
  expect(screen.queryByLabelText(t('auth.email'))).not.toBeInTheDocument()
  await user.click(passkey)
  const email = screen.getByLabelText(t('auth.email'))
  expect(email).toHaveFocus()
  expect(api.auth.passkeyAuthenticationOptions).not.toHaveBeenCalled()
  expect(webauthn.startConditionalPasskeyAuthentication).not.toHaveBeenCalled()
  await user.click(passkey)
  expect(email).toHaveFocus()
  await user.type(email, 'invalid')
  await user.click(passkey)
  expect(api.auth.passkeyAuthenticationOptions).not.toHaveBeenCalled()
  await user.clear(email)
  await user.type(email, 'synthetic@example.com{Enter}')
  await waitFor(() => expect(api.auth.passkeyAuthenticationOptions).toHaveBeenCalledWith('synthetic@example.com'))
  expect(webauthn.startPasskeyAuthentication).toHaveBeenCalledOnce()
  expect(authContext.login).not.toHaveBeenCalled()
  expect(authContext.loginWithToken).toHaveBeenCalledWith('synthetic-token')
})

it.each(['password', 'passkey'])('preserves conditional discovery and aborts it before explicit %s sign-in', async (method) => {
  webauthn.isPasskeySupported.mockReturnValue(true)
  webauthn.isConditionalPasskeySupported.mockResolvedValue(true)
  let finish!: (credential: unknown) => void
  webauthn.startConditionalPasskeyAuthentication.mockImplementationOnce(() => new Promise(resolve => { finish = resolve }))
  authContext.login.mockResolvedValue({ requires_2fa: false })
  const { user } = await renderLogin()
  await waitFor(() => expect(webauthn.startConditionalPasskeyAuthentication).toHaveBeenCalledOnce())
  expect(api.auth.passkeyAuthenticationOptions).toHaveBeenCalledWith()
  const signal = webauthn.startConditionalPasskeyAuthentication.mock.calls[0][1] as AbortSignal
  await user.type(screen.getByLabelText(t('auth.email')), 'synthetic@example.com')
  if (method === 'password') {
    await user.type(screen.getByLabelText(t('auth.password')), 'synthetic-password')
    await user.click(screen.getByRole('button', { name: t('auth.login') }))
  } else {
    await user.click(screen.getByRole('button', { name: t('auth.loginWithPasskey') }))
    expect(api.auth.passkeyAuthenticationOptions).toHaveBeenLastCalledWith('synthetic@example.com')
  }
  expect(signal.aborted).toBe(true)
  await act(async () => { finish({ id: 'late-conditional-key' }) })
  expect(api.auth.verifyPasskeyAuthentication.mock.calls.flat()).not.toContainEqual({ id: 'late-conditional-key' })
})

it('starts account-less discovery when the ordinary local-account section is expanded', async () => {
  webauthn.isPasskeySupported.mockReturnValue(true)
  webauthn.isConditionalPasskeySupported.mockResolvedValue(true)
  webauthn.startConditionalPasskeyAuthentication.mockImplementationOnce(() => new Promise(() => {}))
  api.auth.oidcConfig.mockResolvedValue({ enabled: true, provider_name: 'SSO', local_auth_enabled: true })
  const { user } = renderWithProviders(<LoginPage />)
  await user.click(await screen.findByRole('button', { name: t('auth.useLocalAccount') }))
  await waitFor(() => expect(webauthn.startConditionalPasskeyAuthentication).toHaveBeenCalledOnce())
  expect(api.auth.passkeyAuthenticationOptions).toHaveBeenCalledWith()
})

it.each([null, { enabled: true, local_auth_enabled: false, provider_name: 'SSO' }])('starts no ceremony while config is loading or local auth is disabled', async (config) => {
  webauthn.isPasskeySupported.mockReturnValue(true)
  webauthn.isConditionalPasskeySupported.mockResolvedValue(true)
  api.auth.oidcConfig.mockImplementationOnce(() => config ? Promise.resolve(config) : new Promise(() => {}))
  renderWithProviders(<LoginPage />)
  await act(async () => {})
  expect(screen.queryByLabelText(t('auth.email'))).not.toBeInTheDocument()
  expect(screen.queryByRole('button', { name: t('auth.loginWithPasskey') })).not.toBeInTheDocument()
  expect(api.auth.passkeyAuthenticationOptions).not.toHaveBeenCalled()
})
