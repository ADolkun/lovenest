import { useState, useCallback } from 'react'
import { Link, Outlet, useLocation } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import { useQuery } from '@tanstack/react-query'
import { useAuth } from '@/contexts/auth-context'
import { auth as authApi } from '@/lib/api'
import { OnboardingTour } from '@/components/onboarding-tour'
import { useTheme } from 'next-themes'
import { accounts as accountsApi } from '@/lib/api'
import { Button } from '@/components/ui/button'
import { Avatar, AvatarFallback } from '@/components/ui/avatar'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'
import { cn } from '@/lib/utils'
import { ShellLogo } from '@/components/shell-logo'
import {
  LayoutDashboard,
  ArrowLeftRight,
  Building2,
  SlidersHorizontal,
  Upload,
  LogOut,
  Menu,
  ChevronRight,
  Tag,
  PiggyBank,
  Eye,
  EyeOff,
  Repeat,
  Landmark,
  Sun,
  Moon,
} from 'lucide-react'
import { usePrivacyMode } from '@/hooks/use-privacy-mode'

const navItems = [
  { key: 'dashboard', path: '/', icon: LayoutDashboard },
  { key: 'transactions', path: '/transactions', icon: ArrowLeftRight },
  { key: 'accounts', path: '/accounts', icon: Building2 },
  { key: 'categories', path: '/categories', icon: Tag },
  { key: 'budgets', path: '/budgets', icon: PiggyBank },
  { key: 'assets', path: '/assets', icon: Landmark },
  { key: 'recurring', path: '/recurring', icon: Repeat },
  { key: 'rules', path: '/rules', icon: SlidersHorizontal },
  { key: 'import', path: '/import', icon: Upload },
] as const

function formatCurrency(value: number, currency = 'BRL', locale = 'pt-BR') {
  return new Intl.NumberFormat(locale, { style: 'currency', currency }).format(value)
}

