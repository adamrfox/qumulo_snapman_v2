import { useEffect, useState } from 'react'
import { api } from '../api'
import { getConsoleLogText } from '../consoleLog'
import type { Role, User } from '../types'
import { useAuth } from '../App'

const ROLE_LABELS: Record<Role, string> = {
  admin: 'Admin',
  operator: 'Operator',
  viewer: 'Viewer',
}

export default function Admin() {
  const { user: me } = useAuth()
  const [users, setUsers] = useState<User[]>([])
  const [showCreate, setShowCreate] = useState(false)
  const [form, setForm] = useState({ username: '', password: '', role: 'viewer' as Role })
  const [createError, setCreateError] = useState('')
  const [actionError, setActionError] = useState('')
  const [downloadingLogs, setDownloadingLogs] = useState(false)
  const [downloadError, setDownloadError] = useState('')

  useEffect(() => {
    api.users.list().then(setUsers).catch(() => {})
  }, [])

  async function downloadDiagnostics() {
    setDownloadError('')
    setDownloadingLogs(true)
    try {
      const { backend_log, nginx_access_log, nginx_error_log } = await api.admin.getLogs()
      const text = [
        `snapman diagnostics — ${new Date().toISOString()}`,
        '',
        '===== BACKEND LOG =====',
        backend_log || '(empty)',
        '',
        '===== NGINX ACCESS LOG =====',
        nginx_access_log || '(empty)',
        '',
        '===== NGINX ERROR LOG =====',
        nginx_error_log || '(empty)',
        '',
        '===== BROWSER CONSOLE (this session) =====',
        getConsoleLogText() || '(empty)',
      ].join('\n')

      const blob = new Blob([text], { type: 'text/plain' })
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      a.download = `snapman-diagnostics-${new Date().toISOString().replace(/[:.]/g, '-')}.log`
      document.body.appendChild(a)
      a.click()
      a.remove()
      URL.revokeObjectURL(url)
    } catch (err: unknown) {
      setDownloadError(err instanceof Error ? err.message : 'Failed to download diagnostics')
    } finally {
      setDownloadingLogs(false)
    }
  }

  async function createUser(e: React.FormEvent) {
    e.preventDefault()
    setCreateError('')
    try {
      const u = await api.users.create(form.username, form.password, form.role) as User
      setUsers(prev => [...prev, u])
      setShowCreate(false)
      setForm({ username: '', password: '', role: 'viewer' })
    } catch (err: unknown) {
      setCreateError(err instanceof Error ? err.message : 'Failed')
    }
  }

  async function setRole(id: string, role: Role) {
    setActionError('')
    try {
      const updated = await api.users.update(id, { role }) as User
      setUsers(prev => prev.map(u => u.id === id ? updated : u))
    } catch (err: unknown) {
      setActionError(err instanceof Error ? err.message : 'Failed to update role')
    }
  }

  async function toggleActive(id: string, is_active: boolean) {
    setActionError('')
    try {
      const updated = await api.users.update(id, { is_active }) as User
      setUsers(prev => prev.map(u => u.id === id ? updated : u))
    } catch (err: unknown) {
      setActionError(err instanceof Error ? err.message : 'Failed')
    }
  }

  return (
    <div className="p-6">
      <div className="mb-4 flex items-center justify-between">
        <h2 className="text-lg font-light text-lychee-100">User management</h2>
        <button
          onClick={() => setShowCreate(true)}
          className="rounded-md bg-agave-500 px-4 py-1.5 text-sm text-blackberry-950 hover:bg-agave-600"
        >
          Add user
        </button>
      </div>

      {actionError && <p className="mb-3 text-sm text-pomegranate-400">{actionError}</p>}

      <div className="overflow-x-auto rounded-lg border border-blackberry-700 bg-blackberry-900 shadow-md">
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b border-blackberry-700 bg-blackberry-800 text-left text-xs font-medium uppercase text-lychee-100">
              <th className="px-4 py-3">Username</th>
              <th className="px-4 py-3">Role</th>
              <th className="px-4 py-3">Status</th>
              <th className="px-4 py-3">Created</th>
              <th className="px-4 py-3"></th>
            </tr>
          </thead>
          <tbody className="divide-y divide-blackberry-700">
            {users.map(u => (
              <tr key={u.id} className={`text-lychee-300 ${!u.is_active ? 'opacity-50' : ''}`}>
                <td className="px-4 py-3 font-medium text-lychee-100">{u.username}</td>
                <td className="px-4 py-3">
                  {u.id === me?.id ? (
                    <span className="rounded bg-blackberry-800 px-2 py-0.5 text-xs">{ROLE_LABELS[u.role]}</span>
                  ) : (
                    <select
                      value={u.role}
                      onChange={e => setRole(u.id, e.target.value as Role)}
                      className="rounded-md border border-blackberry-700 bg-blackberry-800 px-2 py-0.5 text-xs text-lychee-300"
                    >
                      <option value="viewer">Viewer</option>
                      <option value="operator">Operator</option>
                      <option value="admin">Admin</option>
                    </select>
                  )}
                </td>
                <td className="px-4 py-3">
                  <span className={`rounded px-2 py-0.5 text-xs ${u.is_active ? 'bg-kiwi-600 text-lychee-50' : 'bg-blackberry-800 text-lychee-500'}`}>
                    {u.is_active ? 'Active' : 'Inactive'}
                  </span>
                </td>
                <td className="px-4 py-3 text-xs text-lychee-500">
                  {new Date(u.created_at).toLocaleDateString()}
                </td>
                <td className="px-4 py-3 text-right">
                  {u.id !== me?.id && (
                    <button
                      onClick={() => toggleActive(u.id, !u.is_active)}
                      className="text-xs text-lychee-400 hover:text-lychee-100"
                    >
                      {u.is_active ? 'Deactivate' : 'Reactivate'}
                    </button>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <div className="mt-8">
        <h2 className="mb-1 text-lg font-light text-lychee-100">Diagnostics</h2>
        <p className="mb-3 text-sm text-lychee-400">
          Bundle the backend log, nginx log, and this browser's console output from the current
          session into one file to send along when reporting an issue.
        </p>
        <div className="flex items-center gap-3">
          <button
            onClick={downloadDiagnostics}
            disabled={downloadingLogs}
            className="rounded-md border border-blackberry-700 px-4 py-1.5 text-sm text-lychee-300 hover:bg-blackberry-850 disabled:opacity-40"
          >
            {downloadingLogs ? 'Preparing…' : 'Download diagnostics'}
          </button>
          {downloadError && <p className="text-sm text-pomegranate-400">{downloadError}</p>}
        </div>
      </div>

      {showCreate && (
        <div className="fixed inset-0 flex items-center justify-center bg-black/40 p-4">
          <div className="w-full max-w-sm rounded-lg border border-blackberry-700 bg-blackberry-900 p-6 shadow-xl">
            <h3 className="mb-4 text-base font-semibold text-lychee-100">Add user</h3>
            <form onSubmit={createUser} className="space-y-3">
              <div>
                <label className="mb-1 block text-xs font-medium text-lychee-400">Username</label>
                <input
                  value={form.username}
                  onChange={e => setForm(f => ({ ...f, username: e.target.value }))}
                  required
                  className="w-full rounded-md border border-blackberry-700 bg-blackberry-800 px-3 py-1.5 text-sm text-lychee-300 focus:outline-none focus:ring-2 focus:ring-agave-500/30 focus:border-agave-500"
                />
              </div>
              <div>
                <label className="mb-1 block text-xs font-medium text-lychee-400">Password</label>
                <input
                  type="password"
                  value={form.password}
                  onChange={e => setForm(f => ({ ...f, password: e.target.value }))}
                  required
                  className="w-full rounded-md border border-blackberry-700 bg-blackberry-800 px-3 py-1.5 text-sm text-lychee-300 focus:outline-none focus:ring-2 focus:ring-agave-500/30 focus:border-agave-500"
                />
              </div>
              <div>
                <label className="mb-1 block text-xs font-medium text-lychee-400">Role</label>
                <select
                  value={form.role}
                  onChange={e => setForm(f => ({ ...f, role: e.target.value as Role }))}
                  className="w-full rounded-md border border-blackberry-700 bg-blackberry-800 px-3 py-1.5 text-sm text-lychee-300"
                >
                  <option value="viewer">Viewer — view only</option>
                  <option value="operator">Operator — view + delete snapshots</option>
                  <option value="admin">Admin — full access + manage users</option>
                </select>
              </div>
              {createError && <p className="text-xs text-pomegranate-400">{createError}</p>}
              <div className="flex justify-end gap-2 pt-2">
                <button
                  type="button"
                  onClick={() => setShowCreate(false)}
                  className="rounded-md px-4 py-1.5 text-sm text-lychee-300 hover:bg-blackberry-850"
                >
                  Cancel
                </button>
                <button
                  type="submit"
                  className="rounded-md bg-agave-500 px-4 py-1.5 text-sm text-blackberry-950 hover:bg-agave-600"
                >
                  Create
                </button>
              </div>
            </form>
          </div>
        </div>
      )}
    </div>
  )
}
