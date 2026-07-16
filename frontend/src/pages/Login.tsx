import { FormEvent, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { api } from '../api'
import { useAuth } from '../App'
import type { AuthUser } from '../types'

export default function Login() {
  const { setUser } = useAuth()
  const navigate = useNavigate()
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(false)

  async function submit(e: FormEvent) {
    e.preventDefault()
    setError('')
    setLoading(true)
    try {
      const u = await api.auth.login(username, password)
      setUser(u as AuthUser)
      navigate('/')
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : 'Login failed')
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="flex h-screen items-center justify-center bg-blackberry-950">
      <div className="w-full max-w-sm rounded-lg border border-blackberry-700 bg-blackberry-900 p-8 shadow-lg">
        <h1 className="mb-6 text-center text-2xl font-light text-lychee-100">snapman</h1>
        <form onSubmit={submit} className="space-y-4">
          <div>
            <label className="mb-1 block text-sm font-medium text-lychee-300">Username</label>
            <input
              type="text"
              value={username}
              onChange={e => setUsername(e.target.value)}
              required
              autoFocus
              className="w-full rounded-md border border-blackberry-700 bg-blackberry-800 px-3 py-2 text-sm text-lychee-300 focus:outline-none focus:ring-2 focus:ring-agave-500/30 focus:border-agave-500"
            />
          </div>
          <div>
            <label className="mb-1 block text-sm font-medium text-lychee-300">Password</label>
            <input
              type="password"
              value={password}
              onChange={e => setPassword(e.target.value)}
              required
              className="w-full rounded-md border border-blackberry-700 bg-blackberry-800 px-3 py-2 text-sm text-lychee-300 focus:outline-none focus:ring-2 focus:ring-agave-500/30 focus:border-agave-500"
            />
          </div>
          {error && <p className="text-sm text-pomegranate-400">{error}</p>}
          <button
            type="submit"
            disabled={loading}
            className="w-full rounded-md bg-agave-500 py-2 text-sm font-normal text-blackberry-950 hover:bg-agave-600 disabled:opacity-50"
          >
            {loading ? 'Signing in…' : 'Sign in'}
          </button>
        </form>
      </div>
    </div>
  )
}
