import { Link } from 'react-router-dom';
import { Compass } from 'lucide-react';

export default function NotFound() {
  return (
    <div className="flex min-h-screen flex-col items-center justify-center gap-5 px-6 text-center">
      <span className="flex h-14 w-14 items-center justify-center rounded-2xl bg-ink-800 text-slate-400">
        <Compass className="h-7 w-7" aria-hidden />
      </span>
      <div>
        <h1 className="text-2xl font-semibold text-white">That page does not exist</h1>
        <p className="mt-2 text-sm text-slate-400">
          The link may be out of date, or the project it pointed at was deleted.
        </p>
      </div>
      <div className="flex gap-2">
        <Link to="/dashboard" className="btn-primary">
          Go to dashboard
        </Link>
        <Link to="/" className="btn-secondary">
          Back to the homepage
        </Link>
      </div>
    </div>
  );
}
