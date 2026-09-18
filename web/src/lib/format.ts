/** Thousands separators, and a zero rather than a blank for a missing count. */
export const fmt = (n: number | null | undefined): string =>
  (n ?? 0).toLocaleString();

/** Dates arrive as ISO strings; the UI only ever shows the date part. */
export const day = (iso: string | null | undefined): string =>
  String(iso ?? "").slice(0, 10);

export const year = (iso: string | null | undefined): string =>
  String(iso ?? "").slice(0, 4);

export const minute = (iso: string | null | undefined): string =>
  String(iso ?? "").slice(0, 16).replace("T", " ");

/** A stored attribute key as a person would say it: research_field ->
 *  "Research field". The keys are the database's, not the reader's. */
export function attributeLabel(key: string): string {
  const text = (key || "").replace(/_/g, " ").trim();
  return text ? text[0].toUpperCase() + text.slice(1) : "";
}
