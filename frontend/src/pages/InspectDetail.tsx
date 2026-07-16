import { useEffect, useRef, useState } from 'react'
import { useLocation, useNavigate, useParams } from 'react-router-dom'
import { api } from '../api'
import type { CurvePoint, ReclaimRow, SnapshotGroup } from '../types'
import { useAuth } from '../App'

function fmtBytes(n: number | null): string {
  if (n === null || n === 0) return '—'
  const units = ['B', 'KiB', 'MiB', 'GiB', 'TiB']
  let v = n
  let i = 0
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i++ }
  return `${v.toFixed(2)} ${units[i]}`
}

interface InspectState {
  index: number
  total: number
  found: number
  sized: number
  discovered: number
}

export default function InspectDetail() {
  const { clusterId, sourceFileId } = useParams<{ clusterId: string; sourceFileId: string }>()
  const location = useLocation()
  const navigate = useNavigate()
  const { user } = useAuth()

  const group: SnapshotGroup | undefined = location.state?.group
  const clusterName: string = location.state?.clusterName ?? ''

  const [rows, setRows] = useState<ReclaimRow[]>([])
  const [unmeasured, setUnmeasured] = useState(0)
  const [running, setRunning] = useState(false)
  const [progress, setProgress] = useState<InspectState | null>(null)
  const [statusMsg, setStatusMsg] = useState('')
  const [deleteTarget, setDeleteTarget] = useState<ReclaimRow | null>(null)
  const [deleteConfirm, setDeleteConfirm] = useState('')
  const [deleteResult, setDeleteResult] = useState<{ deleted: number[]; errors: { id: number; error: string }[] } | null>(null)
  const esRef = useRef<EventSource | null>(null)
  const jobIdRef = useRef<string | null>(null)
  const cancelRequestedRef = useRef(false)

  useEffect(() => {
    if (!clusterId || !sourceFileId) return
    api.inspect.curve(clusterId, sourceFileId)
      .then(r => { setRows(r.rows); setUnmeasured(r.unmeasured_pairs) })
      .catch(() => {})
  }, [clusterId, sourceFileId])

  async function startInspect() {
    if (!clusterId || !sourceFileId || !group) return
    setRunning(true)
    setStatusMsg('Starting…')
    esRef.current?.close()
    jobIdRef.current = null
    cancelRequestedRef.current = false

    try {
      const { job_id } = await api.inspect.startInspect(clusterId, sourceFileId, group.path)
      jobIdRef.current = job_id
      if (cancelRequestedRef.current) {
        api.inspect.cancelInspect(clusterId, job_id).catch(() => {})
      }
      const es = new EventSource(`/api/clusters/${clusterId}/jobs/${job_id}/stream`, { withCredentials: true })
      esRef.current = es

      let discovered = 0
      const newPoints: CurvePoint[] = []

      es.onmessage = (evt) => {
        const msg = JSON.parse(evt.data)
        switch (msg.type) {
          case 'pair_start':
            setProgress(p => ({ index: msg.index, total: msg.total, found: p?.found ?? 0, sized: p?.sized ?? 0, discovered }))
            setStatusMsg(`Measuring pair ${msg.index}/${msg.total} (${msg.older_date} → ${msg.newer_date})`)
            break
          case 'progress':
            setProgress(p => ({ ...p!, found: msg.found, sized: msg.sized }))
            break
          case 'discovered':
            discovered += msg.freed_bytes
            setProgress(p => p ? { ...p, discovered } : null)
            break
          case 'pair_result':
            newPoints.push({
              older_id: msg.older_id, older_date: msg.older_date, older_name: msg.older_name,
              newer_id: msg.newer_id, newer_date: msg.newer_date,
              freed_bytes: msg.freed_bytes, cumulative_bytes: msg.cumulative_bytes,
              total_files: msg.total_files,
              status: msg.pending ? 'pending' : msg.cached ? 'cached' : 'computed',
            })
            break
          case 'finish':
            es.close()
            setRunning(false)
            setStatusMsg('Done — reloading curve…')
            api.inspect.curve(clusterId!, sourceFileId!)
              .then(r => { setRows(r.rows); setUnmeasured(r.unmeasured_pairs); setStatusMsg('') })
            break
          case 'error':
            es.close()
            setRunning(false)
            setStatusMsg(`Error: ${msg.message}`)
            break
          case 'no_curve':
            es.close()
            setRunning(false)
            setStatusMsg('Only one snapshot — nothing to measure.')
            break
        }
      }
      es.onerror = () => {
        es.close()
        setRunning(false)
        setStatusMsg('Stream disconnected.')
      }
    } catch (err: unknown) {
      setRunning(false)
      setStatusMsg(err instanceof Error ? err.message : 'Failed to start')
    }
  }

  function stopInspect() {
    esRef.current?.close()
    cancelRequestedRef.current = true
    setRunning(false)
    setStatusMsg('Stopping…')

    const jobId = jobIdRef.current
    if (!clusterId || !sourceFileId || !jobId) return
    api.inspect.cancelInspect(clusterId, jobId)
      .catch(() => {})
      .finally(() => {
        api.inspect.curve(clusterId, sourceFileId)
          .then(r => {
            setRows(r.rows)
            setUnmeasured(r.unmeasured_pairs)
            setStatusMsg('Stopped. Progress so far is saved — click Inspect again to resume.')
          })
          .catch(() => {})
      })
  }

  async function doDelete() {
    if (!clusterId || !deleteTarget || !sourceFileId) return
    const { snapshot_ids } = await api.inspect.olderThan(clusterId, sourceFileId, deleteTarget.delete_before)
    const result = await api.inspect.deleteSnapshots(clusterId, snapshot_ids)
    setDeleteResult(result)
    setDeleteTarget(null)
  }

  const canDelete = user?.role === 'admin' || user?.role === 'operator'

  return (
    <div className="p-6">
      <div className="mb-4 flex items-center justify-between">
        <div>
          <button
            onClick={() => navigate('/', { state: { selectedClusterId: clusterId } })}
            className="mb-1 text-sm text-agave-400 hover:underline"
          >
            ← Back
          </button>
          <h2 className="font-mono text-base font-semibold text-lychee-100">{group?.path ?? sourceFileId}</h2>
          {clusterName && <p className="text-xs text-lychee-500">{clusterName}</p>}
        </div>
        <div className="flex gap-2">
          {!running ? (
            <button
              onClick={startInspect}
              className="rounded-md bg-agave-500 px-4 py-1.5 text-sm text-blackberry-950 hover:bg-agave-600"
            >
              {rows.length > 0 ? 'Re-inspect' : 'Inspect'}
            </button>
          ) : (
            <button
              onClick={stopInspect}
              className="rounded-md bg-kumquat-500 px-4 py-1.5 text-sm text-blackberry-950 hover:bg-kumquat-600"
            >
              Stop
            </button>
          )}
        </div>
      </div>

      {/* Progress */}
      {running && progress && (
        <div className="mb-4 rounded-lg border border-blackberry-700 bg-blackberry-900 p-4 text-sm">
          <p className="mb-2 font-medium text-lychee-100">{statusMsg}</p>
          <div className="mb-1 flex justify-between text-xs text-lychee-400">
            <span>Pair {progress.index}/{progress.total}</span>
            <span>{progress.found} files found · {progress.sized} sized</span>
          </div>
          <div className="h-2 rounded-full bg-blackberry-800">
            <div
              className="h-2 rounded-full bg-agave-500 transition-all"
              style={{ width: `${Math.round((progress.index / Math.max(progress.total, 1)) * 100)}%` }}
            />
          </div>
          {progress.discovered > 0 && (
            <p className="mt-2 text-xs text-lychee-400">Discovered so far: {fmtBytes(progress.discovered)}</p>
          )}
        </div>
      )}
      {statusMsg && !running && (
        <p className="mb-4 text-sm text-lychee-400">{statusMsg}</p>
      )}

      {/* Reclaim curve table */}
      {rows.length > 0 && (
        <div className="mb-6 overflow-x-auto rounded-lg border border-blackberry-700 bg-blackberry-900 shadow-md">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-blackberry-700 bg-blackberry-800 text-left text-xs font-medium uppercase text-lychee-100">
                <th className="px-4 py-3">Keep</th>
                <th className="px-4 py-3">Delete before</th>
                <th className="px-4 py-3 text-right">Deletes</th>
                <th className="px-4 py-3 text-right">Reclaims</th>
                {canDelete && <th className="px-4 py-3"></th>}
              </tr>
            </thead>
            <tbody className="divide-y divide-blackberry-700">
              {rows.map((row, i) => (
                <tr key={i} className="text-lychee-300 hover:bg-blackberry-850">
                  <td className="px-4 py-3">last {row.keep_days}d</td>
                  <td className="px-4 py-3 font-mono text-xs">{row.delete_before}</td>
                  <td className="px-4 py-3 text-right">{row.delete_count} snaps</td>
                  <td className="px-4 py-3 text-right font-medium text-kiwi-400">
                    {fmtBytes(row.reclaim_bytes)}
                  </td>
                  {canDelete && (
                    <td className="px-4 py-3 text-right">
                      <button
                        onClick={() => { setDeleteTarget(row); setDeleteConfirm('') }}
                        className="rounded-md bg-pomegranate-600 px-3 py-1 text-xs text-lychee-50 hover:bg-pomegranate-700"
                      >
                        Delete
                      </button>
                    </td>
                  )}
                </tr>
              ))}
            </tbody>
          </table>
          {unmeasured > 0 && (
            <p className="px-4 py-2 text-xs text-lychee-500">
              {unmeasured} pair{unmeasured > 1 ? 's' : ''} not yet measured — run Inspect to get a full curve
            </p>
          )}
        </div>
      )}

      {rows.length === 0 && !running && (
        <div className="rounded-lg border border-blackberry-700 bg-blackberry-900 p-8 text-center text-sm text-lychee-500">
          No measurements yet — click Inspect to start
        </div>
      )}

      {/* Delete result */}
      {deleteResult && (
        <div className="mb-4 rounded-lg border border-blackberry-700 bg-blackberry-900 p-4 text-sm">
          <p className="font-medium text-lychee-100">
            Deleted {deleteResult.deleted.length} snapshot{deleteResult.deleted.length !== 1 ? 's' : ''}
            {deleteResult.errors.length > 0 && ` · ${deleteResult.errors.length} error(s)`}
          </p>
          {deleteResult.errors.map((e, i) => (
            <p key={i} className="mt-1 text-xs text-pomegranate-400">Snapshot {e.id}: {e.error}</p>
          ))}
          <button onClick={() => setDeleteResult(null)} className="mt-2 text-xs text-agave-400 hover:underline">
            Dismiss
          </button>
        </div>
      )}

      {/* Delete confirmation modal */}
      {deleteTarget && (
        <div className="fixed inset-0 flex items-center justify-center bg-black/40 p-4">
          <div className="w-full max-w-md rounded-lg border border-blackberry-700 bg-blackberry-900 p-6 shadow-xl">
            <h3 className="mb-2 text-base font-semibold text-pomegranate-400">Confirm deletion</h3>
            <p className="mb-4 text-sm text-lychee-300">
              This will delete <strong className="text-lychee-100">{deleteTarget.delete_count} snapshots</strong> older than{' '}
              <strong className="text-lychee-100">{deleteTarget.delete_before}</strong>, freeing approximately{' '}
              <strong className="text-lychee-100">{fmtBytes(deleteTarget.reclaim_bytes)}</strong>.
              This cannot be undone.
            </p>
            <p className="mb-2 text-sm text-lychee-300">
              Type <code className="rounded bg-blackberry-800 px-1">delete {deleteTarget.delete_count} snapshots</code> to confirm:
            </p>
            <input
              value={deleteConfirm}
              onChange={e => setDeleteConfirm(e.target.value)}
              className="mb-4 w-full rounded-md border border-blackberry-700 bg-blackberry-800 px-3 py-2 font-mono text-sm text-lychee-300 focus:outline-none focus:ring-2 focus:ring-agave-500/30 focus:border-agave-500"
              autoFocus
            />
            <div className="flex justify-end gap-2">
              <button
                onClick={() => setDeleteTarget(null)}
                className="rounded-md px-4 py-1.5 text-sm text-lychee-300 hover:bg-blackberry-850"
              >
                Cancel
              </button>
              <button
                disabled={deleteConfirm !== `delete ${deleteTarget.delete_count} snapshots`}
                onClick={doDelete}
                className="rounded-md bg-pomegranate-600 px-4 py-1.5 text-sm text-lychee-50 hover:bg-pomegranate-700 disabled:opacity-40"
              >
                Delete
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
