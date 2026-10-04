/** Component behaviour that users actually depend on. */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { Route, Routes } from 'react-router-dom';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import ConnectionBadge from '@/components/ConnectionBadge';
import RequireAuth from '@/components/RequireAuth';
import ToastHost from '@/components/ToastHost';
import { ProgressBar, StatusDot } from '@/components/ui';
import { useAuthStore } from '@/stores/auth';
import { toast, useUiStore } from '@/stores/ui';
import { renderWithProviders } from '@/test/utils';

beforeEach(() => {
  useAuthStore.setState({ status: 'unknown', user: null, error: null, busy: false });
  useUiStore.setState({ toasts: [], sidebarOpen: false, commandOpen: false });
});

afterEach(() => {
  vi.restoreAllMocks();
  useUiStore.setState({ toasts: [], sidebarOpen: false, commandOpen: false });
});

describe('RequireAuth', () => {
  it('renders the protected page for a signed-in user', () => {
    useAuthStore.setState({ status: 'authenticated' });
    renderWithProviders(
      <RequireAuth>
        <p>Editor</p>
      </RequireAuth>,
    );
    expect(screen.getByText('Editor')).toBeInTheDocument();
  });

  it('redirects an anonymous visitor to sign-in with the intended destination', () => {
    useAuthStore.setState({ status: 'anonymous' });
    renderWithProviders(
      <Routes>
        <Route
          path="/projects/p1/editor/c1"
          element={
            <RequireAuth>
              <p>Editor</p>
            </RequireAuth>
          }
        />
        <Route path="/login" element={<p>Sign in screen</p>} />
      </Routes>,
      { route: '/projects/p1/editor/c1' },
    );

    expect(screen.getByText('Sign in screen')).toBeInTheDocument();
    expect(screen.queryByText('Editor')).not.toBeInTheDocument();
  });

  it('waits (rather than redirecting) while the session is still unknown', () => {
    useAuthStore.setState({ status: 'unknown' });
    renderWithProviders(
      <Routes>
        <Route
          path="/dashboard"
          element={
            <RequireAuth>
              <p>Dashboard</p>
            </RequireAuth>
          }
        />
        <Route path="/login" element={<p>Sign in screen</p>} />
      </Routes>,
      { route: '/dashboard' },
    );

    // Redirecting a refreshing user to /login before the session resolves would
    // bounce them out of their own bookmarks.
    expect(screen.queryByText('Sign in screen')).not.toBeInTheDocument();
  });
});

describe('ConnectionBadge', () => {
  it('says "Live" only when the socket is genuinely open', () => {
    const { rerender } = render(<ConnectionBadge state="open" />);
    expect(screen.getByText('Live')).toBeInTheDocument();

    rerender(<ConnectionBadge state="reconnecting" />);
    expect(screen.getByText('Reconnecting')).toBeInTheDocument();
    expect(screen.queryByText('Live')).not.toBeInTheDocument();
  });

  it('explains that data still refreshes over the API while degraded', () => {
    render(<ConnectionBadge state="closed" />);
    const badge = screen.getByTitle(/still refreshed over the API/i);
    expect(badge).toHaveTextContent('Offline');
  });
});

describe('ToastHost', () => {
  it('renders nothing when there are no toasts', () => {
    render(<ToastHost />);
    expect(screen.queryByRole('status')).not.toBeInTheDocument();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });

  it('renders each toast with its message and can dismiss one', async () => {
    const user = userEvent.setup();
    toast({ kind: 'error', title: 'Render failed', description: 'FFmpeg exited with code 1.' });
    render(<ToastHost />);

    expect(screen.getByText('Render failed')).toBeInTheDocument();
    expect(screen.getByText('FFmpeg exited with code 1.')).toBeInTheDocument();

    const dismiss = screen.getAllByRole('button', { name: /dismiss/i })[0];
    await user.click(dismiss);

    // The exit animation may hold the node briefly, so wait for its removal.
    await waitFor(() => expect(screen.queryByText('Render failed')).not.toBeInTheDocument());
    expect(useUiStore.getState().toasts).toHaveLength(0);
  });

  it('announces errors to assistive technology', () => {
    toast({ kind: 'error', title: 'Upload failed' });
    render(<ToastHost />);
    expect(screen.getByRole('alert')).toHaveTextContent('Upload failed');
  });
});

describe('ProgressBar', () => {
  it('clamps the visible width and exposes the value', () => {
    const { container } = render(<ProgressBar value={150} />);
    const bar = container.querySelector('[style*="width"]') as HTMLElement;
    expect(bar.style.width).toBe('100%');

    const { container: lowContainer } = render(<ProgressBar value={-20} />);
    const lowBar = lowContainer.querySelector('[style*="width"]') as HTMLElement;
    expect(lowBar.style.width === '0%' || lowBar.style.width === '').toBe(true);
  });
});

describe('StatusDot', () => {
  it('renders without requiring a label', () => {
    const { container } = render(<StatusDot tone="danger" />);
    expect(container.querySelector('span')).toBeTruthy();
  });
});
