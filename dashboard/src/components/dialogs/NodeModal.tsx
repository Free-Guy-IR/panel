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
      <DialogContent className="max-w-[1000px] h-full sm:h-auto">
        <DialogHeader>
          <DialogTitle className={cn("text-xl font-semibold", dir === "rtl" && "sm:text-right")}>
            {editingNode ? t('editNode.title') : t('nodeModal.title')}
          </DialogTitle>
          <p className={cn("text-sm text-muted-foreground", dir === "rtl" && "sm:text-right")}>
            {editingNode ? t('nodes.prompt') : t('nodeModal.description')}
          </p>
        </DialogHeader>
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

                <div className="grid grid-cols-2 gap-4">
                  <FormField
                    control={form.control}
                    name="usage_coefficient"
                    render={({ field }) => (
                      <FormItem>
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
                    name="connection_type"
                    render={({ field }) => (
                      <FormItem>
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
                </div>

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
                disabled={addNodeMutation.isPending || modifyNodeMutation.isPending}
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