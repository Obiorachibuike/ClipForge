import { Moon, Sun } from 'lucide-react';
import { useUiStore } from '@/stores/ui';
import { cn } from '@/lib/format';

/** Accessible theme switch shared by public pages and the signed-in shell. */
export default function ThemeToggle({ className }: { className?: string }) {
  const theme = useUiStore((state) => state.theme);
  const toggleTheme = useUiStore((state) => state.toggleTheme);
  const next = theme === 'dark' ? 'light' : 'dark';

  return (
    <button
      type="button"
      className={cn('btn-icon border border-ink-700 bg-ink-900/60 shadow-sm', className)}
      onClick={toggleTheme}
      aria-label={`Use ${next} mode`}
      title={`Use ${next} mode`}
    >
      {theme === 'dark' ? <Sun className="h-4 w-4" aria-hidden /> : <Moon className="h-4 w-4" aria-hidden />}
    </button>
  );
}
