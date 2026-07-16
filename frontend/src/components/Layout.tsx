import { Link, Outlet, useNavigate } from 'react-router-dom'
import { api } from '../api'
import { useAuth } from '../App'

export default function Layout() {
  const { user, setUser } = useAuth()
  const navigate = useNavigate()

  async function logout() {
    await api.auth.logout()
    setUser(null)
    navigate('/login')
  }

  return (
    <div className="flex h-screen flex-col bg-blackberry-950">
      <header className="flex items-center justify-between border-b border-blackberry-700 bg-blackberry-925 px-6 py-3 text-lychee-100">
        <Link to="/" className="text-lg font-semibold tracking-tight text-lychee-100">
          snapman
        </Link>
        <div className="flex items-center gap-4 text-sm">
          {user?.role === 'admin' && (
            <Link to="/admin" className="text-lychee-300 hover:text-lychee-100">
              Admin
            </Link>
          )}
          <span className="text-lychee-400">
            {user?.username}
            <span className="ml-1 rounded bg-blackberry-800 px-1.5 py-0.5 text-xs text-lychee-300">{user?.role}</span>
          </span>
          <button onClick={logout} className="text-lychee-300 hover:text-lychee-100">
            Sign out
          </button>
        </div>
      </header>
      <main className="flex-1 overflow-auto bg-blackberry-950">
        <Outlet />
      </main>
    </div>
  )
}
