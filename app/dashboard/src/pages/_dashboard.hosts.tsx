
import PageHeader from '@/components/page-header'
import { Plus } from 'lucide-react'
import MainSection from '@/components/hosts/Hosts'
import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { getHosts } from '@/service/api'

const Hosts = () => {
    const [isDialogOpen, setIsDialogOpen] = useState<boolean>(false)
    const { data, error, isLoading } = useQuery({
        queryKey: ["getGetHostsQueryKey"],
        queryFn: () => getHosts(),
    });

    return (
        <div className="pb-8">
            <PageHeader
                title='hosts'
                description='manageHosts'
                buttonIcon={Plus}
                buttonText='hostsDialog.addHost'
                onButtonClick={() => setIsDialogOpen(true)}
            />
            <div>
                <MainSection data={data} isDialogOpen={isDialogOpen} onAddHost={(open) => setIsDialogOpen(open)} />
            </div>
        </div>
    )
}

export default Hosts
