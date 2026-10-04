import { create } from 'zustand';

export type ToastKind = 'success' | 'error' | 'info' | 'warning';

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
  pushToast: (toast: Omit<Toast, 'id'>) => string;
  dismissToast: (id: string) => void;
  setSidebar: (open: boolean) => void;
  setCommandOpen: (open: boolean) => void;
}

let counter = 0;

export const useUiStore = create<UiState>((set) => ({
  toasts: [],
  sidebarOpen: false,
  commandOpen: false,

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
