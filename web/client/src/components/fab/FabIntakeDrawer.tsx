import { useEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { AlertCircle, CheckCircle2, FileUp, Loader2, RotateCcw, Trash2, UploadCloud, X } from "lucide-react";
import { useFabLocale } from "./fabLocale";
import { asRecord, text, type FabRecord } from "./fabView";
import { appendIntakeItems, completeIntakeItem, finishIntakeBatch, MAX_INTAKE_FILES_PER_BATCH, tryBeginIntakeBatch, type IntakeQueueItem } from "./fabIntakeQueue";

const MAX_UPLOAD_BYTES = 6 * 1024 * 1024;
const ACCEPTED_EXTENSIONS = ["pdf", "jpg", "jpeg", "png", "heic", "tif", "tiff", "txt", "csv"];

type IntakeItem = IntakeQueueItem<File, FabRecord>;

type FabIntakeDrawerProps = {
  open: boolean;
  connected: boolean;
  onClose: () => void;
  onUploadFile: (file: File) => Promise<FabRecord>;
  onFinished: (uploadedCount: number) => Promise<void> | void;
  onBusyChange: (busy: boolean) => void;
};

export function FabIntakeDrawer({ open, connected, onClose, onUploadFile, onFinished, onBusyChange }: FabIntakeDrawerProps) {
  const { copy } = useFabLocale();
  const [items, setItems] = useState<IntakeItem[]>([]);
  const [selectionNotice, setSelectionNotice] = useState("");
  const [completionNotice, setCompletionNotice] = useState("");
  const [dragging, setDragging] = useState(false);
  const [busy, setBusy] = useState(false);
  const closeRef = useRef<HTMLButtonElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const restoreFocusRef = useRef<HTMLElement | null>(null);
  const busyRef = useRef(false);
  const queued = useMemo(() => items.filter((item) => item.status === "queued" || item.status === "error"), [items]);

  useEffect(() => {
    if (!open) return;
    restoreFocusRef.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    document.body.classList.add("fab-dialog-open");
    closeRef.current?.focus();
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape" && !busyRef.current) onClose();
      if (event.key !== "Tab") return;
      const dialog = closeRef.current?.closest<HTMLElement>("[role=dialog]");
      const focusable = dialog ? Array.from(dialog.querySelectorAll<HTMLElement>("button:not(:disabled), input:not(:disabled)")) : [];
      if (!focusable.length) return;
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
      if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
    };
    window.addEventListener("keydown", onKeyDown);
    return () => {
      window.removeEventListener("keydown", onKeyDown);
      document.body.classList.remove("fab-dialog-open");
      restoreFocusRef.current?.focus();
    };
  }, [onClose, open]);

  if (!open) return null;

  function addFiles(files: ArrayLike<File>) {
    const result = appendIntakeItems(items, files, (file) => {
      const extension = file.name.split(".").pop()?.toLowerCase() || "";
      const error = file.size > MAX_UPLOAD_BYTES
        ? copy("Exceeds the 6 MB local upload limit.", "Overschrijdt de lokale uploadlimiet van 6 MB.")
        : !ACCEPTED_EXTENSIONS.includes(extension)
          ? copy("Unsupported file type.", "Niet-ondersteund bestandstype.")
          : undefined;
      return {
        id: createFileItemId(),
        file,
        fileName: file.name,
        fileSize: file.size,
        status: error ? "error" as const : "queued" as const,
        error,
      };
    });
    setItems(result.items);
    setSelectionNotice(result.rejectedCount
      ? copy(
        `The ${MAX_INTAKE_FILES_PER_BATCH}-file batch limit was reached. ${result.rejectedCount} additional file(s) were not added. Clear this batch to start another.`,
        `De limiet van ${MAX_INTAKE_FILES_PER_BATCH} bestanden per batch is bereikt. ${result.rejectedCount} extra bestand(en) zijn niet toegevoegd. Wis deze batch om een nieuwe te starten.`,
      )
      : "");
  }

  async function uploadSelected() {
    if (!tryBeginIntakeBatch(busyRef, connected, busy, queued.length)) return;
    setCompletionNotice("");
    setBusy(true);
    onBusyChange(true);
    let uploadedCount = 0;
    for (const item of queued) {
      const file = item.file;
      if (!file || !isUploadable(file)) continue;
      setItems((current) => current.map((entry) => entry.id === item.id ? { ...entry, status: "uploading", error: undefined } : entry));
      try {
        const result = await onUploadFile(file);
        uploadedCount += 1;
        setItems((current) => current.map((entry) => entry.id === item.id ? completeIntakeItem(entry, result) : entry));
      } catch (error) {
        setItems((current) => current.map((entry) => entry.id === item.id ? { ...entry, status: "error", error: error instanceof Error ? error.message : copy("Upload failed.", "Upload mislukt.") } : entry));
      }
    }
    setBusy(false);
    busyRef.current = false;
    onBusyChange(false);
    await finishIntakeBatch(uploadedCount, onFinished, () => {
      setCompletionNotice(copy(
        "Upload succeeded, but FAB could not refresh the dashboard. The documents are registered; refresh before retrying any upload.",
        "Upload geslaagd, maar FAB kon het dashboard niet vernieuwen. De documenten zijn geregistreerd; vernieuw eerst voordat u een upload opnieuw probeert.",
      ));
    });
  }

  return createPortal(
    <div className="fab-command-overlay" role="presentation" onMouseDown={(event) => { if (!busy && event.target === event.currentTarget) onClose(); }}>
      <aside className="fab-detail-drawer fab-intake-drawer" role="dialog" aria-modal="true" aria-labelledby="fab-intake-title" aria-describedby="fab-intake-description">
        <div className="fab-command-header">
          <div><span>{copy("Document intake", "Documentinname")}</span><h2 id="fab-intake-title">{copy("Add receipts and invoices", "Bonnen en facturen toevoegen")}</h2></div>
          <button ref={closeRef} className="fab-icon-button" onClick={onClose} disabled={busy} aria-label={copy("Close document intake", "Documentinname sluiten")} title={copy("Close document intake", "Documentinname sluiten")}><X aria-hidden="true" /></button>
        </div>
        <div className="fab-detail-body">
          <p id="fab-intake-description">{copy("Review files before they enter the authoritative local ledger. Uploading does not submit anything to an external bookkeeping platform.", "Controleer bestanden voordat ze het gezaghebbende lokale grootboek ingaan. Uploaden dient niets in bij een extern boekhoudplatform.")}</p>
          <input ref={inputRef} className="sr-only" type="file" multiple accept={ACCEPTED_EXTENSIONS.map((type) => `.${type}`).join(",")} onChange={(event) => { if (event.currentTarget.files) addFiles(event.currentTarget.files); event.currentTarget.value = ""; }} />
          <button
            className={`fab-drop-zone ${dragging ? "is-dragging" : ""}`}
            onClick={() => inputRef.current?.click()}
            onDragEnter={(event) => { event.preventDefault(); setDragging(true); }}
            onDragOver={(event) => event.preventDefault()}
            onDragLeave={() => setDragging(false)}
            onDrop={(event) => { event.preventDefault(); setDragging(false); addFiles(event.dataTransfer.files); }}
          >
            <UploadCloud aria-hidden="true" />
            <strong>{copy("Drop files here or choose files", "Sleep bestanden hierheen of kies bestanden")}</strong>
            <span>{copy("PDF, JPG, PNG, HEIC, TIFF, TXT, or CSV. Maximum 6 MB per file and 100 files per batch.", "PDF, JPG, PNG, HEIC, TIFF, TXT of CSV. Maximaal 6 MB per bestand en 100 bestanden per batch.")}</span>
          </button>

          <div className="fab-intake-summary"><strong>{items.length} {copy("selected", "geselecteerd")}</strong><span>{formatBytes(items.reduce((total, item) => total + item.fileSize, 0))} {copy("total", "totaal")}</span></div>
          {selectionNotice && <div className="fab-inline-error" role="alert">{selectionNotice}</div>}
          {completionNotice && <div className="fab-inline-error" role="alert">{completionNotice}</div>}
          <div className="fab-intake-list" aria-live="polite">
            {items.map((item) => (
              <div className={`fab-intake-row tone-${item.status === "error" ? "bad" : item.status === "complete" ? "good" : item.status === "uploading" ? "info" : "neutral"}`} key={item.id}>
                {item.status === "uploading" ? <Loader2 className="is-spinning" aria-hidden="true" /> : item.status === "complete" ? <CheckCircle2 aria-hidden="true" /> : item.status === "error" ? <AlertCircle aria-hidden="true" /> : <FileUp aria-hidden="true" />}
                <div>
                  <strong>{item.fileName}</strong>
                  <span>{formatBytes(item.fileSize)}{item.error ? ` - ${item.error}` : ` - ${intakeStatus(item, copy)}`}</span>
                  {item.result && <small>{intakeResult(item.result, copy)}</small>}
                </div>
                <button className="fab-icon-button" onClick={() => { setItems((current) => current.filter((entry) => entry.id !== item.id)); setSelectionNotice(""); }} disabled={busy} aria-label={`${copy("Remove", "Verwijder")} ${item.fileName}`} title={`${copy("Remove", "Verwijder")} ${item.fileName}`}><Trash2 aria-hidden="true" /></button>
              </div>
            ))}
            {!items.length && <div className="fab-empty-state compact"><FileUp aria-hidden="true" /><strong>{copy("No files selected", "Geen bestanden geselecteerd")}</strong><span>{copy("Add documents to review their type and size before upload.", "Voeg documenten toe om type en grootte voor de upload te controleren.")}</span></div>}
          </div>
          {!connected && <div className="fab-panel-state tone-bad" role="alert"><AlertCircle aria-hidden="true" /><div><strong>{copy("Local API disconnected", "Lokale API niet verbonden")}</strong><span>{copy("Selection is available, but upload requires the authoritative local FAB API.", "Selecteren is mogelijk, maar uploaden vereist de gezaghebbende lokale FAB-API.")}</span></div></div>}
          <div className="fab-detail-actions">
            <button className="fab-primary-button" onClick={() => { void uploadSelected(); }} disabled={!connected || busy || !queued.some((item) => item.file && isUploadable(item.file))}>{busy ? <Loader2 className="is-spinning" aria-hidden="true" /> : queued.some((item) => item.status === "error" && item.file && isUploadable(item.file)) ? <RotateCcw aria-hidden="true" /> : <FileUp aria-hidden="true" />}{busy ? copy("Uploading...", "Uploaden...") : copy("Upload ready files", "Gereedstaande bestanden uploaden")}</button>
            <button className="fab-secondary-button" onClick={() => { setItems([]); setSelectionNotice(""); }} disabled={busy || !items.length}>{copy("Clear selection", "Selectie wissen")}</button>
          </div>
        </div>
      </aside>
    </div>,
    document.body,
  );
}

