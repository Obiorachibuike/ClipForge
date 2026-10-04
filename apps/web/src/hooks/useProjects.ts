import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { api } from '@/lib/api';
import { queryKeys } from '@/lib/query';
import { toast } from '@/stores/ui';
import type { Page, Project, ProjectSummary } from '@/types/api';

export function useProjects(params: { limit?: number; offset?: number; archived?: boolean } = {}) {
  return useQuery({
    queryKey: queryKeys.projects(params),
    queryFn: () => api.get<Page<ProjectSummary>>('/projects', params as Record<string, string | number>),
    staleTime: 10_000,
  });
}

export function useProject(projectId: string | undefined) {
  return useQuery({
    queryKey: queryKeys.project(projectId ?? ''),
    queryFn: () => api.get<ProjectSummary>(`/projects/${projectId}`),
    enabled: Boolean(projectId),
  });
}

export function useCreateProject() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (input: { name: string; description?: string; target_aspect_ratio?: string; privacy_mode?: string }) =>
      api.post<Project>('/projects', input),
    onSuccess: (project) => {
      queryClient.invalidateQueries({ queryKey: ['projects'] });
      toast({ kind: 'success', title: 'Project created', description: project.name });
    },
    onError: (error: unknown) => {
      toast({
        kind: 'error',
        title: 'Could not create the project',
        description: error instanceof Error ? error.message : undefined,
      });
    },
  });
}

export function useUpdateProject(projectId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (input: Partial<Pick<Project, 'name' | 'description' | 'target_aspect_ratio' | 'status'>>) =>
      api.patch<Project>(`/projects/${projectId}`, input),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: queryKeys.project(projectId) });
      queryClient.invalidateQueries({ queryKey: ['projects'] });
    },
    onError: (error: unknown) =>
      toast({
        kind: 'error',
        title: 'Could not save changes',
        description: error instanceof Error ? error.message : undefined,
      }),
  });
}

export function useDeleteProject() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (projectId: string) => api.delete<{ message: string }>(`/projects/${projectId}`),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['projects'] });
      toast({ kind: 'success', title: 'Project deleted' });
    },
    onError: (error: unknown) =>
      toast({
        kind: 'error',
        title: 'Could not delete the project',
        description: error instanceof Error ? error.message : undefined,
      }),
  });
}
