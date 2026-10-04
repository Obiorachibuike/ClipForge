import { useCallback, useRef, useState } from 'react';
import { useQueryClient } from '@tanstack/react-query';
import { api } from '@/lib/api';
import { queryKeys } from '@/lib/query';
import { toast } from '@/stores/ui';

export type UploadState = 'idle' | 'uploading' | 'paused' | 'done' | 'failed' | 'cancelled';

interface UploadInitResponse {
  upload_id: string;
  project_id: string;
  video_id: string;
  chunk_size: number;
  total_chunks: number;
  received_chunks: number[];
  expires_at: string;
  storage_backend: string;
}

interface UploadStatusResponse {
  upload_id: string;
  status: string;
  received_chunks: number[];
  received_bytes: number;
  total_chunks: number;
  size_bytes: number;
  expires_at: string;
}

interface CompleteResponse {
  video: { id: string; original_filename: string };
  job: { id: string; type: string };
}

const MAX_RETRIES_PER_CHUNK = 4;

/**
 * Chunked, resumable upload.
 *
 * The file never enters React state or Zustand: only a `File` reference and
 * progress counters live here. Chunks are sliced from the File and PUT one at a
 * time, so memory stays flat regardless of video length, and a dropped
 * connection can resume from the chunks the server already has.
 */
export function useUpload(projectId: string) {
  const queryClient = useQueryClient();
  const [status, setStatus] = useState<UploadState>('idle');
  const [progress, setProgress] = useState(0);
  const [message, setMessage] = useState('');
  const [filename, setFilename] = useState('');

  const fileRef = useRef<File | null>(null);
  const sessionRef = useRef<UploadInitResponse | null>(null);
  const abortedRef = useRef(false);

  const reset = useCallback(() => {
    fileRef.current = null;
    sessionRef.current = null;
    abortedRef.current = false;
    setStatus('idle');
    setProgress(0);
    setMessage('');
    setFilename('');
  }, []);

  const uploadChunks = useCallback(
    async (session: UploadInitResponse, file: File, alreadyHave: Set<number>) => {
      const total = session.total_chunks;
      let completed = alreadyHave.size;

      for (let index = 0; index < total; index += 1) {
        if (abortedRef.current) return;
        if (alreadyHave.has(index)) continue;

        const start = index * session.chunk_size;
        const slice = file.slice(start, Math.min(start + session.chunk_size, file.size));

        let attempt = 0;
        for (;;) {
          try {
            await api.raw(`/uploads/${session.upload_id}/chunk/${index}`, {
              method: 'PUT',
              body: slice,
              headers: { 'Content-Type': 'application/octet-stream' },
            });
            break;
          } catch (error) {
            attempt += 1;
            if (attempt >= MAX_RETRIES_PER_CHUNK) throw error;
            // Network hiccups are expected on long uploads: back off and retry.
            await new Promise((resolve) => setTimeout(resolve, 400 * attempt));
          }
        }

        completed += 1;
        const pct = (completed / total) * 100;
        setProgress(pct);
        setMessage(`Uploading chunk ${completed} of ${total}`);
      }
    },
    [],
  );

  const finish = useCallback(
    async (session: UploadInitResponse) => {
      setMessage('Finalising upload');
      const result = await api.post<CompleteResponse>(`/uploads/${session.upload_id}/complete`);
      setProgress(100);
      setStatus('done');
      setMessage('Uploaded — inspecting media');
      queryClient.invalidateQueries({ queryKey: queryKeys.project(projectId) });
      queryClient.invalidateQueries({ queryKey: queryKeys.projectJobs(projectId) });
      queryClient.invalidateQueries({ queryKey: ['projects'] });
      toast({
        kind: 'success',
        title: 'Upload complete',
        description: `${result.video.original_filename} is being inspected.`,
      });
    },
    [projectId, queryClient],
  );

  const start = useCallback(
    async (file: File) => {
      reset();
      fileRef.current = file;
      abortedRef.current = false;
      setFilename(file.name);
      setStatus('uploading');
      setMessage('Preparing upload');

      try {
        const session = await api.post<UploadInitResponse>('/uploads/init', {
          filename: file.name,
          size_bytes: file.size,
          content_type: file.type || 'application/octet-stream',
          project_id: projectId,
        });
        sessionRef.current = session;
        await uploadChunks(session, file, new Set(session.received_chunks));
        if (abortedRef.current) return;
        await finish(session);
      } catch (error) {
        setStatus('failed');
        const detail = error instanceof Error ? error.message : 'Upload failed.';
        setMessage(detail);
        toast({ kind: 'error', title: 'Upload failed', description: detail });
      }
    },
    [projectId, reset, uploadChunks, finish],
  );

  /** Resume after a failure: the server tells us which chunks it already has. */
  const resume = useCallback(async () => {
    const file = fileRef.current;
    const session = sessionRef.current;
    if (!file || !session) return;
    abortedRef.current = false;
    setStatus('uploading');
    try {
      const state = await api.get<UploadStatusResponse>(`/uploads/${session.upload_id}`);
      await uploadChunks(session, file, new Set(state.received_chunks));
      if (abortedRef.current) return;
      await finish(session);
    } catch (error) {
      setStatus('failed');
      setMessage(error instanceof Error ? error.message : 'Could not resume the upload.');
    }
  }, [uploadChunks, finish]);

  const cancel = useCallback(async () => {
    const session = sessionRef.current;
    abortedRef.current = true;
    setStatus('cancelled');
    setMessage('Upload cancelled');
    if (session) {
      try {
        await api.delete(`/uploads/${session.upload_id}`);
      } catch {
        // Cancelling locally is enough; the session expires on its own.
      }
    }
  }, []);

  return { start, resume, cancel, reset, status, progress, message, filename };
}
