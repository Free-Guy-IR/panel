export type ResetUsageTarget = {
  username: string
  isOwner: boolean
}

export const currentAdminUsername = (admin: unknown): string | undefined => {
  if (typeof admin !== 'object' || admin === null || !('username' in admin)) return undefined
  return typeof admin.username === 'string' ? admin.username : undefined
}

const isRefusedSelfReset = (target: ResetUsageTarget, currentAdminUsername: string | undefined) => !target.isOwner && target.username === currentAdminUsername

export const canResetAdminUsage = (hasHandler: boolean, target: ResetUsageTarget, currentAdminUsername: string | undefined) => hasHandler && !isRefusedSelfReset(target, currentAdminUsername)

export const resetEligibleAdmins = <T>(admins: T[], toTarget: (admin: T) => ResetUsageTarget, currentAdminUsername: string | undefined) =>
  admins.filter(admin => !isRefusedSelfReset(toTarget(admin), currentAdminUsername))
