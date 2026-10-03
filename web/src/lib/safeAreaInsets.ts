// Shared Radix collision boundary: an invisible fixed element inset by the
// `--omnigent-safe-*` variables (index.css). An element, not a number, lets the
// CSS values resolve live on every position update.

import { getEmbedRoot } from "@/lib/host";

type CollisionBoundary = Element | null | (Element | null)[];

let safeAreaBoundary: HTMLElement | null = null;

/** The shared safe-area boundary element, kept inside the Radix portal container. */
export function getSafeAreaCollisionBoundary(): HTMLElement {
  // The embed registers its root after the first closed menus render, and its
  // scoped variables only resolve inside it, so follow the current container.
  const container = getEmbedRoot() ?? document.body;
  if (safeAreaBoundary?.isConnected && safeAreaBoundary.parentElement === container) {
    return safeAreaBoundary;
  }
  const el = safeAreaBoundary ?? document.createElement("div");
  el.style.position = "fixed";
  el.style.top = "var(--omnigent-safe-top, 0px)";
  el.style.right = "var(--omnigent-safe-right, 0px)";
  el.style.bottom = "var(--omnigent-safe-bottom, 0px)";
  el.style.left = "var(--omnigent-safe-left, 0px)";
  el.style.visibility = "hidden";
  el.style.pointerEvents = "none";
  el.setAttribute("aria-hidden", "true");
  container.appendChild(el);
  safeAreaBoundary = el;
  return el;
}

/**
 * Fold the safe-area boundary into a Radix `collisionBoundary` value. Radix
 * intersects the rects of every listed boundary (and the viewport), so the
 * caller's own boundaries keep applying.
 */
export function withSafeAreaCollisionBoundary(
  boundary: CollisionBoundary | undefined,
): (Element | null)[] {
  const list = boundary == null ? [] : Array.isArray(boundary) ? boundary : [boundary];
  return [...list, getSafeAreaCollisionBoundary()];
}
