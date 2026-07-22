import { useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { api, ClusterAuthError, UnsupportedClusterVersionError } from '../api'
import type { GoalResult, GoalSkippedTree, SnapshotGroup } from '../types'

function fmtBytes(n: number): string {
  if (n === 0) return '0 B'
  const units = ['B', 'KiB', 'MiB', 'GiB', 'TiB']
  let v = n
  let i = 0
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i++ }
  return `${v.toFixed(2)} ${units[i]}`
}

const UNITS = { GiB: 1 << 30, TiB: 1024 * (1 << 30) } as const
type Unit = keyof typeof UNITS

// Mirrors backend/app/qumulo/paths.py's is_ancestor/paths_nest -- same
// "startsWith the prefix, slash-normalized" rule the server uses for
// is_upper_bound. Recomputed client-side against only the *currently
// selected* trees rather than trusting is_upper_bound directly, since that
// field reflects overlap against every tree in the whole cluster -- it
// doesn't drop away just because the specific tree it was conflicting with
// got removed from this selection.
function isAncestor(anc: string, desc: string): boolean {
  return desc === anc || desc.startsWith(anc.replace(/\/+$/, '') + '/')
}

function pathsNest(a: string, b: string): boolean {
  return isAncestor(a, b) || isAncestor(b, a)
}

interface Props {
  clusterId: string
  clusterName: string
  groups: SnapshotGroup[]
  onClose: () => void
  onDeselect: (sourceFileId: string) => void
  initialResult?: GoalResult
  initialSkipped?: GoalSkippedTree[]
  initialHandledIds?: string[]
}

type Phase = 'input' | 'running' | 'results'

