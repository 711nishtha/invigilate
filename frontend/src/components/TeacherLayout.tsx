import { NavLink, Outlet, useNavigate } from 'react-router-dom'

import { useAuth } from '../lib/auth'

const NAV = [
  { to: '/teacher/dashboard', label: 'Dashboard' },
  { to: '/teacher/quizzes', label: 'My quizzes' },
  { to: '/teacher/quizzes/new', label: 'Create quiz' },
]

export function TeacherLayout() {
  const { profile, signOut } = useAuth()
  const navigate = useNavigate()

  async function handleSignOut() {
    await signOut()
    navigate('/', { replace: true })
  }

  return (
    <div className="min-h-screen bg-slate-50">
      <header className="border-b border-slate-200 bg-white">
        <div className="mx-auto flex max-w-7xl items-center justify-between gap-4 px-6 py-3">
          <div className="flex items-center gap-8">
            <NavLink to="/teacher/dashboard" className="text-sm font-semibold text-slate-900">
              Secure Quiz
            </NavLink>
            <nav className="flex items-center gap-1">
              {NAV.map((item) => (
                <NavLink
                  key={item.to}
                  to={item.to}
                  end={item.to === '/teacher/quizzes'}
                  className={({ isActive }) =>
                    `rounded-lg px-3 py-1.5 text-sm font-medium transition-colors ${
                      isActive
                        ? 'bg-brand-50 text-brand-700'
                        : 'text-slate-600 hover:bg-slate-100 hover:text-slate-900'
                    }`
                  }
                >
                  {item.label}
                </NavLink>
              ))}
            </nav>
          </div>

          <div className="flex items-center gap-3">
            <div className="text-right">
              <p className="text-sm font-medium text-slate-900">{profile?.name ?? 'Teacher'}</p>
              <p className="text-xs text-slate-500">{profile?.email}</p>
            </div>
            <button type="button" onClick={handleSignOut} className="btn-secondary">
              Sign out
            </button>
          </div>
        </div>
      </header>

      <main className="mx-auto max-w-7xl px-6 py-8">
        <Outlet />
      </main>
    </div>
  )
}
