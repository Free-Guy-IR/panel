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
