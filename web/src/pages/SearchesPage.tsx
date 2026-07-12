import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useCallback, useState } from 'react';

import { ErrorText } from '../components/ErrorText';
import {
  formFromSearch,
  payloadFromForm,
  SearchBuilderModal,
  SearchCard,
  type SearchFormState,
  withDefaultCategoryFilter,
} from '../components/searches/SearchBuilder';
import { api } from '../lib/api';
import { queryKeys } from '../lib/queryKeys';
import { applySizeFilter } from '../lib/searchFilters';
import type { SearchRecord, SearchUpdatePayload } from '../types';

type BuilderState = { searchId: string; form: SearchFormState };

export function SearchesPage() {
  const queryClient = useQueryClient();
  const searchesQuery = useQuery({ queryKey: queryKeys.searches, queryFn: api.getSearches, refetchInterval: 30_000 });
  const settingsQuery = useQuery({ queryKey: queryKeys.settings, queryFn: api.getSettings });
  const categoryOptionsQuery = useQuery({ queryKey: queryKeys.searchCategoryOptions, queryFn: api.getSearchCategoryOptions });
  const [builder, setBuilder] = useState<BuilderState | null>(null);
  const [builderError, setBuilderError] = useState<string | null>(null);

  const closeBuilder = () => {
    setBuilder(null);
    setBuilderError(null);
  };

  const openEditBuilder = (search: SearchRecord) => {
    setBuilder({ searchId: search.id, form: withDefaultCategoryFilter(formFromSearch(search), categoryOptionsQuery.data) });
    setBuilderError(null);
  };

  const invalidateSearchRunData = () => {
    queryClient.invalidateQueries({ queryKey: queryKeys.searches });
    queryClient.invalidateQueries({ queryKey: queryKeys.candidates });
    queryClient.invalidateQueries({ queryKey: queryKeys.stats });
    queryClient.invalidateQueries({ queryKey: queryKeys.costs });
  };

  const updateMutation = useMutation({
    mutationFn: ({ searchId, payload }: { searchId: string; payload: SearchUpdatePayload }) => api.updateSearch(searchId, payload),
    onSuccess: () => {
      closeBuilder();
      queryClient.invalidateQueries({ queryKey: queryKeys.searches });
    },
    onError: (error) => {
      if (builder) setBuilderError(error.message);
    },
  });

  const toggleMutation = useMutation({
    mutationFn: api.toggleSearch,
    onSuccess: () => queryClient.invalidateQueries({ queryKey: queryKeys.searches }),
  });

  const liveMutation = useMutation({
    mutationFn: api.runSearch,
    onSuccess: invalidateSearchRunData,
  });

  const runAllMutation = useMutation({
    mutationFn: api.runAllSearches,
    onSuccess: invalidateSearchRunData,
  });

  const syncSizesMutation = useMutation({ mutationFn: api.syncVintedSizes });

  const applySizesMutation = useMutation({
    mutationFn: api.applyProfileSizesToAll,
    onSuccess: () => queryClient.invalidateQueries({ queryKey: queryKeys.searches }),
  });

  const updateBuilderForm = (form: SearchFormState) => {
    setBuilder((current) => (current ? { ...current, form } : current));
  };

  const handleSyncSizes = useCallback(() => {
    if (!builder) return;
    syncSizesMutation.mutate(undefined, {
      onSuccess: (result) => {
        setBuilder((current) => (
          current ? { ...current, form: { ...current.form, filters: applySizeFilter(current.form.filters, result.sizes) } } : current
        ));
      },
    });
  }, [builder, syncSizesMutation]);

  const submitBuilder = () => {
    if (!builder) return;
    try {
      setBuilderError(null);
      const payload = payloadFromForm(builder.form);
      updateMutation.mutate({ searchId: builder.searchId, payload: { ...payload, enabled: builder.form.enabled } });
    } catch (error) {
      setBuilderError(error instanceof Error ? error.message : 'Search could not be saved.');
    }
  };

  if (searchesQuery.isLoading) return <p>Loading searches…</p>;
  if (searchesQuery.isError || !searchesQuery.data) return <ErrorText error={searchesQuery.error ?? 'Could not load searches.'} />;
  const globalAlertThreshold = settingsQuery.data?.alert_threshold ?? 9;

  return (
    <section>
      <div className="page-header searches-page-header">
        <div>
          <p className="eyebrow">Search control</p>
          <h2>Saved searches</h2>
          <p className="muted">One editable Vinted radar per clothing bucket. Tune the existing category searches; no duplicates, no mystery closet clones.</p>
        </div>
        <button
          className="secondary"
          onClick={() => applySizesMutation.mutate()}
          disabled={applySizesMutation.isPending}
        >
          {applySizesMutation.isPending ? 'Syncing sizes…' : 'Sync sizes'}
        </button>
        <button onClick={() => runAllMutation.mutate()} disabled={runAllMutation.isPending}>
          {runAllMutation.isPending ? 'Running all…' : 'Run all'}
        </button>
      </div>

      <ErrorText error={categoryOptionsQuery.error} prefix="Category metadata failed to load" />
      <ErrorText error={runAllMutation.error} prefix="Run all failed" />
      <ErrorText error={applySizesMutation.error} prefix="Size sync failed" />

      {searchesQuery.data.length === 0 ? (
        <article className="card empty-searches-card">
          <p className="eyebrow">No canonical searches found</p>
          <h3>Restart the backend to seed category searches</h3>
          <p className="muted">The API should create exactly one search per clothing bucket on startup.</p>
        </article>
      ) : (
        <div className="stack">
          {searchesQuery.data.map((search) => {
            const runResult = liveMutation.data?.search_id === search.id
              ? liveMutation.data
              : runAllMutation.data?.find((r) => r.search_id === search.id);
            return (
              <SearchCard
                key={search.id}
                search={search}
                globalAlertThreshold={globalAlertThreshold}
                onEdit={() => openEditBuilder(search)}
                onToggle={() => toggleMutation.mutate(search.id)}
                onLiveRun={() => liveMutation.mutate(search.id)}
                onQuickSave={(payload) => updateMutation.mutate({ searchId: search.id, payload })}
                runResult={runResult}
                errors={{
                  toggle: toggleMutation.variables === search.id ? toggleMutation.error : undefined,
                  live: liveMutation.variables === search.id ? liveMutation.error : undefined,
                  quick: updateMutation.variables?.searchId === search.id && !builder ? updateMutation.error : undefined,
                }}
                isBusy={{
                  live: liveMutation.isPending && liveMutation.variables === search.id,
                  quick: updateMutation.isPending && updateMutation.variables?.searchId === search.id && !builder,
                }}
              />
            );
          })}
        </div>
      )}

      {builder ? (
        <SearchBuilderModal
          form={builder.form}
          categoryOptions={categoryOptionsQuery.data}
          onChange={updateBuilderForm}
          onClose={closeBuilder}
          onSubmit={submitBuilder}
          isPending={updateMutation.isPending}
          error={builderError}
          onSyncSizes={handleSyncSizes}
          isSyncingSizes={syncSizesMutation.isPending}
          syncSizesError={syncSizesMutation.error}
        />
      ) : null}
    </section>
  );
}
