import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Pagination, PaginationContent, PaginationEllipsis, PaginationItem, PaginationLink, PaginationNext, PaginationPrevious } from '@/components/ui/pagination'
import { Select, SelectContent, SelectGroup, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { Badge } from '@/components/ui/badge'
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/popover'
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuTrigger, DropdownMenuSeparator, DropdownMenuLabel } from '@/components/ui/dropdown-menu'
import useDirDetection from '@/hooks/use-dir-detection'
import { cn } from '@/lib/utils'
import { debounce } from 'es-toolkit'
import { RefreshCw, SearchIcon, Filter, X, ArrowUpDown, User, Calendar, ChartPie, ChevronDown } from 'lucide-react'
import { useState, useMemo } from 'react'
import { useTranslation } from 'react-i18next'
import { useGetUsers, UserStatus } from '@/service/api'
import { RefetchOptions } from '@tanstack/react-query'
import { LoaderCircle } from 'lucide-react'
import { UseFormReturn } from 'react-hook-form'

// Sort configuration to eliminate duplication
const sortSections = [
  {
    key: 'username',
    icon: User,
    label: 'username',
    items: [
      { value: 'username', label: 'sort.username.asc' },
      { value: '-username', label: 'sort.username.desc' },
    ],
  },
  {
    key: 'expire',
    icon: Calendar,
    label: 'expireDate',
    items: [
      { value: 'expire', label: 'sort.expire.oldest' },
      { value: '-expire', label: 'sort.expire.newest' },
    ],
  },
  {
    key: 'usage',
    icon: ChartPie,
    label: 'dataUsage',
    items: [
      { value: 'used_traffic', label: 'sort.usage.low' },
      { value: '-used_traffic', label: 'sort.usage.high' },
    ],
  },
] as const

interface FiltersProps {
  filters: {
    search?: string
    limit?: number
    offset?: number
    sort: string
    status?: UserStatus | null
    load_sub: boolean
  }
  onFilterChange: (filters: Partial<FiltersProps['filters']>) => void
  refetch?: (options?: RefetchOptions) => Promise<unknown>
  advanceSearchOnOpen: (status: boolean) => void
  advanceSearchForm?: UseFormReturn<Record<string, unknown>>
  onClearAdvanceSearch?: () => void
  handleSort?: (column: string, fromDropdown?: boolean) => void
}

