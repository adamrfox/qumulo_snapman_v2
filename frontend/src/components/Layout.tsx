import { useState } from 'react'
import { Link, Outlet, useNavigate } from 'react-router-dom'
import { api } from '../api'
import { useAuth } from '../App'

export default function Layout() {
  const { user, setUser } = useAuth()
  const navigate = useNavigate()
  const [showChangePassword, setShowChangePassword] = useState(false)
  const [pwForm, setPwForm] = useState({ current_password: '', new_password: '', confirm: '' })
  const [pwError, setPwError] = useState('')
  const [pwSuccess, setPwSuccess] = useState(false)

  async function logout() {
    await api.auth.logout()
    setUser(null)
    navigate('/login')
  }

  function openChangePassword() {
    setPwForm({ current_password: '', new_password: '', confirm: '' })
    setPwError('')
    setPwSuccess(false)
    setShowChangePassword(true)
  }

  async function submitChangePassword(e: React.FormEvent) {
    e.preventDefault()
    setPwError('')
    if (pwForm.new_password !== pwForm.confirm) {
      setPwError('New passwords do not match')
      return
    }
    try {
      await api.users.changePassword(pwForm.current_password, pwForm.new_password)
      setPwSuccess(true)
      setPwForm({ current_password: '', new_password: '', confirm: '' })
    } catch (err: unknown) {
      setPwError(err instanceof Error ? err.message : 'Failed to change password')
    }
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
          <button onClick={openChangePassword} className="text-lychee-300 hover:text-lychee-100">
            Change password
          </button>
          <button onClick={logout} className="text-lychee-300 hover:text-lychee-100">
            Sign out
          </button>
        </div>
      </header>
      <main className="flex-1 overflow-auto bg-blackberry-950">
        <Outlet />
      </main>

      {showChangePassword && (
        <div className="fixed inset-0 flex items-center justify-center bg-black/40 p-4">
          <div className="w-full max-w-sm rounded-lg border border-blackberry-700 bg-blackberry-900 p-6 shadow-xl">
            <h3 className="mb-4 text-base font-semibold text-lychee-100">Change password</h3>
            {pwSuccess ? (
              <div className="space-y-4">
                <p className="text-sm text-kiwi-400">Password updated.</p>
                <div className="flex justify-end">
                  <button
                    onClick={() => setShowChangePassword(false)}
                    className="rounded-md bg-agave-500 px-4 py-1.5 text-sm text-blackberry-950 hover:bg-agave-600"
                  >
                    Done
                  </button>
                </div>
              </div>
            ) : (
              <form onSubmit={submitChangePassword} className="space-y-3">
                <div>
                  <label className="mb-1 block text-xs font-medium text-lychee-400">Current password</label>
                  <input
                    type="password"
                    value={pwForm.current_password}
                    onChange={e => setPwForm(f => ({ ...f, current_password: e.target.value }))}
                    required
                    className="w-full rounded-md border border-blackberry-700 bg-blackberry-800 px-3 py-1.5 text-sm text-lychee-300 focus:outline-none focus:ring-2 focus:ring-agave-500/30 focus:border-agave-500"
                  />
                </div>
                <div>
                  <label className="mb-1 block text-xs font-medium text-lychee-400">New password</label>
                  <input
                    type="password"
                    value={pwForm.new_password}
                    onChange={e => setPwForm(f => ({ ...f, new_password: e.target.value }))}
                    required
                    className="w-full rounded-md border border-blackberry-700 bg-blackberry-800 px-3 py-1.5 text-sm text-lychee-300 focus:outline-none focus:ring-2 focus:ring-agave-500/30 focus:border-agave-500"
                  />
                </div>
                <div>
                  <label className="mb-1 block text-xs font-medium text-lychee-400">Confirm new password</label>
                  <input
                    type="password"
                    value={pwForm.confirm}
                    onChange={e => setPwForm(f => ({ ...f, confirm: e.target.value }))}
                    required
                    className="w-full rounded-md border border-blackberry-700 bg-blackberry-800 px-3 py-1.5 text-sm text-lychee-300 focus:outline-none focus:ring-2 focus:ring-agave-500/30 focus:border-agave-500"
                  />
                </div>
                {pwError && <p className="text-xs text-pomegranate-400">{pwError}</p>}
                <div className="flex justify-end gap-2 pt-2">
                  <button
                    type="button"
                    onClick={() => setShowChangePassword(false)}
                    className="rounded-md px-4 py-1.5 text-sm text-lychee-300 hover:bg-blackberry-850"
                  >
                    Cancel
                  </button>
                  <button
                    type="submit"
                    className="rounded-md bg-agave-500 px-4 py-1.5 text-sm text-blackberry-950 hover:bg-agave-600"
                  >
                    Update
                  </button>
                </div>
              </form>
            )}
          </div>
        </div>
      )}
    </div>
  )
}
