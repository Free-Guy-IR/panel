export interface AdminDetails {
  username: string
  password: string
  is_sudo: boolean
}

export interface Admin extends AdminDetails {
  id: string
  created_at: string
}

export const getAdmins = async (): Promise<Admin[]> => {
  const response = await api.get('/admins')
  return response.data
}

export const addAdmin = async (data: AdminDetails): Promise<Admin> => {
  const response = await api.post('/admins', data)
  return response.data
}

export const modifyAdmin = async (id: string, data: AdminDetails): Promise<Admin> => {
  const response = await api.put(`/admins/${id}`, data)
  return response.data
}

export const deleteAdmin = async (id: string): Promise<void> => {
  await api.delete(`/admins/${id}`)
} 