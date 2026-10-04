import { useState } from 'react';
import { Link, Navigate, useLocation, useNavigate, useSearchParams } from 'react-router-dom';
import { motion } from 'framer-motion';
import { ArrowRight, KeyRound, Mail, Sparkles, User } from 'lucide-react';
import { useAuthStore } from '@/stores/auth';
import { api } from '@/lib/api';
import { toast } from '@/stores/ui';

export default function AuthPage({ mode }: { mode: 'login' | 'register' }) {
  const navigate = useNavigate();
  const location = useLocation();
  const [params] = useSearchParams();
  const status = useAuthStore((state) => state.status);
  const login = useAuthStore((state) => state.login);
  const register = useAuthStore((state) => state.register);
  const error = useAuthStore((state) => state.error);
  const busy = useAuthStore((state) => state.busy);

  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [name, setName] = useState('');
  const [fieldErrors, setFieldErrors] = useState<Record<string, string>>({});
  const [resetSent, setResetSent] = useState(false);

  if (status === 'authenticated') {
    const next = params.get('next');
    return <Navigate to={next ? decodeURIComponent(next) : '/dashboard'} replace />;
  }

  const isRegister = mode === 'register';
  const destination = params.get('next') ? decodeURIComponent(params.get('next')!) : '/dashboard';

  async function onSubmit(event: React.FormEvent) {
    event.preventDefault();
    setFieldErrors({});
    try {
      if (isRegister) {
        await register({ email, password, name });
      } else {
        await login(email, password);
      }
      navigate(destination, { replace: true });
    } catch (caught) {
      // Field-level messages come from the API's validation details.
      const details = (caught as { details?: { errors?: Array<{ field: string; message: string }> } })?.details;
      const errors: Record<string, string> = {};
      details?.errors?.forEach((item) => {
        if (item.field) errors[item.field] = item.message;
      });
      setFieldErrors(errors);
    }
  }

  async function requestReset() {
    if (!email) {
      setFieldErrors({ email: 'Enter your email address first.' });
      return;
    }
    try {
      await api.post('/auth/password/reset-request', { email });
      setResetSent(true);
    } catch (caught) {
      toast({
        kind: 'error',
        title: 'Could not request a reset link',
        description: caught instanceof Error ? caught.message : undefined,
      });
    }
  }

  return (
    <div className="relative flex min-h-screen items-center justify-center overflow-hidden px-4 py-12">
      <div className="pointer-events-none absolute inset-0 bg-hero-glow" />
      <motion.div
        initial={{ opacity: 0, y: 14 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ duration: 0.4, ease: [0.22, 1, 0.36, 1] }}
        className="relative w-full max-w-md"
      >
        <Link to="/" className="mb-8 flex items-center justify-center gap-2.5">
          <span className="flex h-9 w-9 items-center justify-center rounded-xl bg-gradient-to-br from-accent-500 to-violet-500">
            <Sparkles className="h-4.5 w-4.5 text-white" aria-hidden />
          </span>
          <span className="text-lg font-semibold tracking-tight text-white">ClipForge</span>
        </Link>

        <div className="card p-7">
          <h1 className="text-xl font-semibold text-white">
            {isRegister ? 'Create your account' : 'Welcome back'}
          </h1>
          <p className="mt-1.5 text-sm text-slate-400">
            {isRegister
              ? 'Start with a short video and see the full pipeline run.'
              : 'Sign in to pick up where you left off.'}
          </p>

          <form className="mt-6 space-y-4" onSubmit={onSubmit} noValidate>
            {isRegister ? (
              <div>
                <label className="label" htmlFor="name">
                  Name
                </label>
                <div className="relative">
                  <User className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-slate-500" />
                  <input
                    id="name"
                    className="input pl-9"
                    value={name}
                    onChange={(event) => setName(event.target.value)}
                    placeholder="Ada Lovelace"
                    autoComplete="name"
                  />
                </div>
              </div>
            ) : null}

            <div>
              <label className="label" htmlFor="email">
                Email
              </label>
              <div className="relative">
                <Mail className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-slate-500" />
                <input
                  id="email"
                  type="email"
                  required
                  className="input pl-9"
                  value={email}
                  onChange={(event) => setEmail(event.target.value)}
                  placeholder="you@example.com"
                  autoComplete="email"
                />
              </div>
              {fieldErrors.email ? <p className="mt-1.5 text-xs text-red-300">{fieldErrors.email}</p> : null}
            </div>

            <div>
              <label className="label" htmlFor="password">
                Password
              </label>
              <div className="relative">
                <KeyRound className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-slate-500" />
                <input
                  id="password"
                  type="password"
                  required
                  minLength={8}
                  className="input pl-9"
                  value={password}
                  onChange={(event) => setPassword(event.target.value)}
                  placeholder={isRegister ? 'At least 8 characters' : '••••••••'}
                  autoComplete={isRegister ? 'new-password' : 'current-password'}
                />
              </div>
              {fieldErrors.password ? <p className="mt-1.5 text-xs text-red-300">{fieldErrors.password}</p> : null}
            </div>

            {error ? (
              <div className="rounded-xl border border-red-500/30 bg-red-500/10 px-3.5 py-2.5">
                <p className="text-xs text-red-200">{error}</p>
              </div>
            ) : null}

            <button type="submit" className="btn-primary w-full" disabled={busy}>
              {busy ? 'Working…' : isRegister ? 'Create account' : 'Sign in'}
              {!busy ? <ArrowRight className="h-4 w-4" aria-hidden /> : null}
            </button>
          </form>

          {!isRegister ? (
            <div className="mt-5 text-center">
              {resetSent ? (
                <p className="text-xs text-slate-400">
                  If that email has an account, a reset link is on its way. Check your inbox.
                </p>
              ) : (
                <button
                  type="button"
                  className="text-xs text-slate-400 underline-offset-2 hover:text-slate-200 hover:underline"
                  onClick={requestReset}
                >
                  Forgot your password?
                </button>
              )}
            </div>
          ) : null}
        </div>

        <p className="mt-6 text-center text-sm text-slate-500">
          {isRegister ? 'Already have an account?' : 'New to ClipForge?'}{' '}
          <Link
            to={isRegister ? '/login' : '/register'}
            state={location.state}
            className="font-medium text-accent-300 hover:text-accent-200"
          >
            {isRegister ? 'Sign in' : 'Create one'}
          </Link>
        </p>
      </motion.div>
    </div>
  );
}
