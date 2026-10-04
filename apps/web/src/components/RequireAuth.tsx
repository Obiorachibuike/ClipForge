import { Navigate, useLocation } from 'react-router-dom';
import { useAuthStore } from '@/stores/auth';

/**
 * Route guard. Redirects to sign-in with the intended destination, so a
 * bookmarked editor URL survives the round trip.
 */
export default function RequireAuth({ children }: { children: React.ReactNode }) {
  const status = useAuthStore((state) => state.status);
  const location = useLocation();

  if (status === 'anonymous') {
    const next = encodeURIComponent(`${location.pathname}${location.search}`);
    return <Navigate to={`/login?next=${next}`} replace />;
  }
  return <>{children}</>;
}
