import { ORGANIZATION_URL, REPO_URL } from '@/constants/Project'
import { getSystemStats } from '@/service/api'
import { FC, HTMLAttributes, useEffect, useState } from 'react'

const FooterContent = () => {
  const [version, setVersion] = useState<string>('')

  useEffect(() => {
    const fetchVersion = async () => {
      try {
        const data = await getSystemStats()
        if (data?.version) {
          setVersion(` (v${data.version})`)
        }
      } catch (error) {
        console.error('Failed to fetch version:', error)
      }
    }
    fetchVersion()
  }, [])

  return (
    <p className="inline-block flex-grow text-center text-gray-500 text-xs">
      <a className="text-blue-400" href={REPO_URL}>
        Marzban
      </a>
      {version}, Made with ❤️ in{' '}
      <a className="text-blue-400" href={ORGANIZATION_URL}>
        Gozargah
      </a>
    </p>
  )
}

export const Footer: FC<HTMLAttributes<HTMLDivElement>> = (props) => {
  return (
    <div className="flex w-full pt-1 pb-3 relative" {...props}>
      <FooterContent />
    </div>
  )
}