export const Filters = ({ filters, onFilterChange, refetch, advanceSearchOnOpen, advanceSearchForm, onClearAdvanceSearch, handleSort }: FiltersProps) => {
  const { t } = useTranslation()
  const dir = useDirDetection()
  const [search, setSearch] = useState(filters.search || '')
  const [isRefreshing, setIsRefreshing] = useState(false)
  const userQuery = useGetUsers(filters)
  const handleRefetch = refetch || userQuery.refetch

  // Ultra-fast debounced search function
  const debouncedFilterChange = useMemo(
    () =>
      debounce((value: string) => {
        onFilterChange({
          search: value,
          offset: 0, // Reset to first page when search is updated
        })
      }, 25), // Ultra-fast debounce
    [onFilterChange],
  )

  // Handle input change
  const handleSearchChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    const value = e.target.value
    setSearch(value)
    debouncedFilterChange(value)
  }

  // Clear search field
  const clearSearch = () => {
    setSearch('')
    onFilterChange({
      search: '',
      offset: 0,
    })
  }

  // Handle refresh with loading state
  const handleRefreshClick = async () => {
    setIsRefreshing(true)
    try {
      await handleRefetch()
    } finally {
      // Instant response - no delay
      setIsRefreshing(false)
    }
  }

  const handleOpenAdvanceSearch = () => {
    advanceSearchOnOpen(true)
  }

  // Check if any advance search filters are active
  const hasActiveAdvanceFilters = () => {
    if (!advanceSearchForm) return false
    const values = advanceSearchForm.getValues() as Record<string, unknown>
    const admin = values.admin as string[] | undefined
    const group = values.group as string[] | undefined
    const status = values.status as string | undefined
    return (admin && admin.length > 0) || (group && group.length > 0) || status !== '0'
  }

  // Get the count of active advance filters
  const getActiveFiltersCount = () => {
    if (!advanceSearchForm) return 0
    const values = advanceSearchForm.getValues() as Record<string, unknown>
    const admin = values.admin as string[] | undefined
    const group = values.group as string[] | undefined
    const status = values.status as string | undefined
    let count = 0
    if (admin && admin.length > 0) count++
    if (group && group.length > 0) count++
    if (status !== '0') count++
    return count
  }

  return (
    <div dir={dir} className="flex items-center gap-4 py-4">
      {/* Search Input */}
      <div className="relative w-full md:w-[calc(100%/3-10px)]">
        <SearchIcon className={cn('absolute', dir === 'rtl' ? 'right-2' : 'left-2', 'top-1/2 h-4 w-4 -translate-y-1/2 text-gray-400 text-input-placeholder')} />
        <Input placeholder={t('search')} value={search} onChange={handleSearchChange} className="pl-8 pr-10" />
        {search && (
          <button onClick={clearSearch} className={cn('absolute', dir === 'rtl' ? 'left-2' : 'right-2', 'top-1/2 -translate-y-1/2 text-gray-400 hover:text-gray-600')}>
            <X className="h-4 w-4" />
          </button>
        )}
      </div>
      <div className="flex h-full items-center gap-1">
        <Button size="icon-md" variant="ghost" className="relative flex items-center gap-2 border" onClick={handleOpenAdvanceSearch}>
          <Filter className="h-4 w-4" />
          {hasActiveAdvanceFilters() && (
            <Badge variant="destructive" className="absolute -right-1 -top-1 flex h-4 w-4 items-center justify-center rounded-full p-0 text-xs">
              {getActiveFiltersCount()}
            </Badge>
          )}
        </Button>
        {hasActiveAdvanceFilters() && onClearAdvanceSearch && (
          <Popover>
            <PopoverTrigger asChild>
              <Button size="sm" variant="outline" className={cn('h-8 w-8 p-0', dir === 'rtl' ? 'rounded-r-none border-r-0' : 'rounded-l-none border-l-0')} onClick={onClearAdvanceSearch}>
                <X className="h-3 w-3" />
              </Button>
            </PopoverTrigger>
            <PopoverContent className="w-auto p-2" side={dir === 'rtl' ? 'left' : 'right'} align="center">
              <p className="text-sm">{t('clearAllFilters', { defaultValue: 'Clear All Filters' })}</p>
            </PopoverContent>
          </Popover>
        )}
      </div>
      {/* Sort Button */}
      {handleSort && (
        <div className="flex h-full items-center gap-1">
          <DropdownMenu>
            <DropdownMenuTrigger asChild>
              <Button size="icon-md" variant="ghost" className="relative flex items-center gap-2 border" aria-label={t('sortOptions', { defaultValue: 'Sort Options' })}>
                <ArrowUpDown className="h-4 w-4" />
                {filters.sort && filters.sort !== '-created_at' && <div className="absolute -right-1 -top-1 h-2 w-2 rounded-full bg-primary" />}
              </Button>
            </DropdownMenuTrigger>
            <DropdownMenuContent align="end" className="w-52 md:w-56">
              {sortSections.map((section, sectionIndex) => (
                <div key={section.key}>
                  {/* Section Label */}
                  <DropdownMenuLabel className="flex items-center gap-1.5 px-2 py-1.5 text-xs text-muted-foreground md:gap-2 md:px-3 md:py-2">
                    <section.icon className="h-3 w-3" />
                    <span className="text-xs md:text-sm">{t(section.label)}</span>
                  </DropdownMenuLabel>

                  {/* Section Items */}
                  {section.items.map(item => (
                    <DropdownMenuItem
                      key={item.value}
                      onClick={() => handleSort && handleSort(item.value, true)}
                      className={`whitespace-nowrap px-2 py-1.5 text-xs md:px-3 md:py-2 ${filters.sort === item.value ? 'bg-accent' : ''}`}
                    >
                      <section.icon className="mr-1.5 h-3 w-3 flex-shrink-0 md:mr-2 md:h-4 md:w-4" />
                      <span className="truncate">{t(item.label)}</span>
                      {filters.sort === item.value && <ChevronDown className={`ml-auto h-3 w-3 flex-shrink-0 md:h-4 md:w-4 ${item.value.startsWith('-') ? '' : 'rotate-180'}`} />}
                    </DropdownMenuItem>
                  ))}

                  {/* Add separator except for last section */}
                  {sectionIndex < sortSections.length - 1 && <DropdownMenuSeparator />}
                </div>
              ))}
            </DropdownMenuContent>
          </DropdownMenu>
        </div>
      )}
      {/* Refresh Button */}
      <div className="flex h-full items-center gap-2">
        <Button size="icon-md" onClick={handleRefreshClick} variant="ghost" className="flex items-center gap-2 border" disabled={isRefreshing}>
          <RefreshCw className={cn('h-4 w-4', isRefreshing && 'animate-spin')} />
        </Button>
      </div>
    </div>
  )
}

