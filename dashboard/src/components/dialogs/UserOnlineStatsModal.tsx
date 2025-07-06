import { useState, useEffect } from 'react'
import { Dialog, DialogContent, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Badge } from '@/components/ui/badge'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { useTranslation } from 'react-i18next'
import { useUserOnlineStats, useUserOnlineIpList } from '@/service/api'
import { cn } from '@/lib/utils'
import useDirDetection from '@/hooks/use-dir-detection'
import { 
    Users, 
    Search, 
    RefreshCw, 
    Loader2, 
    Globe, 
    MapPin, 
    Activity,
    Eye,
    AlertCircle
} from 'lucide-react'
import { toast } from 'sonner'
import { ScrollArea } from '@/components/ui/scroll-area'
import { Separator } from '@/components/ui/separator'
import { formatBytes } from '@/utils/formatByte'

interface UserOnlineStatsDialogProps {
    isOpen: boolean
    onOpenChange: (open: boolean) => void
    nodeId: number | null
    nodeName?: string
}

interface UserStatsCardProps {
    username: string
    stats: { [key: string]: number }
    nodeId: number
    onViewIPs: (username: string) => void
}

const UserStatsCard = ({ username, stats, nodeId, onViewIPs }: UserStatsCardProps) => {
    const { t } = useTranslation()
    const dir = useDirDetection()
    
    const totalConnections = Object.values(stats).reduce((sum, val) => sum + val, 0)
    const activeProtocols = Object.keys(stats).filter(proto => stats[proto] > 0)

    return (
        <Card className="hover:bg-accent/50 transition-colors">
            <CardHeader className="pb-3">
                <div className="flex items-center justify-between">
                    <CardTitle className="text-sm font-medium flex items-center gap-2">
                        <Activity className="h-4 w-4 text-green-500" />
                        {username}
                    </CardTitle>
                    <Badge variant="secondary" className="text-xs">
                        {totalConnections} {t('nodeModal.onlineStats.connections', { defaultValue: 'connections' })}
                    </Badge>
                </div>
            </CardHeader>
            <CardContent className="pt-0">
                <div className="space-y-2">
                    <div className="flex flex-wrap gap-1">
                        {activeProtocols.map(protocol => (
                            <Badge key={protocol} variant="outline" className="text-xs">
                                {protocol}: {stats[protocol]}
                            </Badge>
                        ))}
                    </div>
                    <div className="flex justify-between items-center pt-2">
                        <div className="text-xs text-muted-foreground">
                            {activeProtocols.length} {t('nodeModal.onlineStats.protocols', { defaultValue: 'protocols' })}
                        </div>
                        <Button
                            variant="ghost"
                            size="sm"
                            onClick={() => onViewIPs(username)}
                            className="h-6 text-xs"
                        >
                            <Eye className="h-3 w-3 mr-1" />
                            {t('nodeModal.onlineStats.viewIPs', { defaultValue: 'View IPs' })}
                        </Button>
                    </div>
                </div>
            </CardContent>
        </Card>
    )
}

