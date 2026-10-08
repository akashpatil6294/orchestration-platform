/**
 * The signed-in shell: navigation, the account menu, the theme toggle and the
 * routed page. Toast and confirm providers live here so every page (and the
 * shell itself) can raise notifications and ask for confirmation.
 */
import { useState } from 'react'
import { Link, NavLink, Outlet, useNavigate, useLocation } from 'react-router-dom'

import { avatarUrlFor, displayNameFor, initialsFor, useAuth } from '../auth/AuthProvider'
import TeamSwitcher from './TeamSwitcher'
import { useTheme } from '../lib/theme'
import { ConfirmProvider } from './ConfirmDialogProvider'
import { ToastProvider } from './ToastProvider'

const NAV_ITEMS = [
  { to: '/dashboard', label: 'Dashboard' },
  { to: '/workflows', label: 'Workflows' },
  { to: '/runs', label: 'Runs' },
  { to: '/schedules', label: 'Schedules' },
  { to: '/triggers', label: 'Triggers' },
  { to: '/ops/workers', label: 'Workers & queues' },
  { to: '/ops/dlq', label: 'Dead letters' },
  { to: '/settings', label: 'Settings' },
  { to: '/audit', label: 'Audit log' },
  { to: '/runs/compare', label: 'Compare' },
  { to: '/schedules/calendar', label: 'Calendar' },
  { to: '/templates', label: 'Templates' },
  { to: '/connections', label: 'Connections' },
  { to: '/documents', label: 'Documents' },
  { to: '/notifications', label: 'Notifications' },
  { to: '/teams', label: 'Teams' },
  { to: '/alerts', label: 'Alerts' },
  { to: '/ops/recovery', label: 'Recovery' },
]

function ThemeToggle() {
  const { theme, toggle } = useTheme()
  const next = theme === 'dark' ? 'light' : 'dark'
  return (
    <button
      type="button"
      className="button button-ghost"
      onClick={toggle}
      aria-label={`Switch to ${next} theme`}
      title={`Switch to ${next} theme`}
    >
      {theme === 'dark' ? '☀' : '☾'}
    </button>
  )
}

function UserMenu() {
  const { user, signOut } = useAuth()
  const navigate = useNavigate()
  const [open, setOpen] = useState(false)
  const [signingOut, setSigningOut] = useState(false)

  const avatar = avatarUrlFor(user)
  const name = displayNameFor(user)

  async function handleSignOut() {
    setSigningOut(true)
    try {
      await signOut()
      navigate('/login', { replace: true })
    } finally {
      setSigningOut(false)
      setOpen(false)
    }
  }

  return (
    <div className="user-menu">
      <button
        type="button"
        className="user-menu-trigger"
        aria-label="Account menu"
        aria-haspopup="menu"
        aria-expanded={open}
        onClick={() => setOpen((value) => !value)}
      >
        {avatar ? (
          <img className="avatar" src={avatar} alt="" referrerPolicy="no-referrer" />
        ) : (
          <span className="avatar avatar-fallback" aria-hidden="true">
            {initialsFor(user)}
          </span>
        )}
        <span className="user-menu-text">
          <span className="user-menu-name">{name}</span>
          <span className="user-menu-email">{user?.email ?? ''}</span>
        </span>
        <span className="chevron" aria-hidden="true">
          ▾
        </span>
      </button>

      {open ? (
        <div className="user-menu-panel" role="menu">
          <div className="user-menu-panel-header">
            <strong>{name}</strong>
            <span>{user?.email}</span>
          </div>
          <button
            type="button"
            role="menuitem"
            className="user-menu-action"
            onClick={() => {
              setOpen(false)
              navigate('/settings')
            }}
          >
            Account settings
          </button>
          <button type="button" role="menuitem" className="user-menu-action" onClick={handleSignOut} disabled={signingOut}>
            {signingOut ? 'Signing out…' : 'Sign out'}
          </button>
        </div>
      ) : null}
    </div>
  )
}

function HeaderAvatar() {
  const { user } = useAuth()
  const avatar = avatarUrlFor(user)
  return <Link to="/settings" className="header-avatar" aria-label="Account settings" title={displayNameFor(user)}>{avatar ? <img className="avatar" src={avatar} alt="" referrerPolicy="no-referrer"/> : <span className="avatar avatar-fallback" aria-hidden="true">{initialsFor(user).slice(0, 1)}</span>}</Link>
}

export function AppShell() {
  const { pathname } = useLocation()
  const [navigationOpen, setNavigationOpen] = useState(false)
  const pageTitle = [...NAV_ITEMS].sort((a, b) => b.to.length - a.to.length).find(item => pathname === item.to || pathname.startsWith(item.to + '/'))?.label || 'Orchestrator'
  return (
    <ToastProvider>
      <ConfirmProvider>
        <div className={`app-shell${navigationOpen ? ' navigation-open' : ''}`}>
          <aside className="sidebar" id="main-navigation" onKeyDown={event => { if (event.key === 'Escape') setNavigationOpen(false) }}>
            <div className="brand brand-compact">
              <span className="brand-mark" aria-hidden="true">
                ⛓
              </span>
              <span className="brand-text">
                <strong>Orchestrator</strong>
                <small>Workflow platform</small>
              </span>
            </div>
            <nav className="sidebar-nav" aria-label="Main">
              {NAV_ITEMS.map((item) => (
                <NavLink
                  key={item.to}
                  to={item.to}
                  end={item.to === '/runs' || item.to === '/schedules'}
                  onClick={() => setNavigationOpen(false)}
                  className={({ isActive }) => `nav-link${isActive ? ' nav-link-active' : ''}`}
                >
                  {item.label}
                  {item.to === '/ops/dlq' && <span id="dead-letter-indicator"/>}
                </NavLink>
              ))}
            </nav>
            <div className="sidebar-account"><UserMenu /></div>
          </aside>

          <div className="app-main">
            <header className="app-header">
              <button type="button" className="button button-ghost navigation-toggle" aria-label={navigationOpen ? 'Close navigation' : 'Open navigation'} aria-controls="main-navigation" aria-expanded={navigationOpen} onClick={() => setNavigationOpen(open => !open)}>☰</button>
              <div
          id="dashboard-header"
          style={{ display: pathname === '/dashboard' ? undefined : 'none' }}
        />
        {pathname !== '/dashboard' && <div className="app-header-title">{pageTitle}</div>}
              <div className="page-actions">
                <TeamSwitcher />
                <ThemeToggle />
                <HeaderAvatar />
              </div>
            </header>
            <main className="app-content">
              <Outlet />
            </main>
          </div>
        </div>
      </ConfirmProvider>
    </ToastProvider>
  )
}

export default AppShell
