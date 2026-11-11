import { useAdmin } from '@/hooks/use-admin'
import type { AdminDetails } from '@/service/api'
import { hasPermission } from '@/utils/rbac'

export function useCanDeleteHosts(): boolean {
  const { admin } = useAdmin()
  return hasPermission(admin as unknown as AdminDetails | null, 'hosts', 'delete')
}
