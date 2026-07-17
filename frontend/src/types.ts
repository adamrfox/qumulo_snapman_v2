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

export interface User {
  id: string
  username: string
  role: Role
  is_active: boolean
  created_at: string
}
