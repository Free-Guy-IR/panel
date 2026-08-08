import { useConnectionStatesForUsers } from '@/service/api'
import type { ConnectionStateResponse } from '@/service/api'
import { createContext, useContext, useMemo, type ReactNode } from 'react'

interface DeviceCounts {
  byUser: Map<number, ConnectionStateResponse>
  deviceLimit: number
  enabled: boolean
  isFetching: boolean
  refresh: () => void
}

const DeviceCountsContext = createContext<DeviceCounts>({
  byUser: new Map(),
  deviceLimit: 0,
  enabled: false,
  isFetching: false,
  refresh: () => {},
})

export const useDeviceCounts = () => useContext(DeviceCountsContext)

/**
 * Fetches device counts for the rows currently on screen.
 *
 * Keyed on the ids the table is already showing, so a page costs one request
 * rather than one per row - which on a table this size is the difference
 * between usable and not. The numbers come from what the background job last
 * recorded; nothing here talks to a node.
 */
export function DeviceCountsProvider({
  userIds,
  refetchInterval,
  children,
}: {
  userIds: number[]
  refetchInterval: number | false
  children: ReactNode
}) {
  const ids = useMemo(() => [...userIds].sort((a, b) => a - b), [userIds])

  const { data, isFetching, refetch } = useConnectionStatesForUsers(
    { user_ids: ids },
    {
      query: {
        enabled: ids.length > 0,
        refetchInterval,
        refetchOnWindowFocus: false,
        // Keep the previous numbers visible while the next poll is in flight,
        // so badges do not blink out on every refresh.
        placeholderData: previous => previous,
      },
    },
  )

  const value = useMemo<DeviceCounts>(() => {
    const byUser = new Map<number, ConnectionStateResponse>()
    for (const state of data?.states ?? []) {
      byUser.set(state.user_id, state)
    }
    return {
      byUser,
      deviceLimit: data?.device_limit ?? 0,
      enabled: data?.enabled ?? false,
      isFetching,
      refresh: () => void refetch(),
    }
  }, [data, isFetching, refetch])

  return <DeviceCountsContext.Provider value={value}>{children}</DeviceCountsContext.Provider>
}
