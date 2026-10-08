import { flushSync } from "react-dom";

/** Whether this browser can morph one page into the next. Without it (or for
 *  anyone who asks for less motion) the change simply happens. */
export function canMorph(): boolean {
  const still = window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;
  return typeof document.startViewTransition === "function" && !still;
}

/** Runs `update` as a view transition: the browser photographs the page,
 *  React commits the change at once, and elements that share a
 *  `view-transition-name` on both sides (the search box) glide from the old
 *  place to the new while the rest crossfades. */
export function morph(update: () => void): void {
  if (!canMorph()) {
    update();
    return;
  }
  document.startViewTransition(() => flushSync(update));
}