export function AppLayout() {
  const { t, i18n } = useTranslation()
  const { user, logout, updateUser } = useAuth()
  const userCurrency = user?.preferences?.currency_display ?? 'BRL'
  const locale = i18n.language === 'en' ? 'en-US' : i18n.language
  const { theme, setTheme } = useTheme()
  const location = useLocation()
  const [sidebarOpen, setSidebarOpen] = useState(false)
  const [accountsExpanded, setAccountsExpanded] = useState(true)
  const { privacyMode, togglePrivacyMode, mask } = usePrivacyMode()

  const showTour = user && !user.preferences?.onboarding_completed && !localStorage.getItem('onboarding_completed')

  const handleTourComplete = useCallback(async () => {
    localStorage.setItem('onboarding_completed', 'true')
    try {
      const prefs = { ...(user?.preferences || {}), onboarding_completed: true }
      const updated = await authApi.updateMe({ preferences: prefs })
      updateUser(updated)
    } catch {
      // localStorage fallback is already set
    }
  }, [user, updateUser])

  const userInitial = user?.email?.charAt(0).toUpperCase() ?? '?'
  const currentLang = i18n.language

  const { data: accountsList } = useQuery({
    queryKey: ['accounts'],
    queryFn: () => accountsApi.list(),
  })

  const allAccounts = accountsList ?? []
  const totalBalance = allAccounts.reduce((sum, a) => {
    return sum + Number(a.current_balance)
  }, 0)

  return (
    <div className="min-h-screen bg-background">
      {/* Mobile header */}
      <header className="sticky top-0 z-40 flex h-14 items-center gap-3 bg-sidebar border-b border-sidebar-border px-4 lg:hidden">
        <button
          onClick={() => setSidebarOpen(!sidebarOpen)}
          className="text-sidebar-muted hover:text-sidebar-foreground transition-colors"
          aria-label="Toggle menu"
        >
          <Menu size={20} />
        </button>
        <div className="flex items-center gap-2">
          <ShellLogo size={22} className="text-primary shrink-0" />
          <span className="font-bold text-sidebar-foreground">{t('app.name')}</span>
        </div>
        <div className="ml-auto flex items-center gap-2">
          <button
            onClick={togglePrivacyMode}
            className="text-sidebar-muted hover:text-sidebar-foreground transition-colors p-1"
            title={privacyMode ? t('privacy.show') : t('privacy.hide')}
          >
            {privacyMode ? <EyeOff size={18} /> : <Eye size={18} />}
          </button>
          <UserMenu userInitial={userInitial} logout={logout} dark />
        </div>
      </header>

      <div className="flex">
        {/* Sidebar overlay for mobile */}
        {sidebarOpen && (
          <div
            className="fixed inset-0 z-40 bg-black/50 lg:hidden"
            onClick={() => setSidebarOpen(false)}
          />
        )}

        {/* Sidebar */}
        <aside
          className={cn(
            'fixed inset-y-0 left-0 z-50 w-60 bg-sidebar border-r border-sidebar-border flex flex-col transform transition-transform lg:translate-x-0 shrink-0 overflow-y-auto',
            sidebarOpen ? 'translate-x-0' : '-translate-x-full'
          )}
        >
          {/* Logo */}
          <div className="flex h-16 items-center justify-between px-5 border-b border-sidebar-border">
            <div className="flex items-center gap-2.5">
              <ShellLogo size={24} className="text-primary shrink-0" />
              <span className="font-bold text-lg text-sidebar-foreground tracking-tight">{t('app.name')}</span>
            </div>
            <button
              onClick={togglePrivacyMode}
              className="text-sidebar-muted hover:text-sidebar-foreground transition-colors p-1 rounded-md hover:bg-sidebar-accent"
              title={privacyMode ? t('privacy.show') : t('privacy.hide')}
            >
              {privacyMode ? <EyeOff size={16} /> : <Eye size={16} />}
            </button>
          </div>

          {/* Nav */}
          <nav className="flex flex-col gap-1 p-3" data-tour="sidebar">
            {navItems.map((item) => {
              const isActive = item.path === '/'
                ? location.pathname === '/'
                : location.pathname.startsWith(item.path)
              const Icon = item.icon
              return (
                <Link
                  key={item.key}
                  to={item.path}
                  data-tour={`nav-${item.key}`}
                  onClick={() => setSidebarOpen(false)}
                  className={cn(
                    'flex items-center gap-3 text-[15px] font-medium transition-all',
                    isActive
                      ? 'bg-primary/[0.08] text-primary rounded-lg border-l-[3px] border-primary pl-[9px] pr-3 py-2'
                      : 'rounded-lg px-3 py-2 text-sidebar-muted hover:bg-sidebar-accent hover:text-sidebar-foreground'
                  )}
                >
                  <Icon
                    size={20}
                    className={cn('shrink-0', isActive ? 'text-primary' : 'text-sidebar-muted')}
                  />
                  <span>{t(`nav.${item.key}`)}</span>
                </Link>
              )
            })}
          </nav>

          {/* Account list in sidebar */}
          {allAccounts.length > 0 && (
            <div className="px-3 pb-2 mt-2">
              <button
                onClick={() => setAccountsExpanded(!accountsExpanded)}
                className="flex items-center justify-between w-full px-3 py-2 hover:text-sidebar-foreground transition-colors"
              >
                <span className="text-[11px] uppercase tracking-[0.12em] font-semibold text-sidebar-muted">{t('accounts.title')}</span>
                <div className="flex items-center gap-2">
                  <span className={`tabular-nums font-medium text-xs ${totalBalance < 0 ? 'text-rose-400' : 'text-sidebar-muted'}`}>
                    {mask(formatCurrency(totalBalance, userCurrency, locale))}
                  </span>
                  <ChevronRight
                    size={12}
                    className={cn('text-sidebar-muted transition-transform', accountsExpanded && 'rotate-90')}
                  />
                </div>
              </button>
              {accountsExpanded && (
                <div className="mt-1 space-y-0.5">
                  {allAccounts.map((acc) => {
                    const balance = Number(acc.current_balance)
                    const prevBalance = acc.previous_balance ?? 0
                    const pctChange = prevBalance !== 0
                      ? ((balance - prevBalance) / Math.abs(prevBalance)) * 100
                      : null
                    const typeKey = acc.type.replace(/_([a-z])/g, (_, c: string) => c.toUpperCase()).replace(/^./, c => c.toUpperCase())

                    return (
                      <Link
                        key={acc.id}
                        to={`/accounts/${acc.id}`}
                        onClick={() => setSidebarOpen(false)}
                        className="flex items-center justify-between px-3 py-2 rounded-lg text-[13px] text-sidebar-muted hover:bg-sidebar-accent hover:text-sidebar-foreground transition-all"
                      >
                        <div className="truncate min-w-0">
                          <span className="block truncate">{acc.name}</span>
                          <span className="block text-[11px] text-sidebar-muted/60">
                            {t(`accounts.type${typeKey}`)}
                          </span>
                        </div>
                        <div className="text-right shrink-0 ml-2">
                          <span className={`block tabular-nums font-medium text-[13px] ${balance < 0 ? 'text-rose-400' : 'text-sidebar-foreground'}`}>
                            {mask(formatCurrency(balance, acc.currency))}
                          </span>
                          {pctChange !== null && (
                            <span className={`block text-[11px] tabular-nums font-medium ${pctChange >= 0 ? 'text-emerald-400' : 'text-rose-400'}`}>
                              {mask(`${pctChange >= 0 ? '+' : ''}${pctChange.toFixed(1)}%`)}
                            </span>
                          )}
                        </div>
                      </Link>
                    )
                  })}
                </div>
              )}
            </div>
          )}

          <div className="flex-1" />

          {/* Language & Theme toggles */}
          <div className="group/toggles px-3 pb-2 border-b border-sidebar-border">
            <div className="flex items-center justify-between gap-2 px-1 py-2">
              {/* Language toggle */}
              <div className="flex items-center gap-1">
                <button
                  onClick={() => i18n.changeLanguage('pt-BR')}
                  className={cn(
                    'px-2 py-1 rounded text-[11px] font-semibold transition-all duration-300',
                    currentLang === 'pt-BR'
                      ? 'bg-primary/15 text-primary group-hover/toggles:bg-primary/25'
                      : 'text-sidebar-muted/40 group-hover/toggles:text-sidebar-muted group-hover/toggles:hover:text-sidebar-foreground'
                  )}
                >
                  PT
                </button>
                <button
                  onClick={() => i18n.changeLanguage('en')}
                  className={cn(
                    'px-2 py-1 rounded text-[11px] font-semibold transition-all duration-300',
                    currentLang === 'en'
                      ? 'bg-primary/15 text-primary group-hover/toggles:bg-primary/25'
                      : 'text-sidebar-muted/40 group-hover/toggles:text-sidebar-muted group-hover/toggles:hover:text-sidebar-foreground'
                  )}
                >
                  EN
                </button>
              </div>
              {/* Theme toggle */}
              <div className="flex items-center gap-1">
                <button
                  onClick={() => setTheme('light')}
                  className={cn(
                    'p-1.5 rounded transition-all duration-300',
                    theme === 'light'
                      ? 'bg-primary/15 text-primary group-hover/toggles:bg-primary/25'
                      : 'text-sidebar-muted/40 group-hover/toggles:text-sidebar-muted group-hover/toggles:hover:text-sidebar-foreground'
                  )}
                >
                  <Sun size={14} />
                </button>
                <button
                  onClick={() => setTheme('dark')}
                  className={cn(
                    'p-1.5 rounded transition-all duration-300',
                    theme === 'dark'
                      ? 'bg-primary/15 text-primary group-hover/toggles:bg-primary/25'
                      : 'text-sidebar-muted/40 group-hover/toggles:text-sidebar-muted group-hover/toggles:hover:text-sidebar-foreground'
                  )}
                >
                  <Moon size={14} />
                </button>
              </div>
            </div>
          </div>

          {/* User section */}
          <div className="p-3">
            <DropdownMenu>
              <DropdownMenuTrigger asChild>
                <button className="flex items-center gap-3 w-full rounded-lg px-3 py-2.5 text-sm hover:bg-sidebar-accent transition-colors text-left">
                  <Avatar className="h-7 w-7 shrink-0">
                    <AvatarFallback className="bg-primary/20 text-primary text-xs font-semibold">
                      {userInitial}
                    </AvatarFallback>
                  </Avatar>
                  <span className="text-xs text-sidebar-muted truncate flex-1">{user?.email}</span>
                </button>
              </DropdownMenuTrigger>
              <DropdownMenuContent align="end" className="w-48" side="top">
                <DropdownMenuItem
                  onClick={logout}
                  className="flex items-center gap-2 text-rose-600 focus:text-rose-600"
                >
                  <LogOut size={14} />
                  {t('auth.logout')}
                </DropdownMenuItem>
              </DropdownMenuContent>
            </DropdownMenu>
          </div>
        </aside>

        {/* Main content */}
        <main className="flex-1 min-h-screen overflow-x-hidden lg:ml-60">
          <div className="p-6 max-w-7xl mx-auto">
            <Outlet />
          </div>
        </main>
      </div>

      {showTour && <OnboardingTour onComplete={handleTourComplete} />}
    </div>
  )
}

function UserMenu({ userInitial, logout, dark }: { userInitial: string; logout: () => void; dark?: boolean }) {
  const { t } = useTranslation()
  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button variant="ghost" className="relative h-8 w-8 rounded-full p-0">
          <Avatar className="h-8 w-8">
            <AvatarFallback className={dark ? 'bg-primary/20 text-primary text-xs font-semibold' : 'bg-primary/10 text-primary text-xs font-semibold'}>
              {userInitial}
            </AvatarFallback>
          </Avatar>
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end">
        <DropdownMenuItem onClick={logout} className="text-rose-600 focus:text-rose-600">
          {t('auth.logout')}
        </DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  )
}