export default function GoalModal({
  clusterId, clusterName, groups, onClose, onDeselect,
  initialResult, initialSkipped, initialHandledIds,
}: Props) {
  const navigate = useNavigate()
  const [phase, setPhase] = useState<Phase>(initialResult ? 'results' : 'input')
  const [amount, setAmount] = useState('')
  const [unit, setUnit] = useState<Unit>('GiB')
  const [error, setError] = useState('')
  const [statusMsg, setStatusMsg] = useState('')
  const [currentTree, setCurrentTree] = useState<{ index: number; total: number; path: string } | null>(null)
  const [subProgress, setSubProgress] = useState<{ found: number; sized: number } | null>(null)
  const [skipped, setSkipped] = useState<GoalSkippedTree[]>(initialSkipped ?? [])
  const [result, setResult] = useState<GoalResult | null>(initialResult ?? null)
  const [handledIds, setHandledIds] = useState<Set<string>>(new Set(initialHandledIds ?? []))
  const esRef = useRef<EventSource | null>(null)
  const jobIdRef = useRef<string | null>(null)

  const pathFor = (sourceFileId: string) =>
    groups.find(g => g.source_file_id === sourceFileId)?.path ?? sourceFileId

  const conflictIds = new Set(
    groups
      .filter(g => groups.some(o => o.source_file_id !== g.source_file_id && pathsNest(g.path, o.path)))
      .map(g => g.source_file_id)
  )
  const overlapping = groups.filter(g => conflictIds.has(g.source_file_id))

  // Depth = how many other selected trees' paths are an ancestor of this
  // one -- a plain integer, not a real tree structure, but combined with a
  // lexicographic path sort it's enough to render nested paths indented
  // under their ancestors so the *reason* something is flagged is visible
  // at a glance instead of just a flat, unexplained list of paths.
  const treeRows = [...groups]
    .sort((a, b) => a.path.localeCompare(b.path))
    .map(group => ({
      group,
      depth: groups.filter(o => o.source_file_id !== group.source_file_id && isAncestor(o.path, group.path)).length,
    }))

  function targetBytes(): number | null {
    const n = Number(amount)
    if (!Number.isFinite(n) || n <= 0) return null
    return Math.round(n * UNITS[unit])
  }

  async function solve() {
    const bytes = targetBytes()
    if (bytes === null) {
      setError('Enter a positive amount')
      return
    }
    setError('')
    setSkipped([])
    setResult(null)
    setHandledIds(new Set())
    setCurrentTree(null)
    setSubProgress(null)
    setPhase('running')
    setStatusMsg('Starting…')

    try {
      const { job_id } = await api.inspect.startGoal(clusterId, groups.map(g => g.source_file_id), bytes)
      jobIdRef.current = job_id
      const es = new EventSource(`/api/clusters/${clusterId}/jobs/${job_id}/stream`, { withCredentials: true })
      esRef.current = es

      es.onmessage = (evt) => {
        const msg = JSON.parse(evt.data)
        switch (msg.type) {
          case 'tree_start':
            setCurrentTree({ index: msg.index, total: msg.total, path: msg.path })
            setSubProgress(null)
            setStatusMsg(`Checking tree ${msg.index + 1} of ${msg.total}…`)
            break
          case 'tree_measured':
            setStatusMsg('Already measured — moving on…')
            break
          case 'tree_skipped':
            setSkipped(prev => [...prev, { source_file_id: msg.source_file_id, reason: msg.reason }])
            break
          case 'inspect_progress':
            if (msg.waiting) {
              setStatusMsg('Waiting for an in-progress Inspect run on this tree…')
            } else if (msg.event?.type === 'pair_start') {
              setStatusMsg(`Inspecting — measuring ${msg.event.total} pairs…`)
              setSubProgress({ found: 0, sized: 0 })
            } else if (msg.event?.type === 'progress') {
              setSubProgress({ found: msg.event.found, sized: msg.event.sized })
            }
            break
          case 'finish':
            es.close()
            setResult(msg.result)
            setPhase('results')
            break
          case 'error':
            es.close()
            setPhase('input')
            setError(msg.message)
            break
        }
      }
      es.onerror = () => {
        es.close()
        setPhase('input')
        setError('Stream disconnected.')
      }
    } catch (err: unknown) {
      setPhase('input')
      if (err instanceof ClusterAuthError) setError(err.message)
      else if (err instanceof UnsupportedClusterVersionError) setError(err.message)
      else setError(err instanceof Error ? err.message : 'Failed to start')
    }
  }

  function stop() {
    esRef.current?.close()
    if (jobIdRef.current) api.inspect.cancelInspect(clusterId, jobIdRef.current).catch(() => {})
    setPhase('input')
    setStatusMsg('')
  }

  function reviewAndDelete(sourceFileId: string) {
    const alloc = result?.allocations.find(a => a.source_file_id === sourceFileId)
    const group = groups.find(g => g.source_file_id === sourceFileId)
    if (!alloc || alloc.deepest_index === null || !result) return
    navigate(`/cluster/${clusterId}/inspect/${sourceFileId}`, {
      state: {
        group,
        clusterName,
        recommendedTarget: {
          keep_days: alloc.keep_days,
          delete_before: alloc.delete_before,
          delete_count: alloc.delete_count,
          reclaim_bytes: alloc.reclaim_bytes,
        },
        // So cancelling or confirming the delete on that page returns here
        // to this same solved plan (updated with what got handled) instead
        // of a blank input screen.
        goalReturn: { groups, result, skipped, handledIds: Array.from(handledIds) },
      },
    })
  }

  return (
    <div className="fixed inset-0 flex items-center justify-center bg-black/40 p-4">
      <div className="w-full max-w-2xl rounded-lg border border-blackberry-700 bg-blackberry-900 p-6 shadow-xl">
        <div className="mb-4 flex items-center justify-between">
          <h3 className="text-base font-semibold text-lychee-100">Solve for a space goal</h3>
          <button onClick={onClose} className="text-sm text-lychee-400 hover:text-lychee-100">✕</button>
        </div>

        <p className="mb-2 text-xs text-lychee-500">
          Considering {groups.length} tree{groups.length !== 1 ? 's' : ''}. Trees that have never had Inspect
          run will be inspected automatically, one at a time, as part of solving.
        </p>

        {phase === 'input' && overlapping.length > 0 && (
          <p className="mb-2 text-xs text-kumquat-400">
            ⚠ {overlapping.length} of these overlap with another *selected* tree (nested paths, shown
            indented below) — that makes those scans slower and their reclaim estimates less precise.
            Remove one side of a conflict and this updates.
          </p>
        )}

        {phase === 'input' && (
          <div className="mb-4 max-h-64 overflow-y-auto rounded-md border border-blackberry-700 bg-blackberry-925 p-1">
            {treeRows.map(({ group, depth }) => (
              <div
                key={group.source_file_id}
                className={`flex items-center justify-between gap-2 rounded px-2 py-1 font-mono text-xs ${
                  conflictIds.has(group.source_file_id) ? 'text-kumquat-400' : 'text-lychee-300'
                }`}
                style={{ paddingLeft: `${8 + depth * 16}px` }}
              >
                <span
                  className="truncate"
                  title={conflictIds.has(group.source_file_id) ? 'Overlaps with another selected tree' : group.path}
                >
                  {conflictIds.has(group.source_file_id) && '⚠ '}{group.path}
                </span>
                <button
                  type="button"
                  onClick={() => onDeselect(group.source_file_id)}
                  className="flex-shrink-0 rounded px-2 py-0.5 text-lychee-500 hover:bg-blackberry-850 hover:underline"
                >
                  Remove
                </button>
              </div>
            ))}
          </div>
        )}

        {phase === 'input' && (
          <>
            <div className="mb-4 flex items-end gap-2">
              <div className="flex-1">
                <label className="mb-1 block text-xs font-medium text-lychee-400">Free up at least</label>
                <input
                  type="number"
                  min={0}
                  step="any"
                  value={amount}
                  onChange={e => setAmount(e.target.value)}
                  placeholder="e.g. 500"
                  className="w-full rounded-md border border-blackberry-700 bg-blackberry-800 px-3 py-1.5 text-sm text-lychee-300 focus:outline-none focus:ring-2 focus:ring-agave-500/30 focus:border-agave-500"
                  autoFocus
                />
              </div>
              <div className="flex gap-1 rounded-md border border-blackberry-700 bg-blackberry-800 p-1 text-xs">
                {(Object.keys(UNITS) as Unit[]).map(u => (
                  <button
                    key={u}
                    type="button"
                    onClick={() => setUnit(u)}
                    className={`rounded px-2 py-1 ${unit === u ? 'bg-agave-500 text-blackberry-950' : 'text-lychee-300 hover:bg-blackberry-850'}`}
                  >
                    {u}
                  </button>
                ))}
              </div>
            </div>
            {error && <p className="mb-4 text-sm text-pomegranate-400">{error}</p>}
            <div className="flex justify-end gap-2">
              <button onClick={onClose} className="rounded-md px-4 py-1.5 text-sm text-lychee-300 hover:bg-blackberry-850">
                Cancel
              </button>
              <button
                onClick={solve}
                className="rounded-md bg-agave-500 px-4 py-1.5 text-sm text-blackberry-950 hover:bg-agave-600"
              >
                Solve
              </button>
            </div>
          </>
        )}

        {phase === 'running' && (
          <>
            <p className="mb-2 text-sm text-lychee-300">{statusMsg}</p>
            {currentTree && (
              <div className="mb-4 rounded-md border border-blackberry-700 bg-blackberry-925 p-3 text-xs">
                <p className="font-mono text-lychee-300">{currentTree.path}</p>
                <p className="mt-1 text-lychee-500">Tree {currentTree.index + 1} of {currentTree.total}</p>
                {subProgress && (
                  <p className="mt-1 text-lychee-500">{subProgress.sized} of {subProgress.found} candidate files sized so far</p>
                )}
              </div>
            )}
            {skipped.length > 0 && (
              <p className="mb-4 text-xs text-kumquat-500">
                Skipped so far: {skipped.map(s => pathFor(s.source_file_id)).join(', ')}
              </p>
            )}
            <div className="flex justify-end">
              <button onClick={stop} className="rounded-md px-4 py-1.5 text-sm text-lychee-300 hover:bg-blackberry-850">
                Stop
              </button>
            </div>
          </>
        )}

        {phase === 'results' && result && (
          <>
            <div
              className={`mb-4 rounded-md border p-3 text-sm ${
                result.goal_met
                  ? 'border-kiwi-700 bg-kiwi-700/20 text-kiwi-400'
                  : 'border-kumquat-700 bg-kumquat-700/20 text-kumquat-400'
              }`}
            >
              {result.goal_met
                ? <>Goal met — up to <strong>{fmtBytes(result.total_freed_bytes)}</strong> reclaimable.</>
                : <>Fell short by <strong>{fmtBytes(result.shortfall)}</strong> — only <strong>{fmtBytes(result.total_freed_bytes)}</strong> reclaimable from the trees considered.</>}
            </div>

            {skipped.length > 0 && (
              <div className="mb-4 space-y-1">
                <p className="text-xs font-medium text-pomegranate-400">Not counted:</p>
                {skipped.map(s => (
                  <p key={s.source_file_id} className="text-xs text-pomegranate-400">
                    <span className="font-mono">{pathFor(s.source_file_id)}</span> — {s.reason}
                  </p>
                ))}
              </div>
            )}

            <div className="mb-4 max-h-80 overflow-y-auto overflow-x-auto rounded-lg border border-blackberry-700">
              <table className="w-full text-sm">
                <thead>
                  <tr className="border-b border-blackberry-700 bg-blackberry-800 text-left text-xs font-medium uppercase text-lychee-100">
                    <th className="px-3 py-2">Path</th>
                    <th className="px-3 py-2 text-right">Delete before</th>
                    <th className="px-3 py-2 text-right">Deletes</th>
                    <th className="px-3 py-2 text-right">Reclaims</th>
                    <th className="px-3 py-2 text-right">History given up</th>
                    <th className="px-3 py-2"></th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-blackberry-700">
                  {result.allocations
                    .filter(a => a.deepest_index !== null)
                    .sort((a, b) => b.reclaim_bytes - a.reclaim_bytes)
                    .map(a => {
                      const handled = handledIds.has(a.source_file_id)
                      return (
                        <tr key={a.source_file_id} className={handled ? 'text-lychee-600' : 'text-lychee-300'}>
                          <td className={`px-3 py-2 font-mono text-xs ${handled ? 'line-through' : ''}`}>{pathFor(a.source_file_id)}</td>
                          <td className="px-3 py-2 text-right font-mono text-xs">{a.delete_before}</td>
                          <td className="px-3 py-2 text-right">{a.delete_count}</td>
                          <td className="px-3 py-2 text-right font-medium text-kiwi-400">{fmtBytes(a.reclaim_bytes)}</td>
                          <td className="px-3 py-2 text-right">{a.days_sacrificed}d</td>
                          <td className="px-3 py-2 text-right">
                            {handled ? (
                              <span className="text-xs text-kiwi-400">✓ Deleted</span>
                            ) : (
                              <button
                                onClick={() => reviewAndDelete(a.source_file_id)}
                                className="rounded-md border border-blackberry-700 px-2 py-1 text-xs text-lychee-300 hover:bg-blackberry-850"
                              >
                                Review &amp; delete
                              </button>
                            )}
                          </td>
                        </tr>
                      )
                    })}
                  {result.allocations.every(a => a.deepest_index === null) && (
                    <tr>
                      <td colSpan={6} className="px-3 py-6 text-center text-xs text-lychee-500">
                        Nothing needed to be cut.
                      </td>
                    </tr>
                  )}
                </tbody>
              </table>
            </div>

            <div className="flex justify-end">
              <button onClick={onClose} className="rounded-md px-4 py-1.5 text-sm text-lychee-300 hover:bg-blackberry-850">
                Close
              </button>
            </div>
          </>
        )}
      </div>
    </div>
  )
}
