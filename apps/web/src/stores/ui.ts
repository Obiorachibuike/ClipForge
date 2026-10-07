import { create } from 'zustand';

export type Theme = 'light' | 'dark';
export type ToastKind = 'success' | 'error' | 'info' | 'warning';

const THEME_KEY = 'clipforge_theme';

function initialTheme(): Theme {
  if (typeof window === 'undefined') return 'dark';
  try {
    const saved = window.localStorage.getItem(THEME_KEY);
    if (saved === 'light' || saved === 'dark') return saved;
  } catch {
    // Storage may be unavailable in hardened/private browser contexts.
  }
  return window.matchMedia?.('(prefers-color-scheme: light)').matches ? 'light' : 'dark';
}

export function applyTheme(theme: Theme): void {
  if (typeof document === 'undefined') return;
  document.documentElement.classList.toggle('dark', theme === 'dark');
  document.documentElement.classList.toggle('light', theme === 'light');
  document.documentElement.style.colorScheme = theme;
  document.querySelector('meta[name="theme-color"]')?.setAttribute('content', theme === 'dark' ? '#06060A' : '#F7F8FC');
}

export interface Toast {
  id: string;
  kind: ToastKind;
  title: string;
  description?: string;
  /** Milliseconds; `0` keeps the toast until it is dismissed. */
  duration?: number;
  action?: { label: string; run: () => void };
}

interface UiState {
  toasts: Toast[];
  sidebarOpen: boolean;
  commandOpen: boolean;
  theme: Theme;
  setTheme: (theme: Theme) => void;
  toggleTheme: () => void;
  pushToast: (toast: Omit<Toast, 'id'>) => string;
  dismissToast: (id: string) => void;
  setSidebar: (open: boolean) => void;
  setCommandOpen: (open: boolean) => void;
}

let counter = 0;

export const useUiStore = create<UiState>((set, get) => ({
  toasts: [],
  sidebarOpen: false,
  commandOpen: false,
  theme: initialTheme(),

  setTheme: (theme) => {
    try {
      window.localStorage.setItem(THEME_KEY, theme);
    } catch {
      // The in-memory preference still works when persistence is unavailable.
    }
    applyTheme(theme);
    set({ theme });
  },
  toggleTheme: () => get().setTheme(get().theme === 'dark' ? 'light' : 'dark'),

  pushToast: (toast) => {
    const id = `toast-${++counter}`;
    const duration = toast.duration ?? (toast.kind === 'error' ? 7000 : 4000);
    set((state) => ({ toasts: [...state.toasts, { ...toast, id, duration }] }));
    if (duration > 0) {
      window.setTimeout(() => {
        set((state) => ({ toasts: state.toasts.filter((item) => item.id !== id) }));
      }, duration);
    }
    return id;
  },

  dismissToast: (id) => set((state) => ({ toasts: state.toasts.filter((item) => item.id !== id) })),
  setSidebar: (open) => set({ sidebarOpen: open }),
  setCommandOpen: (open) => set({ commandOpen: open }),
}));

/** Convenience helper for non-React call sites (query callbacks, ws client). */
export function toast(input: Omit<Toast, 'id'>): string {
  return useUiStore.getState().pushToast(input);
}

export function errorToast(error: unknown, fallback = 'Something went wrong.'): void {
  const message = error instanceof Error ? error.message : fallback;
  toast({ kind: 'error', title: message });
}
