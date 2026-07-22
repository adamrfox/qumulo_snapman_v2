import { useEffect, useRef, useState } from 'react'
import { useLocation, useNavigate, useParams } from 'react-router-dom'
import { api, ClusterAuthError, UnsupportedClusterVersionError } from '../api'
import type { CurvePoint, GoalReturnState, LastRun, ReclaimRow, SnapshotGroup, SnapshotSizeRow } from '../types'
import { useAuth } from '../App'

function fmtBytes(n: number | null): string {
  if (n === null || n === 0) return '—'
  const units = ['B', 'KiB', 'MiB', 'GiB', 'TiB']
  let v = n
  let i = 0
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i++ }
  return `${v.toFixed(2)} ${units[i]}`
}

// fmtBytes collapses 0 to '—' (handy where 0 means "not measured"), but a
// genuinely computed zero -- e.g. a combined estimate that really is 0 bytes
// -- needs to read as an answer, not as a missing value.
function fmtBytesExact(n: number): string {
  return n === 0 ? '0 B' : fmtBytes(n)
}

function sizeStatusLabel(r: SnapshotSizeRow): { text: string; className: string; title?: string } {
  switch (r.status) {
    case 'computed':
      return r.exclusive_bytes === 0
        ? { text: '0 B', className: 'text-kiwi-400', title: 'Measured — nothing is uniquely tied to this snapshot; whatever it holds is still shared with a neighboring snapshot.' }
        : { text: fmtBytes(r.exclusive_bytes), className: 'text-kiwi-400' }
    case 'not_sizable':
      return { text: 'not sizable', className: 'text-lychee-500', title: 'No live-filesystem diff is available for the newest snapshot.' }
    case 'partial':
      return { text: 'partially sized', className: 'text-kumquat-500', title: 'A previous run started this one but was stopped before it finished. Size snapshots will resume it.' }
    case 'pending':
      return { text: 'stopped early', className: 'text-kumquat-500', title: 'The run was stopped before this snapshot finished.' }
    case 'timed_out':
      return { text: 'timed out', className: 'text-pomegranate-400', title: 'This snapshot exceeded the per-request timeout. Try again, or increase API_TIMEOUT if the cluster is consistently slow.' }
    case 'skipped_held':
      return { text: 'not deletable (held)', className: 'text-lychee-500', title: 'This snapshot or one of its neighbors is locked/replication-owned, so it can\'t be deleted (or the number wouldn\'t be actionable) — skipped by default. Check "Include locked/replication-held snapshots" to measure it anyway.' }
    default:
      return { text: 'not yet sized', className: 'text-lychee-500', title: 'Click Size snapshots to compute this.' }
  }
}

interface ActivePairItem {
  index: number
  total: number
  olderId: number
  olderName: string
  olderDate: string
  newerId: number
  newerName: string
  newerDate: string
  found: number
  sized: number
}

interface ActiveTripleItem {
  index: number
  total: number
  prevId: number
  prevName: string
  targetId: number
  targetName: string
  targetDate: string
  nextId: number
  nextName: string
  found: number
  sized: number
}

