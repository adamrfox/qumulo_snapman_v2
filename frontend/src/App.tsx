import { createContext, useContext, useEffect, useState } from 'react'
import { BrowserRouter, Navigate, Route, Routes } from 'react-router-dom'
import { api } from './api'
import type { AuthUser } from './types'
import Login from './pages/Login'
import Dashboard from './pages/Dashboard'
import InspectDetail from './pages/InspectDetail'
import Admin from './pages/Admin'
import Layout from './components/Layout'

interface AuthCtx {
  user: AuthUser | null
  setUser: (u: AuthUser | null) => void
}

export const AuthContext = createContext<AuthCtx>({ user: null, setUser: () => {} })
export const useAuth = () => useContext(AuthContext)

export default function App() {
  const [user, setUser] = useState<AuthUser | null>(null)
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    api.auth.me()
      .then(u => setUser(u as AuthUser))
      .catch(() => setUser(null))
      .finally(() => setLoading(false))
  }, [])

  if (loading) {
    return <div className="flex h-screen items-center justify-center bg-blackberry-950 text-lychee-400">Loading…</div>
  }

  return (
    <AuthContext.Provider value={{ user, setUser }}>
      <BrowserRouter>
        <Routes>
          <Route path="/login" element={user ? <Navigate to="/" /> : <Login />} />
          <Route element={user ? <Layout /> : <Navigate to="/login" />}>
            <Route path="/" element={<Dashboard />} />
            <Route path="/cluster/:clusterId/inspect/:sourceFileId" element={<InspectDetail />} />
            <Route
              path="/admin"
              element={user?.role === 'admin' ? <Admin /> : <Navigate to="/" />}
            />
          </Route>
          <Route path="*" element={<Navigate to="/" />} />
        </Routes>
      </BrowserRouter>
    </AuthContext.Provider>
  )
}
