export type Role = 'admin' | 'operator' | 'viewer'

export interface AuthUser {
  id: string
  username: string
  role: Role
}

export interface Cluster {
  id: string
  display_name: string
  host: string
  port: number
  insecure: boolean
  created_at: string
  owner_id: string
  owner_username?: string
}

export interface SnapshotGroup {
  source_file_id: string
  path: string
  count: number
  max_age_days: number
  min_age_days: number
  prunable: number
  measured_pairs: number
  total_pairs: number
  reclaim_bytes: number
  is_upper_bound: boolean
  held_reason: string | null
}

export interface ReclaimRow {
  keep_days: number
  delete_before: string
  delete_before_id: number
  delete_count: number
  reclaim_bytes: number
}

export interface CurvePoint {
  older_id: number
  older_date: string
  older_name: string
  newer_id: number
  newer_date: string
  freed_bytes: number | null
  cumulative_bytes: number | null
  total_files: number | null
  status: 'computed' | 'cached' | 'pending' | 'timed_out'
}

export interface SnapshotSizeRow {
  id: number
  name: string
  date: string
  age_days: number
  exclusive_bytes: number | null
  total_files: number | null
  status: 'not_sizable' | 'computed' | 'unmeasured' | 'partial' | 'pending' | 'timed_out' | 'skipped_held'
  held: boolean
  held_reason: string | null
}

export interface LastRun {
  status: string
  error_message: string | null
  finished_at: string | null
}

export interface TreeAllocation {
  source_file_id: string
  deepest_index: number | null
  delete_snapshot_ids: number[]
  keep_days: number | null
  delete_before: string | null
  delete_before_id: number | null
  delete_count: number
  reclaim_bytes: number
  days_sacrificed: number
}

export interface GoalResult {
  goal_met: boolean
  target_bytes: number
  total_freed_bytes: number
  shortfall: number
  allocations: TreeAllocation[]
}

export interface GoalSkippedTree {
  source_file_id: string
  reason: string
}

// Carried through router state from the goal solver's results screen to a
// tree's detail page and back, so cancelling or confirming a "Review &
// delete" round trip returns to the same solved plan (updated with which
// trees have been handled) instead of a blank input screen.
export interface GoalReturnState {
  groups: SnapshotGroup[]
  result: GoalResult
  skipped: GoalSkippedTree[]
  handledIds: string[]
  excludedIds: string[]
}

export interface User {
  id: string
  username: string
  role: Role
  is_active: boolean
  created_at: string
}