export default function InspectDetail() {
  const { clusterId, sourceFileId } = useParams<{ clusterId: string; sourceFileId: string }>()
  const location = useLocation()
  const navigate = useNavigate()
  const { user } = useAuth()

  const group: SnapshotGroup | undefined = location.state?.group
  const clusterName: string = location.state?.clusterName ?? ''
  const goalReturn: GoalReturnState | undefined = location.state?.goalReturn

  const [includeHeld, setIncludeHeld] = useState(false)

  const [rows, setRows] = useState<ReclaimRow[]>([])
  const [points, setPoints] = useState<CurvePoint[]>([])
  const [unmeasured, setUnmeasured] = useState(0)
  const [running, setRunning] = useState(false)
  const [activePairs, setActivePairs] = useState<Record<number, ActivePairItem>>({})
  const [completedPairs, setCompletedPairs] = useState(0)
  const [discoveredBytes, setDiscoveredBytes] = useState(0)
  const [statusMsg, setStatusMsg] = useState('')
  const [runSummary, setRunSummary] = useState<{ skipped: number; errored: number } | null>(null)
  const [deleteTarget, setDeleteTarget] = useState<ReclaimRow | null>(null)
  const [deleteConfirm, setDeleteConfirm] = useState('')
  const [deleteResult, setDeleteResult] = useState<{ deleted: number[]; errors: { id: number; error: string }[] } | null>(null)
  const [deleteTargetIds, setDeleteTargetIds] = useState<number[] | null>(null)
  const [showDeleteTargetList, setShowDeleteTargetList] = useState(false)
  const [loadingDeleteTargetList, setLoadingDeleteTargetList] = useState(false)

  // Arriving here from the goal solver's "Review & delete" pre-fills and
  // opens this exact confirmation modal instead of the results screen
  // inventing its own bulk-delete action -- every delete still needs its own
  // explicit confirmation, tree by tree, same as clicking a curve row here.
  useEffect(() => {
    const recommended: ReclaimRow | undefined = location.state?.recommendedTarget
    if (recommended) {
      setDeleteTarget(recommended)
      setDeleteConfirm('')
      setDeleteTargetIds(null)
      setShowDeleteTargetList(false)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const esRef = useRef<EventSource | null>(null)
  const jobIdRef = useRef<string | null>(null)
  const cancelRequestedRef = useRef(false)

  const [sizeRows, setSizeRows] = useState<SnapshotSizeRow[]>([])
  const [sizeLastRun, setSizeLastRun] = useState<LastRun | null>(null)
  const [sizeRunning, setSizeRunning] = useState(false)
  const [activeTriples, setActiveTriples] = useState<Record<number, ActiveTripleItem>>({})
  const [completedTriples, setCompletedTriples] = useState(0)
  const [sizeDiscoveredBytes, setSizeDiscoveredBytes] = useState(0)
  const [sizeStatusMsg, setSizeStatusMsg] = useState('')
  const [sizeRunSummary, setSizeRunSummary] = useState<{ skipped: number; errored: number } | null>(null)
  const [sizeDeleteTarget, setSizeDeleteTarget] = useState<SnapshotSizeRow | null>(null)
  const [sizeDeleteConfirm, setSizeDeleteConfirm] = useState('')
  const [sizeDeleteResult, setSizeDeleteResult] = useState<{ deleted: number[]; errors: { id: number; error: string }[] } | null>(null)
  const sizeEsRef = useRef<EventSource | null>(null)
  const sizeJobIdRef = useRef<string | null>(null)
  const sizeCancelRequestedRef = useRef(false)

  const [sizeFilterMode, setSizeFilterMode] = useState<'days' | 'date'>('days')
  const [sizeFilterDays, setSizeFilterDays] = useState('')
  const [sizeFilterDate, setSizeFilterDate] = useState('')

  const [selected, setSelected] = useState<Set<number>>(new Set())
  const [estimateRunning, setEstimateRunning] = useState(false)
  const [estimateStatusMsg, setEstimateStatusMsg] = useState('')
  const [estimateResult, setEstimateResult] = useState<{ totalBytes: number; complete: boolean; incompleteRuns: number } | null>(null)
  const [activeEstimatePairs, setActiveEstimatePairs] = useState<Record<string, {
    olderId: number; olderName: string; olderDate: string
    newerId: number; newerName: string; newerDate: string
  }>>({})
  const estimateEsRef = useRef<EventSource | null>(null)
  const [showDeleteSelectedModal, setShowDeleteSelectedModal] = useState(false)
  const [showSelectedList, setShowSelectedList] = useState(false)
  const [deleteSelectedConfirm, setDeleteSelectedConfirm] = useState('')
  const [deleteSelectedResult, setDeleteSelectedResult] = useState<{ deleted: number[]; errors: { id: number; error: string }[] } | null>(null)
  const [clusterAuthExpired, setClusterAuthExpired] = useState(false)
  const [unsupportedVersionMessage, setUnsupportedVersionMessage] = useState<string | null>(null)

  useEffect(() => {
    if (!clusterId || !sourceFileId) return
    api.inspect.curve(clusterId, sourceFileId)
      .then(r => { setRows(r.rows); setPoints(r.points); setUnmeasured(r.unmeasured_pairs) })
      .catch(e => {
        if (e instanceof ClusterAuthError) setClusterAuthExpired(true)
        if (e instanceof UnsupportedClusterVersionError) setUnsupportedVersionMessage(e.message)
      })
    api.inspect.snapshotSizes(clusterId, sourceFileId)
      .then(r => { setSizeRows(r.snapshots); setSizeLastRun(r.last_run) })
      .catch(e => {
        if (e instanceof ClusterAuthError) setClusterAuthExpired(true)
        if (e instanceof UnsupportedClusterVersionError) setUnsupportedVersionMessage(e.message)
      })
  }, [clusterId, sourceFileId])

  async function startInspect() {
    if (!clusterId || !sourceFileId || !group) return
    setRunning(true)
    setStatusMsg('Starting…')
    setRunSummary(null)
    setActivePairs({})
    setCompletedPairs(0)
    setDiscoveredBytes(0)
    esRef.current?.close()
    jobIdRef.current = null
    cancelRequestedRef.current = false

    try {
      const { job_id } = await api.inspect.startInspect(clusterId, sourceFileId, group.path, includeHeld)
      jobIdRef.current = job_id
      if (cancelRequestedRef.current) {
        api.inspect.cancelInspect(clusterId, job_id).catch(() => {})
      }
      const es = new EventSource(`/api/clusters/${clusterId}/jobs/${job_id}/stream`, { withCredentials: true })
      esRef.current = es

      let discovered = 0
      let completed = 0
      let skipped = 0
      let errored = 0

      es.onmessage = (evt) => {
        const msg = JSON.parse(evt.data)
        switch (msg.type) {
          case 'pair_start':
            setActivePairs(prev => ({
              ...prev,
              [msg.index]: {
                index: msg.index, total: msg.total,
                olderId: msg.older_id, olderName: msg.older_name, olderDate: msg.older_date,
                newerId: msg.newer_id, newerName: msg.newer_name, newerDate: msg.newer_date,
                found: 0, sized: 0,
              },
            }))
            setStatusMsg(`Measuring ${msg.total} pairs…`)
            break
          case 'progress':
            setActivePairs(prev => prev[msg.index] ? { ...prev, [msg.index]: { ...prev[msg.index], found: msg.found, sized: msg.sized } } : prev)
            break
          case 'discovered':
            discovered += msg.freed_bytes
            setDiscoveredBytes(discovered)
            break
          case 'pair_finished':
            completed++
            setCompletedPairs(completed)
            setActivePairs(prev => {
              const next = { ...prev }
              delete next[msg.index]
              return next
            })
            break
          case 'pair_result':
            if (msg.skipped_held) skipped++
            if (msg.error) errored++
            break
          case 'finish':
            es.close()
            setRunning(false)
            setStatusMsg('Done — reloading curve…')
            setRunSummary(skipped || errored ? { skipped, errored } : null)
            api.inspect.curve(clusterId!, sourceFileId!)
              .then(r => { setRows(r.rows); setPoints(r.points); setUnmeasured(r.unmeasured_pairs); setStatusMsg('') })
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
      if (err instanceof ClusterAuthError) setClusterAuthExpired(true)
      if (err instanceof UnsupportedClusterVersionError) setUnsupportedVersionMessage(err.message)
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
            setPoints(r.points)
            setUnmeasured(r.unmeasured_pairs)
            setStatusMsg('Stopped. Progress so far is saved — click Inspect again to resume.')
          })
          .catch(() => {})
      })
  }

  async function toggleDeleteTargetList() {
    if (showDeleteTargetList) {
      setShowDeleteTargetList(false)
      return
    }
    if (deleteTargetIds === null && clusterId && sourceFileId && deleteTarget) {
      setLoadingDeleteTargetList(true)
      try {
        const { snapshot_ids } = await api.inspect.olderThan(clusterId, sourceFileId, deleteTarget.delete_before)
        setDeleteTargetIds(snapshot_ids)
      } finally {
        setLoadingDeleteTargetList(false)
      }
    }
    setShowDeleteTargetList(true)
  }

  // Whether we got here via the goal solver's "Review & delete" -- if so,
  // cancelling or confirming this modal should return to that same solved
  // plan (updated with what just happened) instead of leaving the user
  // stranded on this tree's page with no way back but a fresh re-solve.
  function returnToPlan(handled: boolean) {
    if (!goalReturn || !sourceFileId) return
    const handledIds = new Set(goalReturn.handledIds)
    if (handled) handledIds.add(sourceFileId)
    navigate('/', {
      state: {
        selectedClusterId: clusterId,
        reopenGoal: { ...goalReturn, handledIds: Array.from(handledIds) },
      },
    })
  }

  function cancelDeleteTarget() {
    setDeleteTarget(null)
    setDeleteConfirm('')
    setDeleteTargetIds(null)
    setShowDeleteTargetList(false)
    returnToPlan(false)
  }

  async function doDelete() {
    if (!clusterId || !deleteTarget || !sourceFileId) return
    // Reuse the ids already fetched for the optional list, if the user looked at
    // it, so what gets deleted is exactly what they saw rather than a second,
    // possibly-slightly-different snapshot of "older than" at delete time.
    const ids = deleteTargetIds ?? (await api.inspect.olderThan(clusterId, sourceFileId, deleteTarget.delete_before)).snapshot_ids
    const result = await api.inspect.deleteSnapshots(clusterId, ids)
    setDeleteResult(result)
    setDeleteTarget(null)
    setDeleteTargetIds(null)
    setShowDeleteTargetList(false)
    returnToPlan(true)
  }

  async function startSizeSnapshots() {
    if (!clusterId || !sourceFileId || !group) return
    setSizeRunning(true)
    setSizeStatusMsg('Scanning…')
    setSizeRunSummary(null)
    setActiveTriples({})
    setCompletedTriples(0)
    setSizeDiscoveredBytes(0)
    sizeEsRef.current?.close()
    sizeJobIdRef.current = null
    sizeCancelRequestedRef.current = false

    try {
      const { job_id } = await api.inspect.startSizeSnapshots(clusterId, sourceFileId, group.path, includeHeld)
      sizeJobIdRef.current = job_id
      if (sizeCancelRequestedRef.current) {
        api.inspect.cancelInspect(clusterId, job_id).catch(() => {})
      }
      const es = new EventSource(`/api/clusters/${clusterId}/jobs/${job_id}/stream`, { withCredentials: true })
      sizeEsRef.current = es

      let discovered = 0
      let completed = 0
      let skipped = 0
      let errored = 0

      const reload = () => {
        api.inspect.snapshotSizes(clusterId!, sourceFileId!)
          .then(r => { setSizeRows(r.snapshots); setSizeLastRun(r.last_run); setSizeStatusMsg('') })
      }

      const applyRowResult = (
        id: number,
        status: SnapshotSizeRow['status'],
        exclusive_bytes: number | null,
        total_files: number | null,
      ) => {
        setSizeRows(prev => prev.map(r => r.id === id ? { ...r, status, exclusive_bytes, total_files } : r))
      }

      es.onmessage = (evt) => {
        const msg = JSON.parse(evt.data)
        switch (msg.type) {
          case 'boundary_start':
            setSizeStatusMsg(`Sizing oldest snapshot boundary — snap ${msg.older_id} (${msg.older_date}) → snap ${msg.newer_id} (${msg.newer_date})…`)
            break
          case 'boundary_result':
            if (msg.skipped_held) {
              skipped++
              applyRowResult(msg.older_id, 'skipped_held', null, null)
            } else if (msg.error) {
              errored++
            } else {
              applyRowResult(msg.older_id, 'computed', msg.freed_bytes, msg.total_files)
            }
            break
          case 'triple_start':
            setActiveTriples(prev => ({
              ...prev,
              [msg.index]: {
                index: msg.index, total: msg.total,
                prevId: msg.prev_id, prevName: msg.prev_name,
                targetId: msg.target_id, targetName: msg.target_name, targetDate: msg.target_date,
                nextId: msg.next_id, nextName: msg.next_name,
                found: 0, sized: 0,
              },
            }))
            setSizeStatusMsg(`Sizing ${msg.total} snapshots…`)
            break
          case 'progress':
            setActiveTriples(prev => prev[msg.index] ? { ...prev, [msg.index]: { ...prev[msg.index], found: msg.found, sized: msg.sized } } : prev)
            break
          case 'discovered':
            discovered += msg.exclusive_bytes
            setSizeDiscoveredBytes(discovered)
            break
          case 'triple_finished':
            completed++
            setCompletedTriples(completed)
            setActiveTriples(prev => {
              const next = { ...prev }
              delete next[msg.index]
              return next
            })
            break
          case 'triple_result': {
            if (msg.skipped_held) skipped++
            if (msg.error) errored++
            const status: SnapshotSizeRow['status'] = msg.skipped_held ? 'skipped_held'
              : msg.error ? 'unmeasured'
              : msg.timed_out ? 'timed_out'
              : msg.pending ? 'pending' : 'computed'
            applyRowResult(msg.target_id, status, msg.exclusive_bytes, msg.total_files)
            break
          }
          case 'no_middle_snapshots':
            break
          case 'finish':
            es.close()
            setSizeRunning(false)
            setSizeStatusMsg('')
            setSizeRunSummary(skipped || errored ? { skipped, errored } : null)
            reload()
            break
          case 'error':
            es.close()
            setSizeRunning(false)
            setSizeStatusMsg(`Error: ${msg.message}`)
            reload()
            break
        }
      }
      es.onerror = () => {
        es.close()
        setSizeRunning(false)
        setSizeStatusMsg('Stream disconnected.')
      }
    } catch (err: unknown) {
      setSizeRunning(false)
      if (err instanceof ClusterAuthError) setClusterAuthExpired(true)
      if (err instanceof UnsupportedClusterVersionError) setUnsupportedVersionMessage(err.message)
      setSizeStatusMsg(err instanceof Error ? err.message : 'Failed to start')
    }
  }

  function stopSizeSnapshots() {
    sizeEsRef.current?.close()
    sizeCancelRequestedRef.current = true
    setSizeRunning(false)
    setSizeStatusMsg('Stopping…')

    const jobId = sizeJobIdRef.current
    if (!clusterId || !sourceFileId || !jobId) return
    api.inspect.cancelInspect(clusterId, jobId)
      .catch(() => {})
      .finally(() => {
        api.inspect.snapshotSizes(clusterId, sourceFileId)
          .then(r => {
            setSizeRows(r.snapshots)
            setSizeStatusMsg('Stopped. Progress so far is saved — click Size snapshots again to resume.')
          })
          .catch(() => {})
      })
  }

  async function doSizeDelete() {
    if (!clusterId || !sizeDeleteTarget) return
    const result = await api.inspect.deleteSnapshots(clusterId, [sizeDeleteTarget.id])
    setSizeDeleteResult(result)
    setSizeDeleteTarget(null)
    setSizeRows(prev => prev.filter(r => r.id !== sizeDeleteTarget.id))
  }

  function toggleSelected(id: number) {
    setSelected(prev => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
    setEstimateResult(null)
  }

  function toggleSelectAll() {
    const selectableIds = filteredSizeRows.filter(r => r.status !== 'not_sizable' && !r.held).map(r => r.id)
    setSelected(prev => {
      const allSelected = selectableIds.length > 0 && selectableIds.every(id => prev.has(id))
      return allSelected ? new Set() : new Set(selectableIds)
    })
    setEstimateResult(null)
  }

  const cumulativeByOlderId = new Map(points.map(p => [p.older_id, p]))

  const sizeFilterDaysNum = sizeFilterDays.trim() === '' ? null : Number(sizeFilterDays)
  const filteredSizeRows = sizeRows.filter(r => {
    if (sizeFilterMode === 'days') {
      return sizeFilterDaysNum === null || Number.isNaN(sizeFilterDaysNum) || r.age_days > sizeFilterDaysNum
    }
    return sizeFilterDate === '' || r.date < sizeFilterDate
  })

  const selectableSizeRowIds = filteredSizeRows.filter(r => r.status !== 'not_sizable' && !r.held).map(r => r.id)
  const allSizeRowsSelected = selectableSizeRowIds.length > 0 && selectableSizeRowIds.every(id => selected.has(id))
  const someSizeRowsSelected = selectableSizeRowIds.some(id => selected.has(id))
  const selectAllRef = useRef<HTMLInputElement>(null)
  useEffect(() => {
    if (selectAllRef.current) {
      selectAllRef.current.indeterminate = someSizeRowsSelected && !allSizeRowsSelected
    }
  }, [someSizeRowsSelected, allSizeRowsSelected])

  async function startEstimate() {
    if (!clusterId || !sourceFileId || selected.size === 0) return
    setEstimateRunning(true)
    setEstimateStatusMsg('Identifying what needs measuring…')
    setEstimateResult(null)
    setActiveEstimatePairs({})
    estimateEsRef.current?.close()

    let runsSeen = 0
    let incompleteRuns = 0

    try {
      const { job_id } = await api.inspect.estimateDeletion(clusterId, sourceFileId, Array.from(selected))
      const es = new EventSource(`/api/clusters/${clusterId}/jobs/${job_id}/stream`, { withCredentials: true })
      estimateEsRef.current = es

      es.onmessage = (evt) => {
        const msg = JSON.parse(evt.data)
        switch (msg.type) {
          case 'run_start':
            runsSeen++
            setEstimateStatusMsg(`Measuring ${runsSeen === 1 ? '1 group' : `${runsSeen} groups`} of selected snapshots…`)
            break
          case 'pair_start':
            setActiveEstimatePairs(prev => ({
              ...prev,
              [`${msg.older_id}-${msg.newer_id}`]: {
                olderId: msg.older_id, olderName: msg.older_name, olderDate: msg.older_date,
                newerId: msg.newer_id, newerName: msg.newer_name, newerDate: msg.newer_date,
              },
            }))
            break
          case 'pair_done':
            setActiveEstimatePairs(prev => {
              const next = { ...prev }
              delete next[`${msg.older_id}-${msg.newer_id}`]
              return next
            })
            break
          case 'run_result':
            if (msg.error) incompleteRuns++
            break
          case 'estimate_result':
            setEstimateResult({ totalBytes: msg.total_bytes, complete: msg.complete, incompleteRuns })
            break
          case 'finish':
            es.close()
            setEstimateRunning(false)
            setEstimateStatusMsg('')
            break
          case 'error':
            es.close()
            setEstimateRunning(false)
            setEstimateStatusMsg(`Error: ${msg.message}`)
            break
        }
      }
      es.onerror = () => {
        es.close()
        setEstimateRunning(false)
        setEstimateStatusMsg('Stream disconnected.')
      }
    } catch (err: unknown) {
      setEstimateRunning(false)
      if (err instanceof ClusterAuthError) setClusterAuthExpired(true)
      if (err instanceof UnsupportedClusterVersionError) setUnsupportedVersionMessage(err.message)
      setEstimateStatusMsg(err instanceof Error ? err.message : 'Failed to start')
    }
  }

  async function doDeleteSelected() {
    if (!clusterId || selected.size === 0) return
    const result = await api.inspect.deleteSnapshots(clusterId, Array.from(selected))
    setDeleteSelectedResult(result)
    setShowDeleteSelectedModal(false)
    setSizeRows(prev => prev.filter(r => !selected.has(r.id)))
    setSelected(new Set())
    setEstimateResult(null)
  }

  const canDelete = user?.role === 'admin' || user?.role === 'operator'

  // The curve only shows a row where it can name an unambiguous "delete before
  // this date" cutoff -- i.e. somewhere an older/newer pair fall on different
  // calendar days. A burst of same-day snapshots can be fully measured (every
  // pair cached, unmeasured === 0) and still produce zero rows, because there's
  // no date boundary to express. Without this check that reads identically to
  // "Inspect hasn't been run yet."
  const curveFullyMeasured = points.length > 0 && unmeasured === 0
  const curveHasNoExpressibleRow = curveFullyMeasured && rows.length === 0
  const curveTotalBytes = points.length > 0 ? points[points.length - 1].cumulative_bytes : null

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
        <div className="flex items-center gap-4">
          <label className="flex items-center gap-2 text-xs text-lychee-400">
            <input
              type="checkbox"
              checked={includeHeld}
              onChange={e => setIncludeHeld(e.target.checked)}
            />
            Include locked/replication-held snapshots
          </label>
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
          {!sizeRunning ? (
            <button
              onClick={startSizeSnapshots}
              className="rounded-md border border-agave-500 px-4 py-1.5 text-sm text-agave-400 hover:bg-blackberry-850"
            >
              {sizeRows.some(r => r.status === 'computed') ? 'Re-size snapshots' : 'Size snapshots'}
            </button>
          ) : (
            <button
              onClick={stopSizeSnapshots}
              className="rounded-md border border-kumquat-500 px-4 py-1.5 text-sm text-kumquat-500 hover:bg-blackberry-850"
            >
              Stop
            </button>
          )}
          </div>
        </div>
      </div>

      {clusterAuthExpired && (
        <div className="mb-4 flex items-center justify-between rounded-md border border-pomegranate-700 bg-pomegranate-700/20 px-4 py-3 text-sm text-pomegranate-400">
          <span>This cluster's stored credentials have expired or are no longer valid.</span>
          <button
            onClick={() => navigate('/', { state: { selectedClusterId: clusterId } })}
            className="flex-shrink-0 rounded-md bg-agave-500 px-3 py-1 text-xs text-blackberry-950 hover:bg-agave-600"
          >
            Update credentials
          </button>
        </div>
      )}

      {unsupportedVersionMessage && (
        <div className="mb-4 flex items-center justify-between rounded-md border border-kumquat-700 bg-kumquat-700/20 px-4 py-3 text-sm text-kumquat-400">
          <span>{unsupportedVersionMessage}</span>
          <button
            onClick={() => setUnsupportedVersionMessage(null)}
            className="flex-shrink-0 rounded-md px-3 py-1 text-xs text-lychee-300 hover:bg-blackberry-850"
          >
            Dismiss
          </button>
        </div>
      )}

      {/* Progress */}
      {running && (
        <div className="mb-4 rounded-lg border border-blackberry-700 bg-blackberry-900 p-4 text-sm">
          <p className="mb-2 font-medium text-lychee-100">{statusMsg}</p>
          {Object.values(activePairs).length > 0 && (
            <>
              <div className="mb-1 flex justify-between text-xs text-lychee-400">
                <span>{completedPairs}/{Object.values(activePairs)[0]?.total ?? 0} pairs done</span>
                <span>{Object.values(activePairs).length} in progress</span>
              </div>
              <div className="mb-3 h-2 rounded-full bg-blackberry-800">
                <div
                  className="h-2 rounded-full bg-agave-500 transition-all"
                  style={{ width: `${Math.round((completedPairs / Math.max(Object.values(activePairs)[0]?.total ?? 1, 1)) * 100)}%` }}
                />
              </div>
              <ul className="space-y-1.5">
                {Object.values(activePairs).sort((a, b) => a.index - b.index).map(p => (
                  <li key={p.index} className="flex justify-between font-mono text-xs text-lychee-400">
                    <span>
                      #{p.index} snap {p.olderId} "{p.olderName}" ({p.olderDate}) → snap {p.newerId} "{p.newerName}" ({p.newerDate})
                    </span>
                    <span className="whitespace-nowrap pl-2">{p.found} found · {p.sized} sized</span>
                  </li>
                ))}
              </ul>
            </>
          )}
          {discoveredBytes > 0 && (
            <p className="mt-2 text-xs text-lychee-400">Discovered so far: {fmtBytes(discoveredBytes)}</p>
          )}
        </div>
      )}
      {statusMsg && !running && (
        <p className="mb-4 text-sm text-lychee-400">{statusMsg}</p>
      )}
      {runSummary && !running && (
        <p className="mb-4 text-sm text-kumquat-500">
          {runSummary.skipped > 0 && `${runSummary.skipped} pair${runSummary.skipped > 1 ? 's' : ''} skipped (locked/replication-held)`}
          {runSummary.skipped > 0 && runSummary.errored > 0 && ' · '}
          {runSummary.errored > 0 && `${runSummary.errored} pair${runSummary.errored > 1 ? 's' : ''} failed (see backend log)`}
        </p>
      )}

      {/* Sizing progress */}
      {sizeRunning && (
        <div className="mb-4 rounded-lg border border-blackberry-700 bg-blackberry-900 p-4 text-sm">
          <p className="mb-2 font-medium text-lychee-100">{sizeStatusMsg}</p>
          {Object.values(activeTriples).length > 0 && (
            <>
              <div className="mb-1 flex justify-between text-xs text-lychee-400">
                <span>{completedTriples}/{Object.values(activeTriples)[0]?.total ?? 0} snapshots done</span>
                <span>{Object.values(activeTriples).length} in progress</span>
              </div>
              <div className="mb-3 h-2 rounded-full bg-blackberry-800">
                <div
                  className="h-2 rounded-full bg-agave-500 transition-all"
                  style={{ width: `${Math.round((completedTriples / Math.max(Object.values(activeTriples)[0]?.total ?? 1, 1)) * 100)}%` }}
                />
              </div>
              <ul className="space-y-1.5">
                {Object.values(activeTriples).sort((a, b) => a.index - b.index).map(t => (
                  <li key={t.index} className="flex justify-between font-mono text-xs text-lychee-400">
                    <span>
                      #{t.index} snap {t.targetId} "{t.targetName}" ({t.targetDate}) — neighbors {t.prevId} "{t.prevName}" / {t.nextId} "{t.nextName}"
                    </span>
                    <span className="whitespace-nowrap pl-2">{t.found} found · {t.sized} sized</span>
                  </li>
                ))}
              </ul>
            </>
          )}
          {sizeDiscoveredBytes > 0 && (
            <p className="mt-2 text-xs text-lychee-400">Discovered so far: {fmtBytes(sizeDiscoveredBytes)}</p>
          )}
        </div>
      )}
      {sizeStatusMsg && !sizeRunning && (
        <p className="mb-4 text-sm text-lychee-400">{sizeStatusMsg}</p>
      )}
      {sizeRunSummary && !sizeRunning && (
        <p className="mb-4 text-sm text-kumquat-500">
          {sizeRunSummary.skipped > 0 && `${sizeRunSummary.skipped} snapshot${sizeRunSummary.skipped > 1 ? 's' : ''} skipped (locked/replication-held)`}
          {sizeRunSummary.skipped > 0 && sizeRunSummary.errored > 0 && ' · '}
          {sizeRunSummary.errored > 0 && `${sizeRunSummary.errored} snapshot${sizeRunSummary.errored > 1 ? 's' : ''} failed (see backend log)`}
        </p>
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
                        onClick={() => { setDeleteTarget(row); setDeleteConfirm(''); setDeleteTargetIds(null); setShowDeleteTargetList(false) }}
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

      {rows.length === 0 && !running && curveHasNoExpressibleRow && (
        <div className="rounded-lg border border-blackberry-700 bg-blackberry-900 p-6 text-sm text-lychee-400">
          <p className="mb-2">
            All {points.length + 1} snapshots in this tree were measured — every consecutive pair
            was taken on the same calendar day as its neighbor, so there's no unambiguous
            "delete everything before this date" cutoff to show as a row here.
          </p>
          <p>
            Deleting every snapshot shown below except the newest would free approximately{' '}
            <strong className="text-kiwi-400">{fmtBytesExact(curveTotalBytes ?? 0)}</strong>. To
            verify that number or act on it, check all of them except the newest in the Snapshot
            sizes table below and click "Estimate combined savings" (or skip straight to "Delete
            selected") — checking just one at a time there will usually show much less, since most
            of what a same-day burst of snapshots holds is still shared with its neighbors.
          </p>
        </div>
      )}

      {rows.length === 0 && !running && !curveHasNoExpressibleRow && (
        <div className="rounded-lg border border-blackberry-700 bg-blackberry-900 p-8 text-center text-sm text-lychee-500">
          {points.length === 0
            ? 'No measurements yet — click Inspect to start'
            : `Still measuring — ${points.length - unmeasured} of ${points.length} pairs done so far. Click Re-inspect to continue.`}
        </div>
      )}

      {/* Snapshot sizes table */}
      {sizeRows.length > 0 && (
        <div className="mb-6 overflow-x-auto rounded-lg border border-blackberry-700 bg-blackberry-900 shadow-md">
          <div className="border-b border-blackberry-700 px-4 py-3">
            <h3 className="text-sm font-medium text-lychee-100">Snapshot sizes</h3>
            <p className="text-xs text-lychee-500">How much deleting just that one snapshot alone would free. Check multiple to see what deleting them together would free.</p>
            <div className="mt-3 flex flex-wrap items-center gap-3">
              <div className="flex gap-1 rounded-md border border-blackberry-700 bg-blackberry-800 p-1 text-xs">
                <button
                  type="button"
                  onClick={() => setSizeFilterMode('days')}
                  className={`rounded px-2 py-1 ${sizeFilterMode === 'days' ? 'bg-agave-500 text-blackberry-950' : 'text-lychee-300 hover:bg-blackberry-850'}`}
                >
                  Older than (days)
                </button>
                <button
                  type="button"
                  onClick={() => setSizeFilterMode('date')}
                  className={`rounded px-2 py-1 ${sizeFilterMode === 'date' ? 'bg-agave-500 text-blackberry-950' : 'text-lychee-300 hover:bg-blackberry-850'}`}
                >
                  Before date
                </button>
              </div>
              {sizeFilterMode === 'days' ? (
                <input
                  type="number"
                  min={0}
                  value={sizeFilterDays}
                  onChange={e => setSizeFilterDays(e.target.value)}
                  placeholder="e.g. 30"
                  className="w-24 rounded-md border border-blackberry-700 bg-blackberry-800 px-2 py-1 text-xs text-lychee-300 focus:outline-none focus:ring-2 focus:ring-agave-500/30 focus:border-agave-500"
                />
              ) : (
                <input
                  type="date"
                  value={sizeFilterDate}
                  onChange={e => setSizeFilterDate(e.target.value)}
                  className="rounded-md border border-blackberry-700 bg-blackberry-800 px-2 py-1 text-xs text-lychee-300 focus:outline-none focus:ring-2 focus:ring-agave-500/30 focus:border-agave-500"
                />
              )}
              {((sizeFilterMode === 'days' && sizeFilterDays.trim() !== '') || (sizeFilterMode === 'date' && sizeFilterDate !== '')) && (
                <button
                  type="button"
                  onClick={() => { setSizeFilterDays(''); setSizeFilterDate('') }}
                  className="text-xs text-agave-400 hover:underline"
                >
                  Clear filter
                </button>
              )}
              {filteredSizeRows.length !== sizeRows.length && (
                <span className="text-xs text-lychee-500">
                  Showing {filteredSizeRows.length} of {sizeRows.length} snapshots
                </span>
              )}
            </div>
          </div>
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-blackberry-700 bg-blackberry-800 text-left text-xs font-medium uppercase text-lychee-100">
                {canDelete && (
                  <th className="px-4 py-3">
                    <input
                      ref={selectAllRef}
                      type="checkbox"
                      checked={allSizeRowsSelected}
                      disabled={selectableSizeRowIds.length === 0}
                      onChange={toggleSelectAll}
                      title="Select all"
                    />
                  </th>
                )}
                <th className="px-4 py-3">Snapshot</th>
                <th className="px-4 py-3 text-right">Age</th>
                <th className="px-4 py-3 text-right">Individual size</th>
                <th
                  className="px-4 py-3 text-right"
                  title="Space freed if you deleted this snapshot and every older one, keeping this snapshot's next-newer neighbor onward"
                >
                  Cumulative reclaim
                </th>
                {canDelete && <th className="px-4 py-3"></th>}
              </tr>
            </thead>
            <tbody className="divide-y divide-blackberry-700">
              {filteredSizeRows.map(r => {
                const unselectable = r.status === 'not_sizable' || r.held
                return (
                  <tr key={r.id} className="text-lychee-300 hover:bg-blackberry-850">
                    {canDelete && (
                      <td className="px-4 py-3">
                        <input
                          type="checkbox"
                          checked={selected.has(r.id)}
                          disabled={unselectable}
                          onChange={() => toggleSelected(r.id)}
                          title={unselectable ? (r.held ? (r.held_reason ?? 'held') : 'The newest snapshot has no later snapshot to compare against') : undefined}
                        />
                      </td>
                    )}
                    <td className="px-4 py-3">
                      <div className="font-mono text-xs">{r.date}</div>
                      <div className="text-xs text-lychee-500">
                        {r.name}
                        {r.held && <span className="ml-1 text-kumquat-500">· {r.held_reason}</span>}
                      </div>
                    </td>
                    <td className="px-4 py-3 text-right">{r.age_days}d</td>
                    <td className="px-4 py-3 text-right font-medium">
                      <span className={sizeStatusLabel(r).className} title={sizeStatusLabel(r).title}>
                        {sizeStatusLabel(r).text}
                      </span>
                    </td>
                    <td className="px-4 py-3 text-right font-medium text-kiwi-400">
                      {(() => {
                        const point = cumulativeByOlderId.get(r.id)
                        if (!point) {
                          return <span className="text-lychee-500" title="No later snapshot to compare against">—</span>
                        }
                        if (point.cumulative_bytes === null) {
                          return <span className="text-lychee-500" title="Not yet measured — run Inspect to compute the reclaim curve">—</span>
                        }
                        return fmtBytesExact(point.cumulative_bytes)
                      })()}
                    </td>
                    {canDelete && (
                      <td className="px-4 py-3 text-right">
                        <button
                          onClick={() => { setSizeDeleteTarget(r); setSizeDeleteConfirm('') }}
                          disabled={r.held}
                          title={r.held ? (r.held_reason ?? 'held') : undefined}
                          className="rounded-md bg-pomegranate-600 px-3 py-1 text-xs text-lychee-50 hover:bg-pomegranate-700 disabled:opacity-40"
                        >
                          Delete
                        </button>
                      </td>
                    )}
                  </tr>
                )
              })}
              {filteredSizeRows.length === 0 && (
                <tr>
                  <td colSpan={canDelete ? 6 : 4} className="px-4 py-6 text-center text-xs text-lychee-500">
                    No snapshots match this filter.
                  </td>
                </tr>
              )}
            </tbody>
          </table>
          {sizeLastRun?.status === 'error' && (
            <p className="px-4 py-2 text-xs text-pomegranate-400">
              Last Size snapshots run failed{sizeLastRun.finished_at ? ` (${new Date(sizeLastRun.finished_at).toLocaleString()})` : ''}: {sizeLastRun.error_message}
            </p>
          )}

          {/* Combined selection summary */}
          {canDelete && selected.size > 0 && (
            <div className="border-t border-blackberry-700 bg-blackberry-850 px-4 py-3">
              <div className="flex items-center justify-between">
                <p className="text-sm text-lychee-100">{selected.size} snapshot{selected.size > 1 ? 's' : ''} selected</p>
                <div className="flex gap-2">
                  <button
                    onClick={startEstimate}
                    disabled={estimateRunning}
                    className="rounded-md border border-agave-500 px-3 py-1.5 text-xs text-agave-400 hover:bg-blackberry-800 disabled:opacity-40"
                  >
                    {estimateRunning ? 'Estimating…' : 'Estimate combined savings'}
                  </button>
                  <button
                    onClick={() => { setShowDeleteSelectedModal(true); setDeleteSelectedConfirm(''); setShowSelectedList(false) }}
                    className="rounded-md bg-pomegranate-600 px-3 py-1.5 text-xs text-lychee-50 hover:bg-pomegranate-700"
                  >
                    Delete selected
                  </button>
                </div>
              </div>

              {estimateRunning && (
                <div className="mt-2 text-xs text-lychee-400">
                  <p>{estimateStatusMsg}</p>
                  {Object.values(activeEstimatePairs).length > 0 && (
                    <ul className="mt-1 space-y-1">
                      {Object.values(activeEstimatePairs).map(p => (
                        <li key={`${p.olderId}-${p.newerId}`} className="font-mono">
                          measuring snap {p.olderId} "{p.olderName}" ({p.olderDate}) → snap {p.newerId} "{p.newerName}" ({p.newerDate})
                        </li>
                      ))}
                    </ul>
                  )}
                </div>
              )}
              {!estimateRunning && estimateStatusMsg && (
                <p className="mt-2 text-xs text-pomegranate-400">{estimateStatusMsg}</p>
              )}
              {!estimateRunning && estimateResult && (
                estimateResult.complete ? (
                  <p className="mt-2 text-sm text-kiwi-400">
                    Deleting these {selected.size} snapshots together would free approximately{' '}
                    <strong>{fmtBytesExact(estimateResult.totalBytes)}</strong>.
                  </p>
                ) : (
                  <p className="mt-2 text-sm text-kumquat-500">
                    Estimate incomplete — {estimateResult.incompleteRuns} group{estimateResult.incompleteRuns > 1 ? 's' : ''} of the selection
                    couldn't be measured (see backend log). Partial total so far: {fmtBytesExact(estimateResult.totalBytes)}.
                  </p>
                )
              )}
            </div>
          )}
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
            <div className="mb-4">
              <button
                type="button"
                onClick={toggleDeleteTargetList}
                className="text-xs text-agave-400 hover:underline"
              >
                {showDeleteTargetList ? 'Hide' : 'Show'} the {deleteTarget.delete_count} snapshots
              </button>
              {showDeleteTargetList && (
                <div className="mt-2 max-h-48 overflow-y-auto rounded-md border border-blackberry-700 bg-blackberry-950 p-2 font-mono text-xs text-lychee-400">
                  {loadingDeleteTargetList
                    ? 'Loading…'
                    : (deleteTargetIds ?? []).map(id => {
                        const row = sizeRows.find(r => r.id === id)
                        return <div key={id}>{row ? `${row.date}  ${row.name}` : `#${id}`}</div>
                      })}
                </div>
              )}
            </div>
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
                onClick={cancelDeleteTarget}
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

      {/* Per-snapshot delete result */}
      {sizeDeleteResult && (
        <div className="mb-4 rounded-lg border border-blackberry-700 bg-blackberry-900 p-4 text-sm">
          <p className="font-medium text-lychee-100">
            Deleted {sizeDeleteResult.deleted.length} snapshot{sizeDeleteResult.deleted.length !== 1 ? 's' : ''}
            {sizeDeleteResult.errors.length > 0 && ` · ${sizeDeleteResult.errors.length} error(s)`}
          </p>
          {sizeDeleteResult.errors.map((e, i) => (
            <p key={i} className="mt-1 text-xs text-pomegranate-400">Snapshot {e.id}: {e.error}</p>
          ))}
          <button onClick={() => setSizeDeleteResult(null)} className="mt-2 text-xs text-agave-400 hover:underline">
            Dismiss
          </button>
        </div>
      )}

      {/* Per-snapshot delete confirmation modal */}
      {sizeDeleteTarget && (
        <div className="fixed inset-0 flex items-center justify-center bg-black/40 p-4">
          <div className="w-full max-w-md rounded-lg border border-blackberry-700 bg-blackberry-900 p-6 shadow-xl">
            <h3 className="mb-2 text-base font-semibold text-pomegranate-400">Confirm deletion</h3>
            <p className="mb-4 text-sm text-lychee-300">
              This will delete the snapshot <strong className="text-lychee-100">{sizeDeleteTarget.name}</strong>{' '}
              ({sizeDeleteTarget.date}), freeing approximately{' '}
              <strong className="text-lychee-100">{sizeDeleteTarget.exclusive_bytes === 0 ? '0 B' : fmtBytes(sizeDeleteTarget.exclusive_bytes)}</strong>.
              This cannot be undone.
            </p>
            <p className="mb-2 text-sm text-lychee-300">
              Type <code className="rounded bg-blackberry-800 px-1">delete snapshot</code> to confirm:
            </p>
            <input
              value={sizeDeleteConfirm}
              onChange={e => setSizeDeleteConfirm(e.target.value)}
              className="mb-4 w-full rounded-md border border-blackberry-700 bg-blackberry-800 px-3 py-2 font-mono text-sm text-lychee-300 focus:outline-none focus:ring-2 focus:ring-agave-500/30 focus:border-agave-500"
              autoFocus
            />
            <div className="flex justify-end gap-2">
              <button
                onClick={() => setSizeDeleteTarget(null)}
                className="rounded-md px-4 py-1.5 text-sm text-lychee-300 hover:bg-blackberry-850"
              >
                Cancel
              </button>
              <button
                disabled={sizeDeleteConfirm !== 'delete snapshot'}
                onClick={doSizeDelete}
                className="rounded-md bg-pomegranate-600 px-4 py-1.5 text-sm text-lychee-50 hover:bg-pomegranate-700 disabled:opacity-40"
              >
                Delete
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Multi-select delete result */}
      {deleteSelectedResult && (
        <div className="mb-4 rounded-lg border border-blackberry-700 bg-blackberry-900 p-4 text-sm">
          <p className="font-medium text-lychee-100">
            Deleted {deleteSelectedResult.deleted.length} snapshot{deleteSelectedResult.deleted.length !== 1 ? 's' : ''}
            {deleteSelectedResult.errors.length > 0 && ` · ${deleteSelectedResult.errors.length} error(s)`}
          </p>
          {deleteSelectedResult.errors.map((e, i) => (
            <p key={i} className="mt-1 text-xs text-pomegranate-400">Snapshot {e.id}: {e.error}</p>
          ))}
          <button onClick={() => setDeleteSelectedResult(null)} className="mt-2 text-xs text-agave-400 hover:underline">
            Dismiss
          </button>
        </div>
      )}

      {/* Multi-select delete confirmation modal */}
      {showDeleteSelectedModal && (
        <div className="fixed inset-0 flex items-center justify-center bg-black/40 p-4">
          <div className="w-full max-w-md rounded-lg border border-blackberry-700 bg-blackberry-900 p-6 shadow-xl">
            <h3 className="mb-2 text-base font-semibold text-pomegranate-400">Confirm deletion</h3>
            <p className="mb-4 text-sm text-lychee-300">
              This will delete <strong className="text-lychee-100">{selected.size} snapshots</strong>
              {estimateResult?.complete && (
                <> together, freeing approximately <strong className="text-lychee-100">{fmtBytesExact(estimateResult.totalBytes)}</strong></>
              )}
              . This cannot be undone.
              {!estimateResult && (
                <span className="mt-2 block text-xs text-kumquat-500">You haven't run "Estimate combined savings" for this selection yet.</span>
              )}
            </p>
            <div className="mb-4">
              <button
                type="button"
                onClick={() => setShowSelectedList(v => !v)}
                className="text-xs text-agave-400 hover:underline"
              >
                {showSelectedList ? 'Hide' : 'Show'} the {selected.size} snapshots
              </button>
              {showSelectedList && (
                <div className="mt-2 max-h-48 overflow-y-auto rounded-md border border-blackberry-700 bg-blackberry-950 p-2 font-mono text-xs text-lychee-400">
                  {Array.from(selected).map(id => {
                    const row = sizeRows.find(r => r.id === id)
                    return <div key={id}>{row ? `${row.date}  ${row.name}` : `#${id}`}</div>
                  })}
                </div>
              )}
            </div>
            <p className="mb-2 text-sm text-lychee-300">
              Type <code className="rounded bg-blackberry-800 px-1">delete {selected.size} snapshots</code> to confirm:
            </p>
            <input
              value={deleteSelectedConfirm}
              onChange={e => setDeleteSelectedConfirm(e.target.value)}
              className="mb-4 w-full rounded-md border border-blackberry-700 bg-blackberry-800 px-3 py-2 font-mono text-sm text-lychee-300 focus:outline-none focus:ring-2 focus:ring-agave-500/30 focus:border-agave-500"
              autoFocus
            />
            <div className="flex justify-end gap-2">
              <button
                onClick={() => setShowDeleteSelectedModal(false)}
                className="rounded-md px-4 py-1.5 text-sm text-lychee-300 hover:bg-blackberry-850"
              >
                Cancel
              </button>
              <button
                disabled={deleteSelectedConfirm !== `delete ${selected.size} snapshots`}
                onClick={doDeleteSelected}
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