function createFileItemId(): string {
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
    return crypto.randomUUID();
  }
  return `${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

function formatBytes(value: number): string {
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
  return `${(value / (1024 * 1024)).toFixed(1)} MB`;
}

function isUploadable(file: File): boolean {
  const extension = file.name.split(".").pop()?.toLowerCase() || "";
  return file.size <= MAX_UPLOAD_BYTES && ACCEPTED_EXTENSIONS.includes(extension);
}

function intakeStatus(item: IntakeItem, copy: (english: string, dutch: string) => string): string {
  if (item.status === "complete") return copy("registered", "geregistreerd");
  if (item.status === "uploading") return copy("uploading", "uploaden");
  if (item.status === "error") return copy("retry required", "opnieuw proberen vereist");
  return copy("ready", "gereed");
}

function intakeResult(result: FabRecord, copy: (english: string, dutch: string) => string): string {
  const document = asRecord(result.document);
  const documentId = text(document.id || document.documentId, "");
  const registered = text(result.filename, "");
  const submission = text(result.externalSubmission, "not_executed");
  const details = [
    documentId ? `${copy("Document", "Document")} #${documentId}` : "",
    registered ? `${copy("stored as", "opgeslagen als")} ${registered}` : "",
    submission === "not_executed" ? copy("no external submission", "geen externe indiening") : submission,
  ].filter(Boolean);
  return details.join(" · ");
}
