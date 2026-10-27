"use client";

import { useCallback, useEffect, useRef, useState } from "react";

export interface ApiState<T> {
  data: T | undefined;
  error: unknown;
  loading: boolean;
  /** Load again (keeps the current data while loading). */
  reload: () => void;
}

interface Settled<T> {
  request: string;
  data?: T;
  error: unknown;
}

/**
 * Run an API call when the component mounts and whenever `key` changes. Earlier requests
 * are aborted, so a slow old response never overwrites a newer one.
 */
export function useApi<T>(load: (signal: AbortSignal) => Promise<T>, key: string): ApiState<T> {
  const [version, setVersion] = useState(0);
  const [settled, setSettled] = useState<Settled<T>>({ request: "", error: null });
  const loadRef = useRef(load);
  useEffect(() => {
    loadRef.current = load;
  });
  const request = `${key}#${version}`;

  useEffect(() => {
    const controller = new AbortController();
    loadRef
      .current(controller.signal)
      .then((data) => {
        if (!controller.signal.aborted) setSettled({ request, data, error: null });
      })
      .catch((error: unknown) => {
        // Keep the previous data on a failed reload; the caller shows the error.
        if (!controller.signal.aborted) setSettled((prev) => ({ request, data: prev.data, error }));
      });
    return () => controller.abort();
  }, [request]);

  const reload = useCallback(() => setVersion((v) => v + 1), []);
  const current = settled.request === request;
  return { data: settled.data, error: current ? settled.error : null, loading: !current, reload };
}
