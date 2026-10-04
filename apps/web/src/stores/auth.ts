import { create } from 'zustand';
import { api, setCsrfToken, setUnauthorizedHandler } from '@/lib/api';

export interface AuthUser {
  id: string;
  email: string;
  name: string;
  avatar_url: string;
  plan: string;
  privacy_mode: string;
  default_aspect_ratio: string;
  default_caption_preset: string;
  is_admin: boolean;
  created_at: string;
  last_login_at: string | null;
}

export interface SessionPayload {
  user: AuthUser;
  csrf_token: string;
  expires_at: string;
}

interface AuthState {
  user: AuthUser | null;
  /** `unknown` until the session probe finishes: screens must not flash a login form. */
  status: 'unknown' | 'authenticated' | 'anonymous';
  sessionExpiresAt: string | null;
  error: string | null;
  busy: boolean;
  bootstrap: () => Promise<void>;
  login: (email: string, password: string) => Promise<void>;
  register: (input: { email: string; password: string; name?: string }) => Promise<void>;
  logout: () => Promise<void>;
  setUser: (user: AuthUser) => void;
  clear: () => void;
}

function applySession(session: SessionPayload) {
  setCsrfToken(session.csrf_token);
}

export const useAuthStore = create<AuthState>((set) => ({
  user: null,
  status: 'unknown',
  sessionExpiresAt: null,
  error: null,
  busy: false,

  bootstrap: async () => {
    try {
      const session = await api.get<SessionPayload>('/auth/session');
      applySession(session);
      set({ user: session.user, status: 'authenticated', sessionExpiresAt: session.expires_at });
    } catch {
      // 401 is the expected answer when nobody is signed in.
      setCsrfToken(null);
      set({ user: null, status: 'anonymous', sessionExpiresAt: null });
    }
  },

  login: async (email, password) => {
    set({ busy: true, error: null });
    try {
      const session = await api.post<SessionPayload>('/auth/login', { email, password });
      applySession(session);
      set({ user: session.user, status: 'authenticated', sessionExpiresAt: session.expires_at });
    } catch (error) {
      const message = error instanceof Error ? error.message : 'Could not sign in.';
      set({ error: message });
      throw error;
    } finally {
      set({ busy: false });
    }
  },

  register: async ({ email, password, name }) => {
    set({ busy: true, error: null });
    try {
      const session = await api.post<SessionPayload>('/auth/register', { email, password, name });
      applySession(session);
      set({ user: session.user, status: 'authenticated', sessionExpiresAt: session.expires_at });
    } catch (error) {
      const message = error instanceof Error ? error.message : 'Could not create the account.';
      set({ error: message });
      throw error;
    } finally {
      set({ busy: false });
    }
  },

  logout: async () => {
    try {
      await api.post('/auth/logout');
    } catch {
      // Signing out locally must work even if the server session already expired.
    }
    setCsrfToken(null);
    set({ user: null, status: 'anonymous', sessionExpiresAt: null });
  },

  setUser: (user) => set({ user }),
  clear: () => {
    setCsrfToken(null);
    set({ user: null, status: 'anonymous' });
  },
}));

// Any 401 from the API drops the local session so the router can react once.
setUnauthorizedHandler(() => {
  const { status, clear } = useAuthStore.getState();
  if (status === 'authenticated') clear();
});
