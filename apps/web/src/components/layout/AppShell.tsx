import { useEffect } from 'react';
import { NavLink, Outlet, useLocation, useNavigate, useParams } from 'react-router-dom';
import {
  CreditCard,
  FolderOpen,
  LayoutDashboard,
  LogOut,
  Menu,
  Scissors,
  Settings,
  Sparkles,
} from 'lucide-react';
import { AnimatePresence, motion } from 'framer-motion';
import { cn } from '@/lib/format';
import { useLive } from '@/lib/live';
import { useAuthStore } from '@/stores/auth';
import { useUiStore } from '@/stores/ui';
import ConnectionBadge from '@/components/ConnectionBadge';

interface NavItem {
  to: string;
  label: string;
  icon: typeof LayoutDashboard;
  end?: boolean;
}

function Navigation({ projectId, onNavigate }: { projectId?: string; onNavigate?: () => void }) {
  const items: NavItem[] = [
    { to: '/dashboard', label: 'Dashboard', icon: LayoutDashboard },
    { to: '/settings', label: 'Settings', icon: Settings },
  ];

  if (projectId) {
    items.splice(
      1,
      0,
      { to: `/projects/${projectId}`, label: 'Project', icon: FolderOpen, end: true },
      { to: `/projects/${projectId}/clips`, label: 'Clips', icon: Scissors },
      { to: `/projects/${projectId}/export`, label: 'Exports', icon: CreditCard },
    );
  }

  return (
    <nav className="flex flex-1 flex-col gap-1">
      {items.map((item) => (
        <NavLink
          key={item.to}
          to={item.to}
          end={item.end}
          onClick={onNavigate}
          className={({ isActive }) =>
            cn(
              'flex items-center gap-3 rounded-xl px-3 py-2.5 text-sm font-medium transition-colors',
              isActive
                ? 'bg-accent-500/15 text-white shadow-[inset_0_0_0_1px_rgba(91,108,255,0.25)]'
                : 'text-slate-400 hover:bg-ink-800 hover:text-slate-100',
            )
          }
        >
          <item.icon className="h-4 w-4" aria-hidden />
          {item.label}
        </NavLink>
      ))}
    </nav>
  );
}

function SidebarContent({ onNavigate }: { onNavigate?: () => void }) {
  const { projectId } = useParams();
  const user = useAuthStore((state) => state.user);
  const logout = useAuthStore((state) => state.logout);
  const navigate = useNavigate();

  const initials = (user?.name || user?.email || '?')
    .split(/[\s@.]+/)
    .filter(Boolean)
    .slice(0, 2)
    .map((part) => part[0]?.toUpperCase())
    .join('');

  return (
    <div className="flex h-full flex-col gap-6 border-r border-ink-800 bg-ink-900/70 px-4 py-5">
      <NavLink to="/dashboard" onClick={onNavigate} className="flex items-center gap-2.5 px-1">
        <span className="flex h-8 w-8 items-center justify-center rounded-lg bg-gradient-to-br from-accent-500 to-violet-500">
          <Sparkles className="h-4 w-4 text-white" aria-hidden />
        </span>
        <span className="text-base font-semibold tracking-tight text-white">ClipForge</span>
      </NavLink>

      <Navigation projectId={projectId} onNavigate={onNavigate} />

      <div className="space-y-3">
        <div className="divider" />
        <div className="flex items-center gap-3 px-1">
          <span className="flex h-8 w-8 items-center justify-center rounded-full bg-ink-700 text-xs font-semibold text-slate-200">
            {initials || '?'}
          </span>
          <div className="min-w-0 flex-1">
            <p className="truncate text-sm font-medium text-slate-200">{user?.name || 'Creator'}</p>
            <p className="truncate text-2xs uppercase tracking-wide text-slate-500">{user?.plan || 'free'} plan</p>
          </div>
        </div>
        <button
          type="button"
          className="btn-ghost btn-sm w-full justify-start"
          onClick={async () => {
            await logout();
            navigate('/');
          }}
        >
          <LogOut className="h-4 w-4" aria-hidden />
          Sign out
        </button>
      </div>
    </div>
  );
}

export default function AppShell() {
  const { connection, resync } = useLive();
  const sidebarOpen = useUiStore((state) => state.sidebarOpen);
  const setSidebar = useUiStore((state) => state.setSidebar);
  const location = useLocation();

  useEffect(() => {
    setSidebar(false);
  }, [location.pathname, setSidebar]);

  // Recovery after a refresh: ask the server for current job state once mounted.
  useEffect(() => {
    resync();
  }, [resync]);

  return (
    <div className="flex min-h-screen bg-ink-950">
      <aside className="hidden w-64 shrink-0 lg:block">
        <div className="fixed inset-y-0 left-0 w-64">
          <SidebarContent />
        </div>
      </aside>

      <AnimatePresence>
        {sidebarOpen ? (
          <motion.div
            className="fixed inset-0 z-50 lg:hidden"
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
          >
            <div className="absolute inset-0 bg-black/70" onClick={() => setSidebar(false)} />
            <motion.div
              className="absolute inset-y-0 left-0 w-72"
              initial={{ x: -300 }}
              animate={{ x: 0 }}
              exit={{ x: -300 }}
              transition={{ type: 'spring', stiffness: 320, damping: 32 }}
            >
              <SidebarContent onNavigate={() => setSidebar(false)} />
            </motion.div>
          </motion.div>
        ) : null}
      </AnimatePresence>

      <div className="flex min-w-0 flex-1 flex-col">
        <header className="sticky top-0 z-40 flex h-14 items-center gap-3 border-b border-ink-800 bg-ink-950/85 px-4 backdrop-blur-md lg:px-6">
          <button
            type="button"
            className="btn-icon lg:hidden"
            onClick={() => setSidebar(true)}
            aria-label="Open navigation"
          >
            <Menu className="h-5 w-5" />
          </button>
          <div className="flex-1" />
          <ConnectionBadge state={connection} />
        </header>
        <main className="min-w-0 flex-1 px-4 py-6 lg:px-8 lg:py-8">
          <Outlet />
        </main>
      </div>
    </div>
  );
}