export default function UserOnlineStatsModal({ 
    isOpen, 
    onOpenChange, 
    nodeId, 
    nodeName 
}: UserOnlineStatsDialogProps) {
    const { t } = useTranslation()
    const dir = useDirDetection()
    const [searchTerm, setSearchTerm] = useState('')
    const [specificUsername, setSpecificUsername] = useState('')
    const [refreshing, setRefreshing] = useState(false)
    const [viewingIPs, setViewingIPs] = useState<string | null>(null)

    // Reset state when modal closes
    useEffect(() => {
        if (!isOpen) {
            setSearchTerm('')
            setSpecificUsername('')
            setViewingIPs(null)
            setRefreshing(false)
        }
    }, [isOpen])

    // Query for specific user stats (when searching for a specific user)
    const { 
        data: userStats, 
        isLoading: isLoadingUserStats,
        error: userStatsError,
        refetch: refetchUserStats 
    } = useUserOnlineStats(
        nodeId || 0, 
        specificUsername, 
        { 
            query: { 
                enabled: !!(isOpen && nodeId && specificUsername),
                refetchInterval: isOpen ? 5000 : false // Only refresh when modal is open
            } 
        }
    )

    // Handle user stats error
    useEffect(() => {
        if (userStatsError && isOpen) {
            const errorMessage = userStatsError?.message || 'Unknown error occurred'
            if (errorMessage.includes('User not found')) {
                toast.error(t('nodeModal.onlineStats.userNotFound', { 
                    defaultValue: 'User not found or not online',
                    username: specificUsername 
                }))
            } else {
                toast.error(t('nodeModal.onlineStats.errorLoading', { 
                    defaultValue: 'Error loading user stats',
                    message: errorMessage 
                }))
            }
        }
    }, [userStatsError, isOpen, specificUsername, t])

    // Query for user IP list (when viewing IPs)
    const { 
        data: userIPs, 
        isLoading: isLoadingIPs,
        error: userIPsError,
        refetch: refetchIPs 
    } = useUserOnlineIpList(
        nodeId || 0, 
        viewingIPs || '', 
        { 
            query: { 
                enabled: !!(isOpen && nodeId && viewingIPs),
                refetchInterval: isOpen ? 5000 : false // Only refresh when modal is open
            } 
        }
    )

    // Handle user IPs error
    useEffect(() => {
        if (userIPsError && isOpen) {
            const errorMessage = userIPsError?.message || 'Unknown error occurred'
            if (errorMessage.includes('User not found')) {
                toast.error(t('nodeModal.onlineStats.userNotFound', { 
                    defaultValue: 'User not found or not online',
                    username: viewingIPs 
                }))
            } else {
                toast.error(t('nodeModal.onlineStats.errorLoadingIPs', { 
                    defaultValue: 'Error loading user IP addresses',
                    message: errorMessage 
                }))
            }
        }
    }, [userIPsError, isOpen, viewingIPs, t])

    const handleSearch = () => {
        if (!searchTerm.trim()) {
            toast.error(t('nodeModal.onlineStats.enterUsername', { defaultValue: 'Please enter a username' }))
            return
        }
        setSpecificUsername(searchTerm.trim())
    }

    const handleRefresh = async () => {
        setRefreshing(true)
        try {
            if (specificUsername) {
                await refetchUserStats()
            }
            if (viewingIPs) {
                await refetchIPs()
            }
            toast.success(t('nodeModal.onlineStats.refreshed', { defaultValue: 'Data refreshed successfully' }))
        } catch (error) {
            toast.error(t('nodeModal.onlineStats.refreshFailed', { defaultValue: 'Failed to refresh data' }))
        } finally {
            setRefreshing(false)
        }
    }

    const handleViewIPs = (username: string) => {
        setViewingIPs(username)
    }

    const handleBackToStats = () => {
        setViewingIPs(null)
    }

    const filteredStats = userStats && typeof userStats === 'object' ? userStats : {}
    
    const renderIPList = () => {
        if (!userIPs || typeof userIPs !== 'object') return null

        return (
            <div className="space-y-4">
                <div className="flex items-center justify-between">
                    <Button variant="ghost" onClick={handleBackToStats} className="text-sm">
                        ← {t('nodeModal.onlineStats.backToStats', { defaultValue: 'Back to Stats' })}
                    </Button>
                    <Badge variant="secondary">
                        {Object.keys(userIPs).length} {t('nodeModal.onlineStats.ipAddresses', { defaultValue: 'IP addresses' })}
                    </Badge>
                </div>
                
                <ScrollArea className="h-[400px]">
                    <div className="space-y-2">
                        {Object.entries(userIPs).map(([ip, protocols]) => (
                            <Card key={ip} className="p-3">
                                <div className="flex items-center justify-between">
                                    <div className="flex items-center gap-2">
                                        <Globe className="h-4 w-4 text-blue-500" />
                                        <span className="font-mono text-sm">{ip}</span>
                                    </div>
                                    <div className="flex gap-1">
                                        {Object.entries(protocols as { [key: string]: number }).map(([protocol, count]) => (
                                            <Badge key={protocol} variant="outline" className="text-xs">
                                                {protocol}: {count}
                                            </Badge>
                                        ))}
                                    </div>
                                </div>
                            </Card>
                        ))}
                    </div>
                </ScrollArea>
            </div>
        )
    }

    const renderUserStats = () => {
        if (isLoadingUserStats) {
            return (
                <div className="flex items-center justify-center h-32">
                    <Loader2 className="h-6 w-6 animate-spin" />
                    <span className="ml-2">{t('loading', { defaultValue: 'Loading...' })}</span>
                </div>
            )
        }

        if (userStatsError) {
            return (
                <div className="flex items-center justify-center h-32 text-muted-foreground">
                    <AlertCircle className="h-5 w-5 mr-2" />
                    {t('nodeModal.onlineStats.errorLoading', { defaultValue: 'Error loading user stats' })}
                </div>
            )
        }

        if (!filteredStats || Object.keys(filteredStats).length === 0) {
            return (
                <div className="flex items-center justify-center h-32 text-muted-foreground">
                    <Users className="h-5 w-5 mr-2" />
                    {specificUsername 
                        ? t('nodeModal.onlineStats.userNotOnline', { defaultValue: 'User is not online' })
                        : t('nodeModal.onlineStats.searchUser', { defaultValue: 'Search for a user to view their online stats' })
                    }
                </div>
            )
        }

        return (
            <div className="space-y-3">
                <UserStatsCard
                    username={specificUsername}
                    stats={filteredStats}
                    nodeId={nodeId || 0}
                    onViewIPs={handleViewIPs}
                />
            </div>
        )
    }

    return (
        <Dialog open={isOpen} onOpenChange={onOpenChange}>
            <DialogContent className="max-w-2xl h-[600px] flex flex-col">
                <DialogHeader>
                    <DialogTitle className={cn('text-xl font-semibold flex items-center gap-2', dir === 'rtl' && 'sm:text-right')}>
                        <Activity className="h-5 w-5" />
                        {viewingIPs 
                            ? t('nodeModal.onlineStats.ipListTitle', { 
                                defaultValue: 'IP Addresses for {{username}}', 
                                username: viewingIPs 
                              })
                            : t('nodeModal.onlineStats.title', { defaultValue: 'Online User Statistics' })
                        }
                    </DialogTitle>
                    {nodeName && (
                        <p className={cn('text-sm text-muted-foreground', dir === 'rtl' && 'sm:text-right')}>
                            {t('nodeModal.onlineStats.nodeInfo', { 
                                defaultValue: 'Node: {{nodeName}}', 
                                nodeName 
                            })}
                        </p>
                    )}
                </DialogHeader>

                {/* Search Bar - Only show when not viewing IPs */}
                {!viewingIPs && (
                    <div className="flex gap-2">
                        <div className="flex-1">
                            <Input
                                placeholder={t('nodeModal.onlineStats.searchPlaceholder', { 
                                    defaultValue: 'Enter username to search...' 
                                })}
                                value={searchTerm}
                                onChange={(e) => setSearchTerm(e.target.value)}
                                onKeyDown={(e) => e.key === 'Enter' && handleSearch()}
                                className="w-full"
                            />
                        </div>
                        <Button 
                            onClick={handleSearch}
                            disabled={!searchTerm.trim() || isLoadingUserStats}
                            className="shrink-0"
                        >
                            <Search className="h-4 w-4" />
                        </Button>
                        <Button 
                            variant="outline" 
                            onClick={handleRefresh}
                            disabled={refreshing || (!specificUsername && !viewingIPs)}
                            className="shrink-0"
                        >
                            <RefreshCw className={cn('h-4 w-4', refreshing && 'animate-spin')} />
                        </Button>
                    </div>
                )}

                <Separator />

                {/* Content Area */}
                <div className="flex-1 overflow-hidden">
                    {viewingIPs ? renderIPList() : renderUserStats()}
                </div>

                {/* Auto-refresh indicator */}
                {(specificUsername || viewingIPs) && (
                    <div className="text-xs text-muted-foreground text-center py-2 border-t">
                        <Activity className="h-3 w-3 inline mr-1" />
                        {t('nodeModal.onlineStats.autoRefresh', { defaultValue: 'Auto-refreshing every 5 seconds' })}
                    </div>
                )}
            </DialogContent>
        </Dialog>
    )
} 