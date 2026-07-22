import { useEffect, useRef, useState } from 'react'
import { useLocation, useNavigate } from 'react-router-dom'
import { api, ClusterAuthError, UnsupportedClusterVersionError } from '../api'
import type { Cluster, GoalReturnState, SnapshotGroup } from '../types'
import GoalModal from './GoalModal'

function fmtBytes(n: number): string {
  if (n === 0) return '—'
  const units = ['B', 'KiB', 'MiB', 'GiB', 'TiB']
  let v = n
  let i = 0
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i++ }
  return `${v.toFixed(2)} ${units[i]}`
}

export default function Dashboard() {
  const navigate = useNavigate()
  const location = useLocation()
  const [clusters, setClusters] = useState<Cluster[]>([])
  const [selectedId, setSelectedId] = useState<string | null>(location.state?.selectedClusterId ?? null)
  const [groups, setGroups] = useState<SnapshotGroup[]>([])
  const [clusterName, setClusterName] = useState('')
  const [loadingGroups, setLoadingGroups] = useState(false)
  const [error, setError] = useState('')
  const [authExpiredClusterId, setAuthExpiredClusterId] = useState<string | null>(null)
  const [unsupportedVersionClusterId, setUnsupportedVersionClusterId] = useState<string | null>(null)
  const [refreshing, setRefreshing] = useState(false)
  const [olderThanDays, setOlderThanDays] = useState(90)
  const [olderThanInput, setOlderThanInput] = useState('90')
  const [selectedTrees, setSelectedTrees] = useState<Set<string>>(new Set())
  const [warmTrees, setWarmTrees] = useState<Set<string>>(new Set())
  const [showGoalModal, setShowGoalModal] = useState(false)
  const [reopenGoal, setReopenGoal] = useState<GoalReturnState | null>(null)

  useEffect(() => {
    const reopen: GoalReturnState | undefined = location.state?.reopenGoal
    if (reopen) {
      setReopenGoal(reopen)
      setSelectedTrees(new Set(reopen.groups.map(g => g.source_file_id)))
      setShowGoalModal(true)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])
  const [showAddCluster, setShowAddCluster] = useState(false)
  const [authMode, setAuthMode] = useState<'token' | 'credentials'>('credentials')
  const [form, setForm] = useState({
    display_name: '', host: '', port: 8000, token: '', username: '', password: '', insecure: false,
  })
  const [addError, setAddError] = useState('')

  const [editingCluster, setEditingCluster] = useState<Cluster | null>(null)
  const [editAuthMode, setEditAuthMode] = useState<'token' | 'credentials'>('credentials')
  const [editForm, setEditForm] = useState({
    display_name: '', host: '', port: 8000, token: '', username: '', password: '', insecure: false,
  })
  const [editError, setEditError] = useState('')

  useEffect(() => {
    api.clusters.list().then(setClusters).catch(() => {})
  }, [])

  async function loadGroups(id: string, days: number) {
    setLoadingGroups(true)
    setError('')
    setAuthExpiredClusterId(null)
    setUnsupportedVersionClusterId(null)
    try {
      const r = await api.inspect.groups(id, days)
      setGroups(r.groups)
      setClusterName(r.cluster_name)
    } catch (e: unknown) {
      if (e instanceof ClusterAuthError) setAuthExpiredClusterId(id)
      if (e instanceof UnsupportedClusterVersionError) setUnsupportedVersionClusterId(id)
      setError(e instanceof Error ? e.message : 'Failed to load groups')
    } finally {
      setLoadingGroups(false)
    }
  }

  useEffect(() => {
    if (!selectedId) return
    loadGroups(selectedId, olderThanDays)
  }, [selectedId, olderThanDays])

  useEffect(() => {
    setSelectedTrees(new Set())
    setWarmTrees(new Set())
    if (!selectedId) return
    api.inspect.warmTrees(selectedId)
      .then(r => setWarmTrees(new Set(r.source_file_ids)))
      .catch(() => {})
  }, [selectedId])

  function toggleTree(sourceFileId: string) {
    setSelectedTrees(prev => {
      const next = new Set(prev)
      if (next.has(sourceFileId)) next.delete(sourceFileId)
      else next.add(sourceFileId)
      return next
    })
  }

  async function toggleWarmTree(sourceFileId: string) {
    if (!selectedId) return
    const isWarm = warmTrees.has(sourceFileId)
    try {
      if (isWarm) await api.inspect.removeWarmTree(selectedId, sourceFileId)
      else await api.inspect.addWarmTree(selectedId, sourceFileId)
      setWarmTrees(prev => {
        const next = new Set(prev)
        if (isWarm) next.delete(sourceFileId)
        else next.add(sourceFileId)
        return next
      })
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : 'Failed to update background inspect setting')
    }
  }

  function toggleSelectAllTrees() {
    setSelectedTrees(prev => {
      const allSelected = groups.length > 0 && groups.every(g => prev.has(g.source_file_id))
      return allSelected ? new Set() : new Set(groups.map(g => g.source_file_id))
    })
  }

  const allTreesSelected = groups.length > 0 && groups.every(g => selectedTrees.has(g.source_file_id))
  const someTreesSelected = groups.some(g => selectedTrees.has(g.source_file_id))
  const selectAllTreesRef = useRef<HTMLInputElement>(null)
  useEffect(() => {
    if (selectAllTreesRef.current) {
      selectAllTreesRef.current.indeterminate = someTreesSelected && !allTreesSelected
    }
  }, [someTreesSelected, allTreesSelected])

  function commitOlderThan() {
    const n = Number(olderThanInput)
    if (Number.isFinite(n) && n >= 0) {
      setOlderThanDays(n)
    } else {
      setOlderThanInput(String(olderThanDays))
    }
  }

  async function refreshCluster() {
    if (!selectedId) return
    setRefreshing(true)
    try {
      await api.clusters.refresh(selectedId)
      await loadGroups(selectedId, olderThanDays)
    } catch (err: unknown) {
      if (err instanceof ClusterAuthError) setAuthExpiredClusterId(selectedId)
      setError(err instanceof Error ? err.message : 'Failed to refresh')
    } finally {
      setRefreshing(false)
    }
  }

  async function addCluster(e: React.FormEvent) {
    e.preventDefault()
    setAddError('')
    try {
      const { display_name, host, port, insecure } = form
      const payload = authMode === 'token'
        ? { display_name, host, port, insecure, token: form.token }
        : { display_name, host, port, insecure, username: form.username, password: form.password }
      const c = await api.clusters.create(payload)
      setClusters(prev => [...prev, c])
      setShowAddCluster(false)
      setForm({ display_name: '', host: '', port: 8000, token: '', username: '', password: '', insecure: false })
      setSelectedId(c.id)
    } catch (err: unknown) {
      setAddError(err instanceof Error ? err.message : 'Failed to add cluster')
    }
  }

  function openEdit(c: Cluster) {
    setEditingCluster(c)
    setEditAuthMode('credentials')
    setEditForm({
      display_name: c.display_name, host: c.host, port: c.port,
      token: '', username: '', password: '', insecure: c.insecure,
    })
    setEditError('')
  }

  async function submitEdit(e: React.FormEvent) {
    e.preventDefault()
    if (!editingCluster) return
    setEditError('')
    try {
      const { display_name, host, port, insecure } = editForm
      const payload: Parameters<typeof api.clusters.update>[1] = { display_name, host, port, insecure }
      if (editAuthMode === 'token' && editForm.token) {
        payload.token = editForm.token
      } else if (editAuthMode === 'credentials' && editForm.username && editForm.password) {
        payload.username = editForm.username
        payload.password = editForm.password
      }
      const updated = await api.clusters.update(editingCluster.id, payload)
      setClusters(prev => prev.map(c => c.id === updated.id ? updated : c))
      setEditingCluster(null)
      if (updated.id === selectedId) {
        loadGroups(selectedId, olderThanDays)
      }
    } catch (err: unknown) {
      setEditError(err instanceof Error ? err.message : 'Failed to update cluster')
    }
  }

  async function deleteCluster(c: Cluster) {
    if (!window.confirm(`Remove "${c.display_name}"? This only removes it from snapman — it does not affect the Qumulo cluster.`)) return
    try {
      await api.clusters.delete(c.id)
      setClusters(prev => prev.filter(x => x.id !== c.id))
      if (selectedId === c.id) {
        setSelectedId(null)
        setGroups([])
      }
    } catch (err: unknown) {
      window.alert(err instanceof Error ? err.message : 'Failed to delete cluster')
    }
  }

  return (
    <div className="flex h-full">
      {/* Sidebar */}
      <aside className="w-56 flex-shrink-0 border-r border-blackberry-700 bg-blackberry-925">
        <div className="flex items-center justify-between border-b border-blackberry-700 px-4 py-3">
          <span className="text-sm font-medium text-lychee-100">Clusters</span>
          <button
            onClick={() => setShowAddCluster(true)}
            className="text-lg leading-none text-agave-400 hover:text-agave-500"
            title="Add cluster"
          >+</button>
        </div>
        <ul>
          {clusters.map(c => (
            <li key={c.id} className="group relative">
              <button
                onClick={() => setSelectedId(c.id)}
                className={`w-full px-4 py-2.5 pr-14 text-left text-sm ${
                  selectedId === c.id ? 'bg-blackberry-850 font-medium text-agave-400' : 'text-lychee-300 hover:bg-blackberry-850'
                }`}
              >
                <div className="truncate">{c.display_name}</div>
                <div className="truncate text-xs text-lychee-500">{c.host}</div>
              </button>
              <div className="absolute right-2 top-1/2 hidden -translate-y-1/2 gap-2 group-hover:flex">
                <button
                  onClick={() => openEdit(c)}
                  title="Edit cluster"
                  className="text-xs text-lychee-400 hover:text-agave-400"
                >
                  Edit
                </button>
                <button
                  onClick={() => deleteCluster(c)}
                  title="Delete cluster"
                  className="text-xs text-lychee-400 hover:text-pomegranate-400"
                >
                  Delete
                </button>
              </div>
            </li>
          ))}
          {clusters.length === 0 && (
            <li className="px-4 py-6 text-center text-xs text-lychee-500">No clusters yet</li>
          )}
        </ul>
      </aside>

      {/* Main */}
      <div className="flex-1 overflow-auto p-6">
        {!selectedId && (
          <div className="flex h-48 items-center justify-center text-lychee-500">
            Select a cluster or add one to get started
          </div>
        )}

        {selectedId && (
          <>
            <div className="mb-4 flex items-center justify-between">
              <div>
                <h2 className="text-lg font-light text-lychee-100">{clusterName || '…'}</h2>
                <p className="text-sm text-lychee-400">Snapshot groups — click a row to inspect</p>
              </div>
              <div className="flex items-center gap-3">
                <label className="flex items-center gap-1.5 text-xs text-lychee-400" title="Prunable, Measured, and Reclaim~ below all use this cutoff — snapshots older than this many days, walked from the oldest until a locked one or one inside the window is hit">
                  Older than
                  <input
                    type="number"
                    min={0}
                    value={olderThanInput}
                    onChange={e => setOlderThanInput(e.target.value)}
                    onBlur={commitOlderThan}
                    onKeyDown={e => { if (e.key === 'Enter') (e.target as HTMLInputElement).blur() }}
                    className="w-16 rounded-md border border-blackberry-700 bg-blackberry-800 px-2 py-1 text-xs text-lychee-300 focus:outline-none focus:ring-2 focus:ring-agave-500/30 focus:border-agave-500"
                  />
                  days
                </label>
                <button
                  onClick={refreshCluster}
                  disabled={refreshing}
                  title="Bypass the 5-minute cache and re-fetch the snapshot list from the cluster now"
                  className="rounded-md border border-blackberry-700 px-3 py-1.5 text-xs text-lychee-300 hover:bg-blackberry-850 disabled:opacity-40"
                >
                  {refreshing ? 'Refreshing…' : 'Refresh'}
                </button>
                <button
                  onClick={() => setShowGoalModal(true)}
                  disabled={selectedTrees.size === 0}
                  title={selectedTrees.size === 0 ? 'Check one or more trees below first' : undefined}
                  className="rounded-md bg-agave-500 px-3 py-1.5 text-xs text-blackberry-950 hover:bg-agave-600 disabled:opacity-40"
                >
                  Solve for a space goal{selectedTrees.size > 0 ? ` (${selectedTrees.size})` : ''}
                </button>
              </div>
            </div>

            {loadingGroups && <p className="text-sm text-lychee-400">Loading groups…</p>}
            {authExpiredClusterId === selectedId ? (
              <div className="mb-4 flex items-center justify-between rounded-md border border-pomegranate-700 bg-pomegranate-700/20 px-4 py-3 text-sm text-pomegranate-400">
                <span>This cluster's stored credentials have expired or are no longer valid.</span>
                <div className="flex flex-shrink-0 gap-2">
                  <button
                    onClick={() => {
                      const c = clusters.find(x => x.id === selectedId)
                      if (c) openEdit(c)
                    }}
                    className="rounded-md bg-agave-500 px-3 py-1 text-xs text-blackberry-950 hover:bg-agave-600"
                  >
                    Update credentials
                  </button>
                  <button
                    onClick={() => setAuthExpiredClusterId(null)}
                    className="rounded-md px-3 py-1 text-xs text-lychee-300 hover:bg-blackberry-850"
                  >
                    Dismiss
                  </button>
                </div>
              </div>
            ) : unsupportedVersionClusterId === selectedId ? (
              <div className="mb-4 flex items-center justify-between rounded-md border border-kumquat-700 bg-kumquat-700/20 px-4 py-3 text-sm text-kumquat-400">
                <span>{error}</span>
                <button
                  onClick={() => setUnsupportedVersionClusterId(null)}
                  className="flex-shrink-0 rounded-md px-3 py-1 text-xs text-lychee-300 hover:bg-blackberry-850"
                >
                  Dismiss
                </button>
              </div>
            ) : (
              error && <p className="text-sm text-pomegranate-400">{error}</p>
            )}

            {!loadingGroups && !error && groups.length > 0 && (
              <div className="overflow-x-auto rounded-lg border border-blackberry-700 bg-blackberry-900 shadow-md">
                <table className="w-full text-sm">
                  <thead>
                    <tr className="border-b border-blackberry-700 bg-blackberry-800 text-left text-xs font-medium uppercase text-lychee-100">
                      <th className="px-4 py-3">
                        <input
                          ref={selectAllTreesRef}
                          type="checkbox"
                          checked={allTreesSelected}
                          disabled={groups.length === 0}
                          onChange={toggleSelectAllTrees}
                          title="Select all"
                        />
                      </th>
                      <th className="px-4 py-3">Path</th>
                      <th className="px-4 py-3 text-right">Snaps</th>
                      <th className="px-4 py-3 text-right">Oldest</th>
                      <th className="px-4 py-3 text-right">Prunable</th>
                      <th className="px-4 py-3 text-right">Measured</th>
                      <th className="px-4 py-3 text-right">Reclaim~</th>
                      <th className="px-4 py-3 text-center" title="Automatically keep this tree's reclaim curve refreshed in the background, even when nobody has the app open">Keep warm</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-blackberry-700">
                    {groups
                      .sort((a, b) => b.reclaim_bytes - a.reclaim_bytes)
                      .map(g => (
                        <tr
                          key={g.source_file_id}
                          onClick={() => navigate(`/cluster/${selectedId}/inspect/${g.source_file_id}`, { state: { group: g, clusterName } })}
                          className="cursor-pointer text-lychee-300 hover:bg-blackberry-850"
                        >
                          <td className="px-4 py-3" onClick={e => e.stopPropagation()}>
                            <input
                              type="checkbox"
                              checked={selectedTrees.has(g.source_file_id)}
                              onChange={() => toggleTree(g.source_file_id)}
                            />
                          </td>
                          <td className="px-4 py-3 font-mono text-xs">{g.path}</td>
                          <td className="px-4 py-3 text-right">{g.count.toLocaleString()}</td>
                          <td className="px-4 py-3 text-right">{g.max_age_days}d</td>
                          <td className="px-4 py-3 text-right">{g.prunable}</td>
                          <td className="px-4 py-3 text-right">
                            {g.prunable === 0
                              ? <span className="text-lychee-500" title="Nothing in this tree is older than the cutoff yet, so there's nothing prunable to measure a percentage of.">n/a</span>
                              : g.total_pairs > 0
                              ? `${Math.round((g.measured_pairs / g.total_pairs) * 100)}%`
                              : '—'}
                          </td>
                          <td className="px-4 py-3 text-right font-medium">
                            {g.prunable === 0
                              ? <span className="text-lychee-500" title="Nothing in this tree is older than the cutoff yet — this isn't the same as unmeasured. Open the tree and check Snapshot sizes for what's been measured so far.">n/a</span>
                              : g.reclaim_bytes > 0
                              ? <span className="text-kiwi-400">{g.is_upper_bound ? '≤ ' : ''}{fmtBytes(g.reclaim_bytes)}</span>
                              : g.measured_pairs === 0
                              ? <span className="text-lychee-500">not measured</span>
                              : '—'}
                          </td>
                          <td className="px-4 py-3 text-center" onClick={e => e.stopPropagation()}>
                            <input
                              type="checkbox"
                              checked={warmTrees.has(g.source_file_id)}
                              onChange={() => toggleWarmTree(g.source_file_id)}
                              title="Automatically keep this tree's reclaim curve refreshed in the background, even when nobody has the app open"
                            />
                          </td>
                        </tr>
                      ))}
                  </tbody>
                </table>
              </div>
            )}
          </>
        )}
      </div>

      {/* Add cluster modal */}
      {showAddCluster && (
        <div className="fixed inset-0 flex items-center justify-center bg-black/40 p-4">
          <div className="w-full max-w-md rounded-lg border border-blackberry-700 bg-blackberry-900 p-6 shadow-xl">
            <h3 className="mb-4 text-base font-semibold text-lychee-100">Add cluster</h3>
            <form onSubmit={addCluster} className="space-y-3">
              <div>
                <label className="mb-1 block text-xs font-medium text-lychee-400">Display name</label>
                <input
                  value={form.display_name}
                  onChange={e => setForm(f => ({ ...f, display_name: e.target.value }))}
                  required
                  className="w-full rounded-md border border-blackberry-700 bg-blackberry-800 px-3 py-1.5 text-sm text-lychee-300 focus:outline-none focus:ring-2 focus:ring-agave-500/30 focus:border-agave-500"
                />
              </div>
              <div className="flex gap-2">
                <div className="flex-1">
                  <label className="mb-1 block text-xs font-medium text-lychee-400">Host</label>
                  <input
                    value={form.host}
                    onChange={e => setForm(f => ({ ...f, host: e.target.value }))}
                    required
                    placeholder="cluster.example.com"
                    className="w-full rounded-md border border-blackberry-700 bg-blackberry-800 px-3 py-1.5 text-sm text-lychee-300 focus:outline-none focus:ring-2 focus:ring-agave-500/30 focus:border-agave-500"
                  />
                </div>
                <div className="w-20">
                  <label className="mb-1 block text-xs font-medium text-lychee-400">Port</label>
                  <input
                    type="number"
                    value={form.port}
                    onChange={e => setForm(f => ({ ...f, port: Number(e.target.value) }))}
                    className="w-full rounded-md border border-blackberry-700 bg-blackberry-800 px-3 py-1.5 text-sm text-lychee-300 focus:outline-none focus:ring-2 focus:ring-agave-500/30 focus:border-agave-500"
                  />
                </div>
              </div>
              <div className="flex gap-1 rounded-md border border-blackberry-700 bg-blackberry-800 p-1 text-xs">
                <button
                  type="button"
                  onClick={() => setAuthMode('credentials')}
                  className={`flex-1 rounded px-2 py-1 ${authMode === 'credentials' ? 'bg-agave-500 text-blackberry-950' : 'text-lychee-300 hover:bg-blackberry-850'}`}
                >
                  Username &amp; password
                </button>
                <button
                  type="button"
                  onClick={() => setAuthMode('token')}
                  className={`flex-1 rounded px-2 py-1 ${authMode === 'token' ? 'bg-agave-500 text-blackberry-950' : 'text-lychee-300 hover:bg-blackberry-850'}`}
                >
                  Bearer token
                </button>
              </div>

              {authMode === 'credentials' ? (
                <div className="flex gap-2">
                  <div className="flex-1">
                    <label className="mb-1 block text-xs font-medium text-lychee-400">Username</label>
                    <input
                      value={form.username}
                      onChange={e => setForm(f => ({ ...f, username: e.target.value }))}
                      required
                      className="w-full rounded-md border border-blackberry-700 bg-blackberry-800 px-3 py-1.5 text-sm text-lychee-300 focus:outline-none focus:ring-2 focus:ring-agave-500/30 focus:border-agave-500"
                    />
                  </div>
                  <div className="flex-1">
                    <label className="mb-1 block text-xs font-medium text-lychee-400">Password</label>
                    <input
                      type="password"
                      value={form.password}
                      onChange={e => setForm(f => ({ ...f, password: e.target.value }))}
                      required
                      className="w-full rounded-md border border-blackberry-700 bg-blackberry-800 px-3 py-1.5 text-sm text-lychee-300 focus:outline-none focus:ring-2 focus:ring-agave-500/30 focus:border-agave-500"
                    />
                  </div>
                </div>
              ) : (
                <div>
                  <label className="mb-1 block text-xs font-medium text-lychee-400">Bearer token</label>
                  <input
                    type="password"
                    value={form.token}
                    onChange={e => setForm(f => ({ ...f, token: e.target.value }))}
                    required
                    className="w-full rounded-md border border-blackberry-700 bg-blackberry-800 px-3 py-1.5 text-sm text-lychee-300 focus:outline-none focus:ring-2 focus:ring-agave-500/30 focus:border-agave-500"
                  />
                </div>
              )}
              <label className="flex items-center gap-2 text-sm text-lychee-300">
                <input
                  type="checkbox"
                  checked={form.insecure}
                  onChange={e => setForm(f => ({ ...f, insecure: e.target.checked }))}
                />
                Skip TLS certificate verification
              </label>
              {addError && <p className="text-xs text-pomegranate-400">{addError}</p>}
              <div className="flex justify-end gap-2 pt-2">
                <button
                  type="button"
                  onClick={() => setShowAddCluster(false)}
                  className="rounded-md px-4 py-1.5 text-sm text-lychee-300 hover:bg-blackberry-850"
                >
                  Cancel
                </button>
                <button
                  type="submit"
                  className="rounded-md bg-agave-500 px-4 py-1.5 text-sm text-blackberry-950 hover:bg-agave-600"
                >
                  Add
                </button>
              </div>
            </form>
          </div>
        </div>
      )}

      {/* Edit cluster modal */}
      {editingCluster && (
        <div className="fixed inset-0 flex items-center justify-center bg-black/40 p-4">
          <div className="w-full max-w-md rounded-lg border border-blackberry-700 bg-blackberry-900 p-6 shadow-xl">
            <h3 className="mb-4 text-base font-semibold text-lychee-100">Edit cluster</h3>
            <form onSubmit={submitEdit} className="space-y-3">
              <div>
                <label className="mb-1 block text-xs font-medium text-lychee-400">Display name</label>
                <input
                  value={editForm.display_name}
                  onChange={e => setEditForm(f => ({ ...f, display_name: e.target.value }))}
                  required
                  className="w-full rounded-md border border-blackberry-700 bg-blackberry-800 px-3 py-1.5 text-sm text-lychee-300 focus:outline-none focus:ring-2 focus:ring-agave-500/30 focus:border-agave-500"
                />
              </div>
              <div className="flex gap-2">
                <div className="flex-1">
                  <label className="mb-1 block text-xs font-medium text-lychee-400">Host</label>
                  <input
                    value={editForm.host}
                    onChange={e => setEditForm(f => ({ ...f, host: e.target.value }))}
                    required
                    className="w-full rounded-md border border-blackberry-700 bg-blackberry-800 px-3 py-1.5 text-sm text-lychee-300 focus:outline-none focus:ring-2 focus:ring-agave-500/30 focus:border-agave-500"
                  />
                </div>
                <div className="w-20">
                  <label className="mb-1 block text-xs font-medium text-lychee-400">Port</label>
                  <input
                    type="number"
                    value={editForm.port}
                    onChange={e => setEditForm(f => ({ ...f, port: Number(e.target.value) }))}
                    className="w-full rounded-md border border-blackberry-700 bg-blackberry-800 px-3 py-1.5 text-sm text-lychee-300 focus:outline-none focus:ring-2 focus:ring-agave-500/30 focus:border-agave-500"
                  />
                </div>
              </div>

              <p className="text-xs text-lychee-500">Leave the fields below blank to keep the current token.</p>
              <div className="flex gap-1 rounded-md border border-blackberry-700 bg-blackberry-800 p-1 text-xs">
                <button
                  type="button"
                  onClick={() => setEditAuthMode('credentials')}
                  className={`flex-1 rounded px-2 py-1 ${editAuthMode === 'credentials' ? 'bg-agave-500 text-blackberry-950' : 'text-lychee-300 hover:bg-blackberry-850'}`}
                >
                  Username &amp; password
                </button>
                <button
                  type="button"
                  onClick={() => setEditAuthMode('token')}
                  className={`flex-1 rounded px-2 py-1 ${editAuthMode === 'token' ? 'bg-agave-500 text-blackberry-950' : 'text-lychee-300 hover:bg-blackberry-850'}`}
                >
                  Bearer token
                </button>
              </div>

              {editAuthMode === 'credentials' ? (
                <div className="flex gap-2">
                  <div className="flex-1">
                    <label className="mb-1 block text-xs font-medium text-lychee-400">Username</label>
                    <input
                      value={editForm.username}
                      onChange={e => setEditForm(f => ({ ...f, username: e.target.value }))}
                      className="w-full rounded-md border border-blackberry-700 bg-blackberry-800 px-3 py-1.5 text-sm text-lychee-300 focus:outline-none focus:ring-2 focus:ring-agave-500/30 focus:border-agave-500"
                    />
                  </div>
                  <div className="flex-1">
                    <label className="mb-1 block text-xs font-medium text-lychee-400">Password</label>
                    <input
                      type="password"
                      value={editForm.password}
                      onChange={e => setEditForm(f => ({ ...f, password: e.target.value }))}
                      className="w-full rounded-md border border-blackberry-700 bg-blackberry-800 px-3 py-1.5 text-sm text-lychee-300 focus:outline-none focus:ring-2 focus:ring-agave-500/30 focus:border-agave-500"
                    />
                  </div>
                </div>
              ) : (
                <div>
                  <label className="mb-1 block text-xs font-medium text-lychee-400">Bearer token</label>
                  <input
                    type="password"
                    value={editForm.token}
                    onChange={e => setEditForm(f => ({ ...f, token: e.target.value }))}
                    className="w-full rounded-md border border-blackberry-700 bg-blackberry-800 px-3 py-1.5 text-sm text-lychee-300 focus:outline-none focus:ring-2 focus:ring-agave-500/30 focus:border-agave-500"
                  />
                </div>
              )}
              <label className="flex items-center gap-2 text-sm text-lychee-300">
                <input
                  type="checkbox"
                  checked={editForm.insecure}
                  onChange={e => setEditForm(f => ({ ...f, insecure: e.target.checked }))}
                />
                Skip TLS certificate verification
              </label>
              {editError && <p className="text-xs text-pomegranate-400">{editError}</p>}
              <div className="flex justify-end gap-2 pt-2">
                <button
                  type="button"
                  onClick={() => setEditingCluster(null)}
                  className="rounded-md px-4 py-1.5 text-sm text-lychee-300 hover:bg-blackberry-850"
                >
                  Cancel
                </button>
                <button
                  type="submit"
                  className="rounded-md bg-agave-500 px-4 py-1.5 text-sm text-blackberry-950 hover:bg-agave-600"
                >
                  Save
                </button>
              </div>
            </form>
          </div>
        </div>
      )}

      {showGoalModal && selectedId && (
        <GoalModal
          clusterId={selectedId}
          clusterName={clusterName}
          groups={reopenGoal ? reopenGoal.groups : groups.filter(g => selectedTrees.has(g.source_file_id))}
          initialResult={reopenGoal?.result}
          initialSkipped={reopenGoal?.skipped}
          initialHandledIds={reopenGoal?.handledIds}
          initialExcludedIds={reopenGoal?.excludedIds}
          onClose={() => { setShowGoalModal(false); setReopenGoal(null) }}
          onDeselect={sourceFileId => setSelectedTrees(prev => {
            const next = new Set(prev)
            next.delete(sourceFileId)
            return next
          })}
        />
      )}
    </div>
  )
}