interface PaginationControlsProps {
  currentPage: number
  totalPages: number
  itemsPerPage: number
  totalUsers: number
  isLoading: boolean
  onPageChange: (page: number) => void
  onItemsPerPageChange: (value: number) => void
}

export const PaginationControls = ({ currentPage, totalPages, itemsPerPage, isLoading, onPageChange, onItemsPerPageChange }: PaginationControlsProps) => {
  const { t } = useTranslation()

  const getPaginationRange = (currentPage: number, totalPages: number) => {
    const delta = 2 // Number of pages to show on each side of current page
    const range = []

    // Handle small number of pages
    if (totalPages <= 5) {
      for (let i = 0; i < totalPages; i++) {
        range.push(i)
      }
      return range
    }

    // Always include first and last page
    range.push(0)

    // Calculate start and end of range
    let start = Math.max(1, currentPage - delta)
    let end = Math.min(totalPages - 2, currentPage + delta)

    // Adjust range if current page is near start or end
    if (currentPage - delta <= 1) {
      end = Math.min(totalPages - 2, start + 2 * delta)
    }
    if (currentPage + delta >= totalPages - 2) {
      start = Math.max(1, totalPages - 3 - 2 * delta)
    }

    // Add ellipsis if needed
    if (start > 1) {
      range.push(-1) // -1 represents ellipsis
    }

    // Add pages in range
    for (let i = start; i <= end; i++) {
      range.push(i)
    }

    // Add ellipsis if needed
    if (end < totalPages - 2) {
      range.push(-1) // -1 represents ellipsis
    }

    // Add last page
    if (totalPages > 1) {
      range.push(totalPages - 1)
    }

    return range
  }

  const paginationRange = getPaginationRange(currentPage, totalPages)
  const dir = useDirDetection()
  return (
    <div className="mt-4 flex flex-col-reverse items-center justify-between gap-4 md:flex-row">
      <div className="flex items-center gap-2">
        <Select value={itemsPerPage.toString()} onValueChange={value => onItemsPerPageChange(parseInt(value, 10))} disabled={isLoading}>
          <SelectTrigger className="w-[70px]">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectGroup>
              <SelectItem value="10">10</SelectItem>
              <SelectItem value="20">20</SelectItem>
              <SelectItem value="30">30</SelectItem>
              <SelectItem value="40">40</SelectItem>
              <SelectItem value="50">50</SelectItem>
            </SelectGroup>
          </SelectContent>
        </Select>
        <span className="whitespace-nowrap text-sm text-muted-foreground">{t('itemsPerPage')}</span>
      </div>

      <Pagination dir="ltr" className={`md:justify-end ic ${dir === 'rtl' ? 'flex-row-reverse' : ''}`}>
        <PaginationContent className={cn("w-full overflow-x-auto justify-center", dir === 'rtl' ? 'md:justify-start' : 'md:justify-end')}>
          <PaginationItem>
            <PaginationPrevious onClick={() => onPageChange(currentPage - 1)} disabled={currentPage === 0 || isLoading} />
          </PaginationItem>
          {paginationRange.map((pageNumber, i) =>
            pageNumber === -1 ? (
              <PaginationItem key={`ellipsis-${i}`}>
                <PaginationEllipsis />
              </PaginationItem>
            ) : (
              <PaginationItem key={pageNumber}>
                <PaginationLink
                  isActive={currentPage === pageNumber}
                  onClick={() => onPageChange(pageNumber as number)}
                  disabled={isLoading}
                  className={isLoading && currentPage === pageNumber ? 'opacity-70' : ''}
                >
                  {isLoading && currentPage === pageNumber ? (
                    <div className="flex items-center">
                      <LoaderCircle className="mr-1 h-3 w-3 animate-spin" />
                      {(pageNumber as number) + 1}
                    </div>
                  ) : (
                    (pageNumber as number) + 1
                  )}
                </PaginationLink>
              </PaginationItem>
            ),
          )}
          <PaginationItem>
            <PaginationNext onClick={() => onPageChange(currentPage + 1)} disabled={currentPage === totalPages - 1 || totalPages === 0 || isLoading} />
          </PaginationItem>
        </PaginationContent>
      </Pagination>
    </div>
  )
}
