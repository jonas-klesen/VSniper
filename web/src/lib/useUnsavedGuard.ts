import { useEffect } from 'react';
import { useBlocker } from 'react-router-dom';

const PROMPT = 'You have unsaved changes that will be lost. Leave anyway?';

// Warns before navigating away (in-app route changes) or closing/reloading the tab while
// `isDirty` is true. In-app navigation is intercepted with react-router's useBlocker (requires
// a data router); tab close/reload uses the native beforeunload prompt.
export function useUnsavedGuard(isDirty: boolean): void {
  const blocker = useBlocker(
    ({ currentLocation, nextLocation }) => isDirty && currentLocation.pathname !== nextLocation.pathname,
  );

  useEffect(() => {
    if (blocker.state !== 'blocked') return;
    if (window.confirm(PROMPT)) blocker.proceed();
    else blocker.reset();
  }, [blocker]);

  useEffect(() => {
    if (!isDirty) return;
    const handler = (event: BeforeUnloadEvent) => {
      event.preventDefault();
      event.returnValue = '';
    };
    window.addEventListener('beforeunload', handler);
    return () => window.removeEventListener('beforeunload', handler);
  }, [isDirty]);
}
