import { useTranslation } from 'react-i18next'
import { Card, CardContent } from '@/components/ui/card'
import { useEffect, useState, useRef } from 'react'
import { useGetNodes } from '@/service/api'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { Label } from '@/components/ui/label'
import { getAuthToken } from '@/utils/authStorage'
import { EventSource } from 'eventsource'
import { ChevronDown, ArrowDown, Terminal, Clock, Search, ArrowDownCircle, FilterIcon, XIcon } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'
import { Switch } from '@/components/ui/switch'
import { Input } from '@/components/ui/input'
import { DropdownMenu, DropdownMenuContent, DropdownMenuCheckboxItem, DropdownMenuTrigger } from '@/components/ui/dropdown-menu'
import useDirDetection from '@/hooks/use-dir-detection'
import { cn } from '@/lib/utils'

// define EventSource globally
globalThis.EventSource = EventSource

type LogLevel = 'debug' | 'info' | 'warning' | 'error' | 'none'

interface LogEntry {
  timestamp: string | number
  message: string
  level: LogLevel
}

export default function NodeLogs() {
  const { t } = useTranslation()
  const dir = useDirDetection()
  const [selectedNode, setSelectedNode] = useState<number>(0)
  const [logs, setLogs] = useState<LogEntry[]>([])
  const [filteredLogs, setFilteredLogs] = useState<LogEntry[]>([])
  const [isLoading, setIsLoading] = useState(true)
  const [autoScroll, setAutoScroll] = useState(true)
  const [showTimestamps, setShowTimestamps] = useState(false)
  const [selectedLevels, setSelectedLevels] = useState<LogLevel[]>(['debug', 'info', 'warning', 'error'])
  const [searchQuery, setSearchQuery] = useState('')

  const eventSourceRef = useRef<EventSource | null>(null)
  const logsContainerRef = useRef<HTMLDivElement>(null)

  const { data: nodes = [] } = useGetNodes({})

  const logLevelColors = {
    debug: 'text-gray-500 dark:text-gray-400',
    info: 'text-blue-600 dark:text-blue-400',
    warning: 'text-amber-600 dark:text-amber-400',
    error: 'text-red-600 dark:text-red-400',
    none: 'text-foreground'
  }

  const logBadgeColors = {
    debug: 'bg-gray-100 text-gray-800 dark:bg-gray-800 dark:text-gray-300 border-gray-300 dark:border-gray-600',
    info: 'bg-blue-100 text-blue-800 dark:bg-blue-900 dark:text-blue-300 border-blue-300 dark:border-blue-700',
    warning: 'bg-amber-100 text-amber-800 dark:bg-amber-900 dark:text-amber-300 border-amber-300 dark:border-amber-700',
    error: 'bg-red-100 text-red-800 dark:bg-red-900 dark:text-red-300 border-red-300 dark:border-red-700',
    none: 'bg-gray-100 text-gray-800 dark:bg-gray-800 dark:text-gray-300 border-gray-300 dark:border-gray-600'
  }

  const logIconColors = {
    debug: 'text-gray-500 dark:text-gray-400',
    info: 'text-blue-500 dark:text-blue-400',
    warning: 'text-amber-500 dark:text-amber-400',
    error: 'text-red-500 dark:text-red-400',
    none: 'text-gray-500 dark:text-gray-400'
  }

  const determineLogLevel = (message: string): LogLevel => {
    const lowerMessage = message.toLowerCase()
    if (lowerMessage.includes('[error]')) return 'error'
    if (lowerMessage.includes('[warning]') || lowerMessage.includes('[warn]')) return 'warning'
    if (lowerMessage.includes('[info]')) return 'info'
    if (lowerMessage.includes('[debug]')) return 'debug'
    return 'none'
  }

  useEffect(() => {
    if (selectedNode === 0) {
      setIsLoading(false);
      return;
    }
    setIsLoading(true);
    setLogs([]);

    const baseUrl = (import.meta.env.VITE_BASE_API && typeof import.meta.env.VITE_BASE_API === 'string' && import.meta.env.VITE_BASE_API.trim() !== '/' && import.meta.env.VITE_BASE_API.startsWith('http')) ? import.meta.env.VITE_BASE_API : window.location.origin
    const token = getAuthToken()
    const eventSource = new EventSource(`${baseUrl}/api/node/${selectedNode}/logs`, {
      fetch: (input: Request | URL | string, init?: RequestInit) => {
        const headers = new Headers(init?.headers || {})
        headers.set('Authorization', `Bearer ${token}`)
        return fetch(input, {
          ...init,
          headers,
        })
      }
    } as any)
    eventSourceRef.current = eventSource

    eventSource.onmessage = (event) => {
      let logEntry: Partial<LogEntry>
      try {
        logEntry = JSON.parse(event.data)
      } catch {
        // If not JSON, treat as plain string
        logEntry = {
          timestamp: Date.now(),
          message: event.data,
        }
      }

      // Determine log level from message
      const level = determineLogLevel(logEntry.message || '')

      const newEntry: LogEntry = {
        timestamp: logEntry.timestamp || Date.now(),
        message: logEntry.message || '',
        level
      }

      setLogs(prevLogs => {
        const newLogs = [...prevLogs, newEntry]
        return newLogs.slice(-1000)
      })
    }

    eventSource.onerror = (error) => {
      console.error('SSE Error:', error)
      eventSource.close()
      setIsLoading(false)
    }

    eventSource.onopen = () => {
      console.log('SSE connection opened')
      setIsLoading(false)
    }

    // Cleanup on unmount
    return () => {
      if (eventSourceRef.current) {
        eventSourceRef.current.close()
      }
    }
  }, [selectedNode])

  // Filter logs based on selected levels and search query
  useEffect(() => {
    setFilteredLogs(logs.filter(log =>
      selectedLevels.includes(log.level) &&
      (searchQuery === '' || log.message.toLowerCase().includes(searchQuery.toLowerCase()))
    ))
  }, [logs, selectedLevels, searchQuery])

  // Auto-scroll to bottom when new logs arrive
  useEffect(() => {
    if (autoScroll && logsContainerRef.current) {
      setTimeout(() => {
        if (logsContainerRef.current) {
          logsContainerRef.current.scrollTop = logsContainerRef.current.scrollHeight
        }
      }, 100)
    }
  }, [filteredLogs, autoScroll])

  const scrollToBottom = () => {
    if (logsContainerRef.current) {
      logsContainerRef.current.scrollTop = logsContainerRef.current.scrollHeight
    }
  }

  const toggleLogLevel = (level: LogLevel) => {
    setSelectedLevels(prev =>
      prev.includes(level)
        ? prev.filter(l => l !== level)
        : [...prev, level]
    )
  }

  const clearLogs = () => {
    setLogs([])
  }

  // Format timestamp function
  const formatTimestamp = (timestamp: string | number) => {
    const date = new Date(timestamp);
    return date.toLocaleTimeString(undefined, {
      hour: '2-digit',
      minute: '2-digit',
      second: '2-digit',
      hour12: false
    });
  };

  const getLevelName = (level: LogLevel) => {
    const keys = {
      debug: t('nodes.logs.debug', { defaultValue: 'DEBUG' }),
      info: t('nodes.logs.info', { defaultValue: 'INFO' }),
      warning: t('nodes.logs.warning', { defaultValue: 'WARNING' }),
      error: t('nodes.logs.error', { defaultValue: 'ERROR' }),
      none: ''
    };
    return keys[level].toUpperCase();
  };

  return (
    <div className={cn("flex flex-col gap-2 w-full", dir === "rtl" && "rtl")}>
      <div className="w-full pt-2">
        <div className={cn("flex flex-col space-y-4 md:space-y-0 md:flex-row md:items-end md:justify-between gap-4 mb-4 py-2")}>
          <div className={cn("flex flex-col sm:flex-row items-start sm:items-center gap-4")}>
            <div className="w-full sm:w-auto">
              <Label htmlFor="node-select" className="mb-1 block text-sm">{t('nodes.title')}</Label>
              <Select
                value={selectedNode.toString()}
                onValueChange={(value) => setSelectedNode(Number(value))}
              >
                <SelectTrigger id="node-select" className="w-full sm:w-[200px] h-8 text-xs sm:text-sm">
                  <SelectValue placeholder={t('nodes.selectNode')} />
                </SelectTrigger>
                <SelectContent>
                  {nodes.map((node) => (
                    <SelectItem key={node.id} value={node.id.toString()} className="text-xs sm:text-sm">
                      {node.name}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>

            <div className="w-full sm:w-auto">
              <Label htmlFor="log-levels" className="mb-1 block text-sm">{t('nodes.logs.filter', { defaultValue: 'Filter Logs' })}</Label>
              <DropdownMenu>
                <DropdownMenuTrigger asChild>
                  <Button variant="outline" className="w-full sm:w-auto flex items-center justify-between gap-2 h-8 text-xs sm:text-sm">
                    <div className="flex items-center gap-1">
                      <FilterIcon size={12} className="mr-1" />
                      <span>{t('nodes.logs.levels', { defaultValue: 'Log Levels' })}</span>
                      <Badge variant="secondary" className="ml-1 text-xs">
                        {selectedLevels.length}
                      </Badge>
                    </div>
                    <ChevronDown size={14} />
                  </Button>
                </DropdownMenuTrigger>
                <DropdownMenuContent align="start" className="w-[180px]">
                  {(['debug', 'info', 'warning', 'error'] as LogLevel[]).map(level => (
                    <DropdownMenuCheckboxItem
                      key={level}
                      checked={selectedLevels.includes(level)}
                      onCheckedChange={() => toggleLogLevel(level)}
                      className="flex items-center gap-2 text-xs sm:text-sm"
                    >
                      <div className="flex items-center gap-2 w-full">
                        <Badge variant="outline" className={cn("shrink-0", logBadgeColors[level], "text-xs")}>
                          {getLevelName(level)}
                        </Badge>
                      </div>
                    </DropdownMenuCheckboxItem>
                  ))}
                </DropdownMenuContent>
              </DropdownMenu>
            </div>

            <div className="w-full sm:w-auto">
              <Label htmlFor="search-logs" className="mb-1 block text-sm">{t('search')}</Label>
              <div className="relative flex items-center">
                <Search className="absolute left-2 h-3 w-3 text-muted-foreground" />
                <Input
                  id="search-logs"
                  placeholder={t('nodes.logs.search', { defaultValue: 'Search logs' })}
                  value={searchQuery}
                  onChange={(e) => setSearchQuery(e.target.value)}
                  className="pl-7 w-full sm:w-[220px] h-8 text-xs sm:text-sm"
                />
              </div>
            </div>
          </div>

          <div className={cn("flex flex-wrap items-center gap-2", dir === "rtl" && "flex-row-reverse")}>
            <div className={cn("flex items-center gap-1 mr-1", dir === "rtl" && "flex-row-reverse ml-1 mr-0")}>
              <Label htmlFor="show-timestamps" className="text-xs whitespace-nowrap">
                <Clock size={10} className={cn("inline mr-1 opacity-70", dir === "rtl" && "ml-1 mr-0")} />
                {t('nodes.logs.timestamps', { defaultValue: 'Timestamps' })}
              </Label>
              <Switch
                id="show-timestamps"
                checked={showTimestamps}
                onCheckedChange={setShowTimestamps}
                className="scale-75 sm:scale-90"
              />
            </div>

            <div className={cn("flex items-center gap-1 mr-1", dir === "rtl" && "flex-row-reverse ml-1 mr-0")}>
              <Label htmlFor="auto-scroll" className="text-xs whitespace-nowrap">
                <ArrowDownCircle size={10} className={cn("inline mr-1 opacity-70", dir === "rtl" && "ml-1 mr-0")} />
                {t('nodes.logs.autoScroll', { defaultValue: 'Auto Scroll' })}
              </Label>
              <Switch
                id="auto-scroll"
                checked={autoScroll}
                onCheckedChange={setAutoScroll}
                className="scale-75 sm:scale-90"
              />
            </div>

            <Button
              onClick={scrollToBottom}
              size="icon"
              variant="outline"
              className="flex items-center gap-1 text-xs"
              title={t('nodes.logs.scrollToEnd', { defaultValue: 'Scroll to End' })}
            >
              <ArrowDown />
            </Button>

            <Button
              onClick={clearLogs}
              size="icon"
              variant="outline"
              className="flex items-center gap-1 text-xs"
              title={t('nodes.logs.clear', { defaultValue: 'Clear Logs' })}
            >
              <XIcon />
            </Button>
          </div>
        </div>

        <Card className="transform-gpu animate-slide-up" style={{ animationDuration: '500ms', animationFillMode: 'both' }}>
          <CardContent dir="ltr" className="p-4">
            <div className="h-[600px] w-full rounded-md overflow-auto" ref={logsContainerRef}>
              <div className="p-1">
                {isLoading ? (
                  <div className="flex items-center justify-center h-full animate-pulse">
                    <p className="text-muted-foreground">{t('loading')}</p>
                  </div>
                ) : selectedNode === 0 ? (
                  <div className="flex items-center justify-center h-full">
                    <p className="text-muted-foreground">{t('nodes.selectNode')}</p>
                  </div>
                ) : filteredLogs.length > 0 ? (
                  <div>
                    {filteredLogs.map((log, index) => (
                      <div
                        key={`${log.timestamp}-${index}`}
                        className="text-sm font-mono p-2 mb-1 border-b border-muted last:border-b-0 hover:bg-muted/20 transition-colors"
                      >
                        <div className="flex flex-wrap items-start gap-2">
                          {showTimestamps && (
                            <div className="shrink-0 text-xs text-muted-foreground flex items-center gap-1 min-w-[85px]">
                              <Clock size={10} className="opacity-60" />
                              {formatTimestamp(log.timestamp)}
                            </div>
                          )}
                          <Badge variant="outline" className={`shrink-0 ${logBadgeColors[log.level]}`}>
                            <Terminal size={12} className={`mr-1 ${logIconColors[log.level]}`} />
                            <span className={cn(logLevelColors[log.level], "text-xs font-body")}>{getLevelName(log.level)}</span>
                          </Badge>
                          <span className="break-words text-foreground">
                            {log.message}
                          </span>
                        </div>
                      </div>
                    ))}
                  </div>
                ) : (
                  <div className="flex items-center justify-center h-full">
                    <p className="text-muted-foreground">{t('nodes.logs.noLogs')}</p>
                  </div>
                )}
              </div>
            </div>
          </CardContent>
        </Card>
      </div>
    </div>
  )
} 