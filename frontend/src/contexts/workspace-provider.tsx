import { useCallback, useEffect, useMemo, useState, type ReactNode } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { useAuth } from '@/contexts/auth-context'
import { workspaces as workspacesApi, WORKSPACE_STORAGE_KEY } from '@/lib/api'
import type { ModuleId } from '@/lib/modules'
import type { Workspace } from '@/types'

import { WorkspaceContext } from '@/contexts/workspace-context'

export function WorkspaceProvider({ children }: { children: ReactNode }) {
  const { user, token, isLoading: authLoading } = useAuth()
  const [list, setList] = useState<Workspace[]>([])
  const [currentId, setCurrentId] = useState<string | null>(() => localStorage.getItem(WORKSPACE_STORAGE_KEY))
  const [isLoading, setIsLoading] = useState(true)
  const queryClient = useQueryClient()

  const applyWorkspaces = useCallback((fetched: Workspace[]) => {
    setList(fetched)
    // Reconcile the stored selection against what's actually accessible.
    // If the stored ID is stale (workspace archived, user removed, etc.)
    // fall back to the first one.
    const storedId = localStorage.getItem(WORKSPACE_STORAGE_KEY)
    const found = fetched.find((w) => w.id === storedId)
    if (found) {
      setCurrentId(found.id)
    } else if (fetched.length > 0) {
      const fallbackId = fetched[0].id
      localStorage.setItem(WORKSPACE_STORAGE_KEY, fallbackId)
      setCurrentId(fallbackId)
    } else {
      localStorage.removeItem(WORKSPACE_STORAGE_KEY)
      setCurrentId(null)
    }
    setIsLoading(false)
  }, [])

  const refresh = useCallback(async () => {
    setIsLoading(true)
    try {
      applyWorkspaces(await workspacesApi.list())
    } catch {
      setList([])
      setIsLoading(false)
    }
  }, [applyWorkspaces])

  const userId = user?.id
  const [authSource, setAuthSource] = useState<{ authLoading: boolean; userId: typeof userId; token: typeof token } | null>(null)
  if (!authSource || authSource.authLoading !== authLoading || authSource.userId !== userId || authSource.token !== token) {
    setAuthSource({ authLoading, userId, token })
    if (!authLoading && (!user || !token)) {
      setList([])
      setCurrentId(null)
      setIsLoading(false)
    } else {
      setIsLoading(true)
    }
  }

  useEffect(() => {
    if (authLoading || !user || !token) return
    let cancelled = false
    workspacesApi.list()
      .then((fetched) => { if (!cancelled) applyWorkspaces(fetched) })
      .catch(() => {
        if (cancelled) return
        setList([])
        setIsLoading(false)
      })
    return () => { cancelled = true }
  }, [authLoading, user, token, applyWorkspaces])

  const switchWorkspace = useCallback(
    async (id: string) => {
      if (id === currentId) return
      // Persist FIRST so the axios interceptor sends the new
      // workspace_id on every refetch fired below.
      localStorage.setItem(WORKSPACE_STORAGE_KEY, id)
      setCurrentId(id)
      // Every cached query was scoped to the previous workspace.
      // `resetQueries` flushes cached data AND refetches active
      // observers in one call — `clear()` alone removed data without
      // triggering refetches (mounted components kept their previous
      // render until a manual reload).
      await queryClient.resetQueries()
    },
    [currentId, queryClient],
  )

  const current = useMemo(
    () => list.find((w) => w.id === currentId) ?? null,
    [list, currentId],
  )

  const role = current?.role ?? null
  const canManage = role === 'owner' || role === 'manager'
  const canWrite = role === 'owner' || role === 'manager' || role === 'editor'

  const enabledModules = useMemo(
    () => (current?.enabled_modules ?? []) as ModuleId[],
    [current],
  )
  const hasModule = useCallback(
    (id: ModuleId) => enabledModules.includes(id),
    [enabledModules],
  )

  return (
    <WorkspaceContext.Provider
      value={{
        current,
        workspaces: list,
        isLoading,
        switchWorkspace,
        refresh,
        role,
        canManage,
        canWrite,
        enabledModules,
        hasModule,
      }}
    >
      {children}
    </WorkspaceContext.Provider>
  )
}
