import UserTemplate from '../components/templates/user-template'
import { useGetUserTemplates, useModifyUserTemplate, UserTemplateResponse, ShadowsocksMethods, XTLSFlows } from '@/service/api'
import PageHeader from '@/components/page-header.tsx'
import { Plus } from 'lucide-react'
import { Separator } from '@/components/ui/separator.tsx'
import { Skeleton } from '@/components/ui/skeleton'
import { Card } from '@/components/ui/card'

import UserTemplateModal, { userTemplateFormSchema, UserTemplatesFromValue } from '@/components/dialogs/user-template-modal.tsx'
import { useState } from 'react'
import { useForm } from 'react-hook-form'
import { zodResolver } from '@hookform/resolvers/zod'
import { queryClient } from '@/utils/query-client.ts'
import { toast } from 'sonner'
import { useTranslation } from 'react-i18next'

const initialDefaultValues: Partial<UserTemplatesFromValue> = {
  name: '',
  status: 'active',
  username_prefix: '',
  username_suffix: '',
  data_limit: 0,
  expire_duration: 0,
  method: ShadowsocksMethods['chacha20-ietf-poly1305'],
  flow: XTLSFlows[''],
  on_hold_timeout: 0,
  groups: [],
  reset_usages: false,
}

export default function UserTemplates() {
  const [isDialogOpen, setIsDialogOpen] = useState(false)
  const [editingUserTemplate, setEditingUserTemplate] = useState<UserTemplateResponse | null>(null)
  const { data: userTemplates, isLoading } = useGetUserTemplates()
  const form = useForm<UserTemplatesFromValue>({
    resolver: zodResolver(userTemplateFormSchema),
  })
  const { t } = useTranslation()
  const modifyUserTemplateMutation = useModifyUserTemplate()
  const handleEdit = (userTemplate: UserTemplateResponse) => {
    setEditingUserTemplate(userTemplate)
    form.reset({
      name: userTemplate.name || undefined,
      status: userTemplate.status || undefined,
      data_limit: userTemplate.data_limit || undefined,
      expire_duration: userTemplate.expire_duration || undefined,
      method: userTemplate.extra_settings?.method || undefined,
      flow: userTemplate.extra_settings?.flow || undefined,
      groups: userTemplate.group_ids || undefined,
      username_prefix: userTemplate.username_prefix || undefined,
      username_suffix: userTemplate.username_suffix || undefined,
      on_hold_timeout: typeof userTemplate.on_hold_timeout === 'number' ? userTemplate.on_hold_timeout : undefined,
      data_limit_reset_strategy: userTemplate.data_limit_reset_strategy || undefined,
      reset_usages: userTemplate.reset_usages || false,
    })

    setIsDialogOpen(true)
  }

  const handleToggleStatus = async (template: UserTemplateResponse) => {
    try {
      await modifyUserTemplateMutation.mutateAsync({
        templateId: template.id,
        data: {
          name: template.name,
          data_limit: template.data_limit,
          expire_duration: template.expire_duration,
          username_prefix: template.username_prefix,
          username_suffix: template.username_suffix,
          group_ids: template.group_ids,
          status: template.status,
          reset_usages: template.reset_usages,
          is_disabled: !template.is_disabled,
          data_limit_reset_strategy: template.data_limit_reset_strategy,
          on_hold_timeout: template.on_hold_timeout,
          extra_settings: template.extra_settings,
        },
      })

      toast.success(t('success', { defaultValue: 'Success' }), {
        description: t(template.is_disabled ? 'templates.enableSuccess' : 'templates.disableSuccess', {
          name: template.name,
          defaultValue: `Template "{name}" has been ${template.is_disabled ? 'enabled' : 'disabled'} successfully`,
        }),
      })

      // Invalidate the groups query to refresh the list
      queryClient.invalidateQueries({
        queryKey: ['/api/user_templates'],
      })
    } catch (error) {
      toast.error(t('error', { defaultValue: 'Error' }), {
        description: t(template.is_disabled ? 'templates.enableFailed' : 'templates.disableFailed', {
          name: template.name,
          defaultValue: `Failed to ${template.is_disabled ? 'enable' : 'disable'} Template "{name}"`,
        }),
      })
    }
  }

  return (
    <div className="flex w-full flex-col items-start gap-2">
      <div className="w-full transform-gpu animate-fade-in" style={{ animationDuration: '400ms' }}>
        <PageHeader
          title="templates.title"
          description="templates.description"
          buttonIcon={Plus}
          buttonText="templates.addTemplate"
          onButtonClick={() => {
            setIsDialogOpen(true)
          }}
        />
        <Separator />
      </div>

      <div className="w-full flex-1 space-y-4 p-4 pt-6">
        <div
          className="mb-12 grid transform-gpu animate-slide-up grid-cols-1 gap-4 md:grid-cols-2 lg:grid-cols-3"
          style={{ animationDuration: '500ms', animationDelay: '100ms', animationFillMode: 'both' }}
        >
          {isLoading
            ? [...Array(6)].map((_, i) => (
                <Card key={i} className="px-4 py-5 sm:px-5 sm:py-6">
                  <div className="flex items-start justify-between gap-2 sm:gap-3">
                    <div className="min-w-0 flex-1">
                      <div className="flex items-center gap-x-2">
                        <Skeleton className="h-2 w-2 shrink-0 rounded-full" />
                        <Skeleton className="h-5 w-24 sm:w-32" />
                      </div>
                      <div className="mt-2 space-y-2">
                        <Skeleton className="h-4 w-32 sm:w-40 md:w-48" />
                        <Skeleton className="h-4 w-28 sm:w-36 md:w-40" />
                      </div>
                    </div>
                    <Skeleton className="h-8 w-8 shrink-0" />
                  </div>
                </Card>
              ))
            : userTemplates?.map((template: UserTemplateResponse) => <UserTemplate onEdit={handleEdit} template={template} key={template.id} onToggleStatus={handleToggleStatus} />)}
        </div>
      </div>

      <UserTemplateModal
        isDialogOpen={isDialogOpen}
        onOpenChange={open => {
          if (!open) {
            setEditingUserTemplate(null)
            form.reset(initialDefaultValues)
          }
          setIsDialogOpen(open)
        }}
        form={form}
        editingUserTemplate={!!editingUserTemplate}
        editingUserTemplateId={editingUserTemplate?.id}
      />
    </div>
  )
}
