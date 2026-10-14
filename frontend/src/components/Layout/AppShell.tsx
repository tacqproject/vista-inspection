import { useState } from 'react'
import { Outlet } from 'react-router-dom'
import { Menu } from 'lucide-react'
import Sidebar from './Sidebar'

export default function AppShell() {
  const [open, setOpen] = useState(false)
  return (
    <div className="flex h-screen overflow-hidden bg-[#f4f6f8]">
      <Sidebar open={open} onClose={() => setOpen(false)} />
      {/* Mobile menu button — bottom-left FAB so it never clashes with the page
          headers' back buttons / titles. Hidden once the sidebar is static. */}
      <button
        onClick={() => setOpen(true)}
        aria-label="Menu"
        className="lg:hidden fixed bottom-4 left-4 z-30 w-11 h-11 flex items-center justify-center rounded-full bg-slate-900 text-white shadow-lg active:scale-95"
      >
        <Menu size={20} />
      </button>
      <main className="flex-1 overflow-auto min-w-0">
        <Outlet />
      </main>
    </div>
  )
}
