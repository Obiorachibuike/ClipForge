import { describe, expect, it, vi } from 'vitest';
import { fireEvent, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import DiscoveryControls, {
  DEFAULT_DISCOVERY,
  clampDiscovery,
} from '@/pages/project/DiscoveryControls';
import { renderWithProviders } from '@/test/utils';

const onStart = () => vi.fn();

describe('DiscoveryControls', () => {
  it('starts from the server defaults and submits them', async () => {
    const start = onStart();
    renderWithProviders(<DiscoveryControls onStart={start} />);

    expect(screen.getByLabelText('Number of clips')).toHaveValue(DEFAULT_DISCOVERY.max_candidates);
    expect(screen.getByLabelText('Minimum clip duration')).toHaveValue(DEFAULT_DISCOVERY.min_duration);
    expect(screen.getByLabelText('Maximum clip duration')).toHaveValue(DEFAULT_DISCOVERY.max_duration);

    await userEvent.click(screen.getByRole('button', { name: /find clips/i }));
    expect(start).toHaveBeenCalledWith(DEFAULT_DISCOVERY);
  });

  it('refuses a reversed duration range instead of sending it', async () => {
    const start = onStart();
    renderWithProviders(<DiscoveryControls onStart={start} />);

    const minimum = screen.getByLabelText('Minimum clip duration');
    // jsdom does not implement selection/clearing for `type=number`, so drive
    // the same change event a browser emits when the value is replaced.
    fireEvent.change(minimum, { target: { value: '120' } });

    expect(screen.getByText(/minimum length must be shorter/i)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /find clips/i })).toBeDisabled();
    expect(start).not.toHaveBeenCalled();
  });

  it('reports the upper clip-count bound', async () => {
    renderWithProviders(<DiscoveryControls onStart={onStart()} />);
    const count = screen.getByLabelText('Number of clips');
    fireEvent.change(count, { target: { value: '100' } });
    expect(screen.getByText(/at most 30 clips/i)).toBeInTheDocument();
  });

  it('clamps values to the server bounds before calling onStart', () => {
    expect(clampDiscovery({ max_candidates: 999, min_duration: 1, max_duration: 900 })).toEqual({
      max_candidates: 30,
      min_duration: 3,
      max_duration: 300,
    });
    // The API caps the *minimum* clip length at 180s separately from the 300s
    // maximum length; the client must not submit a value the server rejects.
    expect(clampDiscovery({ max_candidates: 12, min_duration: 240, max_duration: 300 })).toEqual({
      max_candidates: 12,
      min_duration: 180,
      max_duration: 300,
    });
  });

  it('disables submission while a job is pending or the caller is not ready', () => {
    const { rerender } = renderWithProviders(<DiscoveryControls onStart={onStart()} pending />);
    expect(screen.getByRole('button', { name: /starting/i })).toBeDisabled();

    rerender(<DiscoveryControls onStart={onStart()} disabled />);
    expect(screen.getByRole('button', { name: /find clips/i })).toBeDisabled();
  });
});
