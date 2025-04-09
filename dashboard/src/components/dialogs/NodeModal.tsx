import { Dialog, DialogContent, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Form, FormControl, FormField, FormItem, FormLabel, FormMessage } from '@/components/ui/form'
import { Input } from '@/components/ui/input'
import { Button } from '@/components/ui/button'
import { useTranslation } from 'react-i18next'
import { UseFormReturn } from 'react-hook-form'
import { useAddNode, useModifyNode, NodeConnectionType } from '@/service/api'
import { toast } from '@/hooks/use-toast'
import { z } from 'zod'
import { cn } from '@/lib/utils'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { Textarea } from '@/components/ui/textarea'
import { queryClient } from '@/utils/query-client'
import useDirDetection from '@/hooks/use-dir-detection'
import { useState, useEffect } from 'react'
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert'
import { AlertTriangle, CheckCircle2, Loader2 } from 'lucide-react'

export const nodeFormSchema = z.object({
  name: z.string().min(1, 'Name is required'),
  address: z.string().min(1, 'Address is required'),
  port: z.number().min(1, 'Port is required'),
  usage_coefficient: z.number().optional(),
  connection_type: z.enum([NodeConnectionType.grpc, NodeConnectionType.rest]),
  server_ca: z.string().min(1, 'Server CA is required'),
  keep_alive: z.number().min(1, 'Keep alive is required'),
  max_logs: z.number().min(1, 'Max logs is required'),
})

export type NodeFormValues = z.infer<typeof nodeFormSchema>

interface NodeModalProps {
  isDialogOpen: boolean
  onOpenChange: (open: boolean) => void
  form: UseFormReturn<NodeFormValues>
  editingNode: boolean
  editingNodeId?: number
}

