import PageHeader from '@/components/page-header'
import { Separator } from '@/components/ui/separator'
import { Plus } from 'lucide-react'
import { useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { getAdmins, modifyAdmin } from '@/service/api'
import type { Admin, AdminDetails } from '@/service/api'
import AdminsTable from '@/components/admins/AdminsTable'
import AdminModal from '@/components/dialogs/AdminModal'
import { useToast } from '@/hooks/use-toast'
import { zodResolver } from '@hookform/resolvers/zod'
import { useForm } from 'react-hook-form'
import { z } from 'zod'
import { useTranslation } from 'react-i18next'

const formSchema = z.object({
  username: z.string().min(1),
  password: z.string().min(1),
  is_sudo: z.boolean().default(false),
})

type FormValues = z.infer<typeof formSchema>

export default function AdminsPage() {
  const [isDialogOpen, setIsDialogOpen] = useState(false)
  const [editingAdmin, setEditingAdmin] = useState<Partial<Admin> | null>(null)
  const { toast } = useToast()
  const queryClient = useQueryClient()
  const { t } = useTranslation()

  const form = useForm<FormValues>({
    resolver: zodResolver(formSchema),
    defaultValues: {
      username: '',
      password: '',
      is_sudo: false,
    },
  })

  const { data: admins = [] } = useQuery<Admin[]>({
    queryKey: ['admins'],
    queryFn: () => getAdmins(),
  })

  const updateMutation = useMutation({
    mutationFn: ({ id, data }: { id: string; data: AdminDetails }) => modifyAdmin(id, data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['admins'] })
      setIsDialogOpen(false)
      setEditingAdmin(null)
      form.reset()
      toast({
        title: t('success'),
        description: t('admins.editSuccess'),
      })
    },
    onError: (error: Error) => {
      toast({
        title: t('error'),
        description: t('admins.editFailed'),
        variant: 'destructive',
      })
    },
  })

  const onSubmit = async (data: FormValues) => {
    if (editingAdmin?.id) {
      await updateMutation.mutateAsync({ id: editingAdmin.id, data })
    } else {
      toast({
        title: t('error'),
        description: t('admins.createFailed'),
        variant: 'destructive',
      })
    }
  }

  return (
    <div className="flex flex-col gap-2 w-full items-start">
      <PageHeader
        title="admins.title"
        description="admins.description"
        buttonIcon={Plus}
        buttonText="admins.createAdmin"
        onButtonClick={() => setIsDialogOpen(true)}
      />
      <Separator />
      <AdminsTable
        data={admins}
        onEdit={(admin) => {
          form.reset({
            username: admin.username,
            password: '',
            is_sudo: admin.is_sudo,
          })
          setEditingAdmin(admin)
          setIsDialogOpen(true)
        }}
        onDelete={() => {
          toast({
            title: t('error'),
            description: t('admins.deleteFailed'),
            variant: 'destructive',
          })
        }}
      />
      <AdminModal
        isDialogOpen={isDialogOpen}
        onOpenChange={setIsDialogOpen}
        onSubmit={onSubmit}
        editingAdmin={!!editingAdmin}
        form={form}
      />
    </div>
  )
} 