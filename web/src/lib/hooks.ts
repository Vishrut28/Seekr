import { useEffect, useState } from "react";

/** Marks the whole page as working, so the watermark animates and any caller
 *  can show its own message underneath. */
export function useWorking(active: boolean): void {
  useEffect(() => {
    document.body.classList.toggle("is-working", active);
    return () => document.body.classList.remove("is-working");
  }, [active]);
}

/** The topbar earns its shadow only once there is something scrolled under it. */
export function useScrollShade(): boolean {
  const [scrolled, setScrolled] = useState(false);
  useEffect(() => {
    const onScroll = () => setScrolled(window.scrollY > 4);
    window.addEventListener("scroll", onScroll, { passive: true });
    onScroll();
    return () => window.removeEventListener("scroll", onScroll);
  }, []);
  return scrolled;
}