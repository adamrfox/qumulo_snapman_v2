// Thrown when a cluster's *stored Qumulo token* has expired or been revoked (backend
// status 424) — distinct from the app's own session-expiry 401, which the global
// handler below turns into a full logout. This one should prompt the user to update
// that cluster's credentials instead.
export class ClusterAuthError extends Error {}

// Thrown when the cluster itself runs a Qumulo Core release older than this tool
// supports (backend status 426 — "Upgrade Required"). Unlike ClusterAuthError,
// re-entering credentials can't fix this, so it should read as a plain notice,
// not an actionable prompt.
export class UnsupportedClusterVersionError extends Error {}

async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  const res = await fetch(path, {
    method,
    headers: body !== undefined ? { 'Content-Type': 'application/json' } : {},
    body: body !== undefined ? JSON.stringify(body) : undefined,
    credentials: 'include',
  })
  if (!res.ok) {
    if (res.status === 401 && path !== '/api/auth/login' && path !== '/api/auth/me') {
      window.dispatchEvent(new CustomEvent('snapman:unauthorized'))
      throw new Error('Your snapman session has expired. Please sign in again.')
    }
    let detail = `HTTP ${res.status}`
    try {
      const data = await res.json()
      detail = data.detail ?? detail
    } catch {}
    if (res.status === 424) {
      throw new ClusterAuthError(detail)
    }
    if (res.status === 426) {
      throw new UnsupportedClusterVersionError(detail)
    }
    throw new Error(detail)
  }
  if (res.status === 204) return undefined as T
  return res.json()
}

export const api = {
  auth: {
    login: (username: string, password: string) =>
      request<{ username: string; role: string; id: string }>('POST', '/api/auth/login', { username, password }),
    logout: () => request<void>('POST', '/api/auth/logout'),
    me: () => request<{ id: string; username: string; role: string }>('GET', '/api/auth/me'),
  },
  users: {
    list: () => request<import('./types').User[]>('GET', '/api/users/'),
    create: (username: string, password: string, role: string) =>
      request('POST', '/api/users/', { username, password, role }),
    update: (id: string, data: { role?: string; password?: string; is_active?: boolean }) =>
      request('PATCH', `/api/users/${id}`, data),
    changePassword: (currentPassword: string, newPassword: string) =>
      request('POST', '/api/users/me/password', { current_password: currentPassword, new_password: newPassword }),
  },
  admin: {
    getLogs: (lines = 5000) =>
      request<{ backend_log: string; nginx_access_log: string; nginx_error_log: string }>(
        'GET', `/api/admin/logs?lines=${lines}`
      ),
  },
  clusters: {
    list: () => request<import('./types').Cluster[]>('GET', '/api/clusters/'),
    create: (data: {
      display_name: string
      host: string
      port: number
      token?: string
      username?: string
      password?: string
      insecure: boolean
    }) => request<import('./types').Cluster>('POST', '/api/clusters/', data),
    update: (id: string, data: {
      display_name?: string
      host?: string
      port?: number
      token?: string
      username?: string
      password?: string
      insecure?: boolean
    }) => request<import('./types').Cluster>('PATCH', `/api/clusters/${id}`, data),
    delete: (id: string) => request('DELETE', `/api/clusters/${id}`),
    refresh: (id: string) =>
      request<{ cluster_name: string; snapshot_count: number }>('POST', `/api/clusters/${id}/refresh`),
  },
  inspect: {
    groups: (clusterId: string, olderThanDays = 90) =>
      request<{ cluster_name: string; groups: import('./types').SnapshotGroup[] }>(
        'GET', `/api/clusters/${clusterId}/groups?older_than_days=${olderThanDays}`
      ),
    curve: (clusterId: string, sourceFileId: string) =>
      request<{ rows: import('./types').ReclaimRow[]; points: import('./types').CurvePoint[]; unmeasured_pairs: number }>(
        'GET', `/api/clusters/${clusterId}/groups/${sourceFileId}/curve`
      ),
    snapshotSizes: (clusterId: string, sourceFileId: string) =>
      request<{ cluster_name: string; source_file_id: string; snapshots: import('./types').SnapshotSizeRow[]; last_run: import('./types').LastRun | null }>(
        'GET', `/api/clusters/${clusterId}/groups/${sourceFileId}/snapshots`
      ),
    startInspect: (clusterId: string, sourceFileId: string, path: string, includeHeld = false) =>
      request<{ job_id: string; reused: boolean }>(
        'POST', `/api/clusters/${clusterId}/inspect`, { source_file_id: sourceFileId, path, include_held: includeHeld }
      ),
    startSizeSnapshots: (clusterId: string, sourceFileId: string, path: string, includeHeld = false) =>
      request<{ job_id: string; reused: boolean }>(
        'POST', `/api/clusters/${clusterId}/groups/${sourceFileId}/size-snapshots`, { path, include_held: includeHeld }
      ),
    estimateDeletion: (clusterId: string, sourceFileId: string, snapshotIds: number[]) =>
      request<{ job_id: string; reused: boolean }>(
        'POST', `/api/clusters/${clusterId}/groups/${sourceFileId}/estimate-deletion`, { snapshot_ids: snapshotIds }
      ),
    cancelInspect: (clusterId: string, jobId: string) =>
      request<{ ok: boolean }>('POST', `/api/clusters/${clusterId}/jobs/${jobId}/cancel`),
    olderThan: (clusterId: string, sourceFileId: string, before: string) =>
      request<{ snapshot_ids: number[]; count: number }>(
        'GET', `/api/clusters/${clusterId}/older-than?source_file_id=${sourceFileId}&before=${before}`
      ),
    deleteSnapshots: (clusterId: string, snapshotIds: number[]) =>
      request<{ deleted: number[]; errors: { id: number; error: string }[] }>(
        'POST', `/api/clusters/${clusterId}/snapshots/delete`, { snapshot_ids: snapshotIds }
      ),
    startGoal: (clusterId: string, sourceFileIds: string[], targetBytes: number) =>
      request<{ job_id: string }>(
        'POST', `/api/clusters/${clusterId}/goal`, { source_file_ids: sourceFileIds, target_bytes: targetBytes }
      ),
  },
}
