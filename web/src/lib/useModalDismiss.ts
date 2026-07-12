import { useEffect, useRef, type RefObject } from 'react';

const FOCUSABLE_SELECTOR = [
  'a[href]',
  'button:not([disabled])',
  'textarea:not([disabled])',
  'input:not([disabled])',
  'select:not([disabled])',
  '[tabindex]:not([tabindex="-1"])',
].join(',');

/**
 * Shared accessibility wiring for modal dialogs:
 * - closes on Escape (unless `locked`, e.g. an in-flight request)
 * - moves focus into the dialog on open and restores it to the previously
 *   focused element on close
 * - traps Tab / Shift+Tab within the dialog
 *
 * Pair this with `role="dialog"`, `aria-modal="true"` and an
 * `aria-label`/`aria-labelledby` on the dialog element.
 */
export function useModalDismiss(
  ref: RefObject<HTMLElement>,
  onClose: () => void,
  locked = false,
): void {
  const onCloseRef = useRef(onClose);
  onCloseRef.current = onClose;
  const lockedRef = useRef(locked);
  lockedRef.current = locked;

  useEffect(() => {
    const node = ref.current;
    const previouslyFocused = document.activeElement as HTMLElement | null;

    const focusFirst = () => {
      if (!node) return;
      const focusables = node.querySelectorAll<HTMLElement>(FOCUSABLE_SELECTOR);
      (focusables[0] ?? node).focus();
    };
    focusFirst();

    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        if (lockedRef.current) return;
        event.stopPropagation();
        onCloseRef.current();
        return;
      }
      if (event.key !== 'Tab' || !node) return;
      const focusables = Array.from(node.querySelectorAll<HTMLElement>(FOCUSABLE_SELECTOR));
      if (!focusables.length) {
        event.preventDefault();
        return;
      }
      const first = focusables[0];
      const last = focusables[focusables.length - 1];
      const active = document.activeElement;
      if (event.shiftKey && (active === first || !node.contains(active))) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && active === last) {
        event.preventDefault();
        first.focus();
      }
    };

    document.addEventListener('keydown', onKeyDown, true);
    return () => {
      document.removeEventListener('keydown', onKeyDown, true);
      previouslyFocused?.focus?.();
    };
    // `onClose` and `locked` are read fresh via refs, so the effect runs once per mount
    // (a new `onClose` identity from a parent re-render must not re-trigger focusFirst()).
  }, [ref]);
}
