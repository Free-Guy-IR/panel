import { useQuery } from '@tanstack/react-query'
import { $fetch } from '@/service/http'

interface Goal {
  id: number
  name: string
  detail: string
  price: number
  paid_amount: number
  status: 'pending' | 'completed' | 'cancelled'
  created_at: string
  updated_at: string
}

interface GoalsResponse {
  next_pending: Goal[]
  last_completed: Goal[]
  last_cancelled: Goal[]
  pending_count: number
  completed_count: number
  cancelled_count: number
}

export function useCurrentGoal() {
  return useQuery({
    queryKey: ['current-goal'],
    queryFn: async () => {
      const response = await $fetch<Goal>('https://donate.pasarguard.org/api/v1/goal/current', {
        method: 'GET',
      })
      return response
    },
    refetchInterval: 60000, // Refetch every minute
    retry: 2,
  })
}

export function useAllGoals() {
  return useQuery({
    queryKey: ['all-goals'],
    queryFn: async () => {
      const response = await $fetch<GoalsResponse>('https://donate.pasarguard.org/api/v1/goal/list', {
        method: 'GET',
      })
      return response
    },
    refetchInterval: 300000, // Refetch every 5 minutes
    retry: 2,
  })
}
