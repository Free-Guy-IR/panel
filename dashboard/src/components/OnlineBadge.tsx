import { FC } from 'react'
import { dateUtils } from '../utils/dateFormatter'
import dayjs from '@/lib/dayjs'

type UserStatusProps = {
  lastOnline?: string | null
}

export const OnlineBadge: FC<UserStatusProps> = ({ lastOnline }) => {
  if (!lastOnline) {
    return <div className="min-h-[10px] min-w-[10px] rounded-full border border-gray-400 dark:border-gray-600 shadow-sm" />
  }

  const currentTime = dayjs()
  const lastOnlineTime = dateUtils.toDayjs(lastOnline)
  const diffInSeconds = currentTime.diff(lastOnlineTime, 'seconds')

  const isOnline = diffInSeconds <= 60

  if (isOnline) {
    // Online - green dot
    return <div className="min-h-[10px] min-w-[10px] rounded-full bg-green-500 shadow-sm" />
  } else {
    // Offline - gray dot
    return <div className="min-h-[10px] min-w-[10px] rounded-full bg-gray-400 dark:bg-gray-600 shadow-sm" />
  }
}
