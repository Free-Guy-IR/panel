import { setupColumns } from '@/components/users-table/columns'
import { DataTable } from '@/components/users-table/data-table'
import { Filters } from '@/components/users-table/filters'
import useDirDetection from '@/hooks/use-dir-detection'
import { useGetUsers } from '@/service/api'
import { useState } from 'react'
import { useTranslation } from 'react-i18next'
import { getUsersPerPageLimitSize } from '@/utils/userPreferenceStorage'

const UsersTable = () => {
  const { t } = useTranslation()
  const dir = useDirDetection()
  const [filters, setFilters] = useState({
    limit: getUsersPerPageLimitSize(),
    sort: '-created_at',
    load_sub: true
  })

  const { data: usersData } = useGetUsers(filters)

  const handleSort = (column: string) => {
    let newSort: string

    if (filters.sort === column) {
      newSort = '-' + column
    } else if (filters.sort === '-' + column) {
      newSort = '-created_at'
    } else {
      newSort = column
    }

    setFilters(prev => ({ ...prev, sort: newSort }))
  }

  const handleStatusFilter = (value: any) => {
    const newValue = value === '0' ? '' : value

    setFilters(prev => ({
      ...prev,
      status: value.length > 0 ? newValue : undefined,
    }))
  }

  const handleFilterChange = (newFilters: Partial<typeof filters>) => {
    setFilters(prev => ({ ...prev, ...newFilters }))
  }

  const columns = setupColumns({
    t,
    dir,
    handleSort,
    filters,
    handleStatusFilter,
  })

  return (
    <div>
      <Filters filters={filters} onFilterChange={handleFilterChange} />
      <DataTable columns={columns} data={usersData?.users || []} />
    </div>
  )
}

export default UsersTable