export default function NodeModal({ isDialogOpen, onOpenChange, form, editingNode, editingNodeId }: NodeModalProps) {
  const { t } = useTranslation()
  const dir = useDirDetection()
  const addNodeMutation = useAddNode()
  const modifyNodeMutation = useModifyNode()
  const [statusChecking, setStatusChecking] = useState(false)
  const [connectionStatus, setConnectionStatus] = useState<'idle' | 'success' | 'error'>('idle')
  const [errorDetails, setErrorDetails] = useState<string | null>(null)
  const [autoCheck, setAutoCheck] = useState(false)

  // Reset status when modal opens/closes
  useEffect(() => {
    if (isDialogOpen) {
      setConnectionStatus('idle')
      setErrorDetails(null)
      setAutoCheck(true)
    }
  }, [isDialogOpen])

  // Auto-check connection when form values change and are valid
  useEffect(() => {
    if (!isDialogOpen || !autoCheck) return

    const values = form.getValues()
    if (values.name && values.address && values.port) {
      checkNodeStatus()
    }
  }, [form.watch('name'), form.watch('address'), form.watch('port')])

  const checkNodeStatus = async () => {
    // Get current form values
    const values = form.getValues();

    // Validate required fields before checking
    if (!values.name || !values.address || !values.port) {
      return;
    }

    setStatusChecking(true);
    setConnectionStatus('idle');
    setErrorDetails(null);

    try {
      // In a real implementation, you would make an API call to check the node status
      // For now, we're simulating with a timeout
      await new Promise(resolve => setTimeout(resolve, 2000));

      // This is where you would make the actual API call to check the connection
      // const response = await fetch(`/api/nodes/check-connection`, {
      //   method: 'POST',
      //   headers: { 'Content-Type': 'application/json' },
      //   body: JSON.stringify(values)
      // });

      // Mock success (you'd check the actual response in a real implementation)
      const success = Math.random() > 0.3; // 70% chance of success for demo purposes

      if (success) {
        setConnectionStatus('success');
      } else {
        setConnectionStatus('error');
        setErrorDetails('Connection timed out. Make sure the node address and port are correct and the node is running.');
      }
    } catch (error: any) {
      setConnectionStatus('error');
      setErrorDetails(error?.message || 'Unknown error occurred');
    } finally {
      setStatusChecking(false);
    }
  };

  const onSubmit = async (values: NodeFormValues) => {
    try {
      if (editingNode && editingNodeId) {
        await modifyNodeMutation.mutateAsync({
          nodeId: editingNodeId,
          data: values
        })
        toast({
          title: t('success', { defaultValue: 'Success' }),
          description: t('nodes.editSuccess', {
            name: values.name,
            defaultValue: 'Node «{name}» has been updated successfully'
          })
        })
      } else {
        await addNodeMutation.mutateAsync({
          data: values
        })
        toast({
          title: t('success', { defaultValue: 'Success' }),
          description: t('nodes.createSuccess', {
            name: values.name,
            defaultValue: 'Node «{name}» has been created successfully'
          })
        })
      }
      // Invalidate nodes queries after successful operation
      queryClient.invalidateQueries({ queryKey: ['/api/nodes'] })
      onOpenChange(false)
      form.reset()
    } catch (error: any) {
      console.error('Node operation failed:', error)
      toast({
        title: t('error', { defaultValue: 'Error' }),
        description: t(editingNode ? 'nodes.editFailed' : 'nodes.createFailed', {
          name: values.name,
          error: error?.message || '',
          defaultValue: `Failed to ${editingNode ? 'update' : 'create'} node «{name}». {error}`
        }),
        variant: "destructive"
      })
    }
  }

  return (
    <Dialog open={isDialogOpen} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-[1000px] h-full sm:h-auto overflow-y-auto">
        <DialogHeader>
          <DialogTitle className={cn("text-xl font-semibold", dir === "rtl" && "sm:text-right")}>
            {editingNode ? t('editNode.title') : t('nodeModal.title')}
          </DialogTitle>
          <p className={cn("text-sm text-muted-foreground", dir === "rtl" && "sm:text-right")}>
            {editingNode ? t('nodes.prompt') : t('nodeModal.description')}
          </p>
        </DialogHeader>

        {/* Status Check Results - Positioned at the top of the modal */}
        {connectionStatus !== 'idle' && (
          <div className="mb-4">
            {connectionStatus === 'success' ? (
              <Alert variant="default" className="bg-green-50 dark:bg-green-900/20 text-green-800 dark:text-green-300 border-green-200 dark:border-green-800">
                <CheckCircle2 className="h-4 w-4" />
                <AlertTitle>{t('nodeModal.status')}</AlertTitle>
                <AlertDescription>
                  {t('nodeModal.statusCheckSuccess')}
                </AlertDescription>
              </Alert>
            ) : connectionStatus === 'error' ? (
              <Alert variant="destructive" className="bg-red-50 dark:bg-red-900/20 text-red-800 dark:text-red-300 border-red-200 dark:border-red-800">
                <AlertTriangle className="h-4 w-4" />
                <AlertTitle>{t('nodeModal.connectionError')}</AlertTitle>
                <AlertDescription>
                  {errorDetails && (
                    <div className="mt-2">
                      <p className="font-semibold">{t('nodeModal.errorDetails')}:</p>
                      <p className="text-sm">{errorDetails}</p>
                    </div>
                  )}
                  <Button
                    variant="outline"
                    size="sm"
                    onClick={() => {
                      setAutoCheck(true);
                      checkNodeStatus();
                    }}
                    className="mt-2"
                  >
                    {t('nodeModal.retryConnection')}
                  </Button>
                </AlertDescription>
              </Alert>
            ) : null}
          </div>
        )}

        <Form {...form}>
          <form onSubmit={form.handleSubmit(onSubmit)} className="flex flex-col">
            <div className="flex flex-col sm:flex-row items-start gap-4">
              <div className="flex-1 space-y-4 w-full">
                <FormField
                  control={form.control}
                  name="name"
                  render={({ field }) => (
                    <FormItem>
                      <FormLabel>{t('nodeModal.name')}</FormLabel>
                      <FormControl>
                        <Input placeholder={t('nodeModal.namePlaceholder')} {...field} />
                      </FormControl>
                      <FormMessage />
                    </FormItem>
                  )}
                />

                <div className="grid grid-cols-2 gap-4">
                  <FormField
                    control={form.control}
                    name="address"
                    render={({ field }) => (
                      <FormItem>
                        <FormLabel>{t('nodeModal.address')}</FormLabel>
                        <FormControl>
                          <Input placeholder={t('nodeModal.addressPlaceholder')} {...field} />
                        </FormControl>
                        <FormMessage />
                      </FormItem>
                    )}
                  />

                  <FormField
                    control={form.control}
                    name="port"
                    render={({ field }) => (
                      <FormItem>
                        <FormLabel>{t('nodeModal.port')}</FormLabel>
                        <FormControl>
                          <Input
                            type="number"
                            placeholder={t('nodeModal.portPlaceholder')}
                            {...field}
                            onChange={(e) => field.onChange(parseInt(e.target.value))}
                          />
                        </FormControl>
                        <FormMessage />
                      </FormItem>
                    )}
                  />
                </div>

                <div className="flex flex-col gap-4 w-full md">
                  <div className='flex items-center gap-4'>
                    <FormField
                      control={form.control}
                      name="usage_coefficient"
                      render={({ field }) => (
                        <FormItem className='flex-1'>
                          <FormLabel>{t('nodeModal.usageRatio')}</FormLabel>
                          <FormControl>
                            <Input
                              type="number"
                              step="0.1"
                              placeholder={t('nodeModal.usageRatioPlaceholder')}
                              {...field}
                              onChange={(e) => field.onChange(parseFloat(e.target.value))}
                            />
                          </FormControl>
                          <FormMessage />
                        </FormItem>
                      )}
                    />

                    <FormField
                      control={form.control}
                      name="max_logs"
                      render={({ field }) => (
                        <FormItem className='flex-1'>
                          <FormLabel>{t('nodes.maxLogs')}</FormLabel>
                          <FormControl>
                            <Input
                              type="number"
                              placeholder={t('nodes.maxLogsPlaceholder')}
                              {...field}
                              onChange={(e) => field.onChange(parseInt(e.target.value))}
                            />
                          </FormControl>
                          <FormMessage />
                        </FormItem>
                      )}
                    />
                  </div>

                  <div className='flex flex-col gap-4 w-full'>

                    <FormField
                      control={form.control}
                      name="connection_type"
                      render={({ field }) => (
                        <FormItem className='w-full'>
                          <FormLabel>{t('nodeModal.connectionType')}</FormLabel>
                          <Select
                            onValueChange={field.onChange}
                            defaultValue={field.value}
                          >
                            <FormControl>
                              <SelectTrigger>
                                <SelectValue placeholder="Rest" />
                              </SelectTrigger>
                            </FormControl>
                            <SelectContent>
                              <SelectItem value={NodeConnectionType.grpc}>gRPC</SelectItem>
                              <SelectItem value={NodeConnectionType.rest}>Rest</SelectItem>
                            </SelectContent>
                          </Select>
                          <FormMessage />
                        </FormItem>
                      )}
                    />

                    <FormField
                      control={form.control}
                      name="keep_alive"
                      render={({ field }) => (
                        <FormItem>
                          <FormLabel>{t('nodeModal.keepAlive')}</FormLabel>
                          <div className="flex flex-col gap-1.5">
                            <p className="text-xs text-muted-foreground">{t('nodeModal.keepAliveDescription')}</p>
                            <div className="flex gap-2">
                              <FormControl>
                                <Input
                                  type="number"
                                  {...field}
                                  onChange={(e) => field.onChange(parseInt(e.target.value))}
                                />
                              </FormControl>
                              <Select defaultValue="days">
                                <SelectTrigger className="w-[100px]">
                                  <SelectValue placeholder={t('nodeModal.days')} />
                                </SelectTrigger>
                                <SelectContent>
                                  <SelectItem value="days">{t('nodeModal.days')}</SelectItem>
                                  <SelectItem value="seconds">{t('nodeModal.seconds')}</SelectItem>
                                </SelectContent>
                              </Select>
                            </div>
                          </div>
                          <FormMessage />
                        </FormItem>
                      )}
                    />
                    {/* Connection Status Indicator */}
                    {statusChecking && (
                      <div className="flex items-center gap-2 text-sm text-muted-foreground">
                        <Loader2 className="h-4 w-4 animate-spin" />
                        <span>{t('nodeModal.statusChecking')}</span>
                      </div>
                    )}
                  </div>
                </div>


              </div>

              <FormField
                control={form.control}
                name="server_ca"
                render={({ field }) => (
                  <FormItem className="flex-1 w-full mb-6 md:mb-0 h-full">
                    <FormLabel>{t('nodeModal.certificate')}</FormLabel>
                    <FormControl>
                      <Textarea
                        dir='ltr'
                        placeholder={t('nodeModal.certificatePlaceholder')}
                        className="font-mono text-xs h-[200px] md:h-3/4"
                        {...field}
                      />
                    </FormControl>
                    <FormMessage />
                  </FormItem>
                )}
              />
            </div>
            <div className="flex justify-end gap-2">
              <Button variant="outline" onClick={() => onOpenChange(false)}>
                {t('cancel')}
              </Button>
              <Button
                type="submit"
                disabled={addNodeMutation.isPending || modifyNodeMutation.isPending || statusChecking}
                className="bg-primary hover:bg-primary/90"
              >
                {editingNode ? t('edit') : t('create')}
              </Button>
            </div>
          </form>
        </Form>
      </DialogContent>
    </Dialog>
  )
} 