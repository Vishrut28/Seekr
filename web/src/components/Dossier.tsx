import { useRef, useState } from "react";
import { apiBlob, apiText, errorMessage, isUnauthorized } from "../api/client";

/** The evidence dossier, viewable inline on the page and downloadable as a
 *  PDF — two separate actions, because they serve different moments: "let me
 *  read this now" versus "let me take this with me". The HTML version is
 *  fetched and rendered on click (not eagerly with the rest of the profile),
 *  since it does its own server-side evidence/rubric computation and most
 *  visits to a profile will not open it.
 *
 *  Rendered in an <iframe srcDoc=...> rather than injected into the page
 *  directly: the dossier ships its own complete <style> block tuned for a
 *  printable A4 report, and an iframe keeps that fully isolated from the
 *  app's own CSS in both directions — no bleed either way. */
export function Dossier({ personId }: { personId: string }) {
  const [open, setOpen] = useState(false);
  const [html, setHtml] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [pdfBusy, setPdfBusy] = useState(false);
  const [pdfError, setPdfError] = useState<string | null>(null);
  const frame = useRef<HTMLIFrameElement>(null);

  const view = async () => {
    if (open) {
      setOpen(false);
      return;
    }
    setOpen(true);
    if (html) return; // already fetched this visit — do not refetch on toggle
    setLoading(true);
    setError(null);
    try {
      setHtml(await apiText(`/v1/persons/${personId}/dossier`));
    } catch (e) {
      if (!isUnauthorized(e)) setError(errorMessage(e));
      setOpen(false);
    } finally {
      setLoading(false);
    }
  };

  const download = async () => {
    setPdfBusy(true);
    setPdfError(null);
    try {
      const blob = await apiBlob(`/v1/persons/${personId}/dossier.pdf`);
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = "dossier.pdf";
      a.click();
      setTimeout(() => URL.revokeObjectURL(url), 60_000);
    } catch (e) {
      if (!isUnauthorized(e)) setPdfError(errorMessage(e));
    } finally {
      setPdfBusy(false);
    }
  };

  // The dossier's own content decides its natural height; a fixed iframe
  // height would either clip a long dossier or leave dead space under a
  // short one. Reading contentWindow's own document is safe here because
  // srcDoc content is always same-origin.
  const onFrameLoad = () => {
    const doc = frame.current?.contentWindow?.document;
    if (doc && frame.current) {
      frame.current.style.height = `${doc.documentElement.scrollHeight + 24}px`;
    }
  };

  return (
    <div className="dossier-block">
      <div className="dossier-actions">
        <button className="btn" onClick={view}>
          {loading ? "Loading…" : open ? "Hide dossier" : "View dossier"}
        </button>
        <button className="btn" disabled={pdfBusy} onClick={download}>
          {pdfBusy ? "Building…" : "Download PDF"}
        </button>
      </div>
      {error && <p className="muted" style={{ color: "#b3261e" }}>{error}</p>}
      {pdfError && <p className="muted" style={{ color: "#b3261e" }}>{pdfError}</p>}
      {open && html && (
        <iframe
          ref={frame}
          title="Evidence dossier"
          srcDoc={html}
          onLoad={onFrameLoad}
          style={{ width: "100%", border: "1px solid #e2ddd6", borderRadius: 6, marginTop: 10 }}
        />
      )}
    </div>
  );
}
