import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import {
  AlertCircle,
  CheckCircle2,
  FileSpreadsheet,
  Landmark,
  Loader2,
  ShieldCheck,
  UploadCloud,
  X,
} from "lucide-react";
import { useFabLocale } from "./fabLocale";
import {
  inferBankStatementFormat,
  validateBankStatementFile,
  type FabBankStatementFormat,
} from "./fabBankStatement";
import { count, exactDateTime, records, statusTone, text, type FabRecord } from "./fabView";

const ACCEPTED_STATEMENTS = ".csv,.json,.xml,.camt,.sta,.mt940";

type FabBankImportDrawerProps = {
  open: boolean;
  connected: boolean;
  busy: boolean;
  recentImports: FabRecord[];
  onClose: () => void;
  onImport: (
    file: File,
    format: FabBankStatementFormat,
    accountIdentifier: string,
  ) => Promise<FabRecord>;
  onFinished: (result: FabRecord) => Promise<void> | void;
};

export function FabBankImportDrawer({
  open,
  connected,
  busy,
  recentImports,
  onClose,
  onImport,
  onFinished,
}: FabBankImportDrawerProps) {
  const { copy, status, dateLocale } = useFabLocale();
  const [selectedFile, setSelectedFile] = useState<File | null>(null);
  const [accountIdentifier, setAccountIdentifier] = useState("default");
  const [dragging, setDragging] = useState(false);
  const [error, setError] = useState("");
  const [lastResult, setLastResult] = useState<FabRecord | null>(null);
  const closeRef = useRef<HTMLButtonElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const restoreFocusRef = useRef<HTMLElement | null>(null);
  const busyRef = useRef(busy);
  const onCloseRef = useRef(onClose);
  const validation = selectedFile
    ? validateBankStatementFile(selectedFile)
    : { format: null, error: null };
  const conflictRows = records(lastResult?.identityConflictRows);

  useEffect(() => {
    busyRef.current = busy;
  }, [busy]);

  useEffect(() => {
    onCloseRef.current = onClose;
  }, [onClose]);

  useEffect(() => {
    if (!open) return;
    restoreFocusRef.current = document.activeElement instanceof HTMLElement
      ? document.activeElement
      : null;
    document.body.classList.add("fab-dialog-open");
    closeRef.current?.focus();
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape" && !busyRef.current) onCloseRef.current();
      if (event.key !== "Tab") return;
      const dialog = closeRef.current?.closest<HTMLElement>("[role=dialog]");
      const focusable = dialog
        ? Array.from(dialog.querySelectorAll<HTMLElement>("button:not(:disabled), input:not(:disabled)"))
        : [];
      if (!focusable.length) return;
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      }
      if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    };
    window.addEventListener("keydown", onKeyDown);
    return () => {
      window.removeEventListener("keydown", onKeyDown);
      document.body.classList.remove("fab-dialog-open");
      restoreFocusRef.current?.focus();
    };
  }, [open]);

  if (!open) return null;

  function chooseFile(file: File | null) {
    setSelectedFile(file);
    setLastResult(null);
    if (!file) {
      setError("");
      return;
    }
    const result = validateBankStatementFile(file);
    setError(validationMessage(result.error, copy));
  }

  async function importStatement() {
    const safeAccount = accountIdentifier.trim();
    if (!selectedFile || !validation.format || validation.error || !safeAccount || busy) return;
    setError("");
    try {
      const result = await onImport(selectedFile, validation.format, safeAccount);
      setLastResult(result);
      await onFinished(result);
    } catch (cause) {
      setError(cause instanceof Error
        ? cause.message
        : copy("Bank statement import failed.", "Import van bankafschrift mislukt."));
    }
  }

  return createPortal(
    <div
      className="fab-command-overlay"
      role="presentation"
      onMouseDown={(event) => { if (!busy && event.target === event.currentTarget) onClose(); }}
    >
      <aside
        className="fab-detail-drawer fab-bank-import-drawer"
        role="dialog"
        aria-modal="true"
        aria-labelledby="fab-bank-import-title"
        aria-describedby="fab-bank-import-description"
      >
        <div className="fab-command-header">
          <div>
            <span>{copy("Local banking evidence", "Lokaal bankbewijs")}</span>
            <h2 id="fab-bank-import-title">{copy("Import bank statement", "Bankafschrift importeren")}</h2>
          </div>
          <button
            ref={closeRef}
            className="fab-icon-button"
            onClick={onClose}
            disabled={busy}
            aria-label={copy("Close bank import", "Bankimport sluiten")}
            title={copy("Close bank import", "Bankimport sluiten")}
          >
            <X aria-hidden="true" />
          </button>
        </div>

        <div className="fab-detail-body">
          <p id="fab-bank-import-description">
            {copy(
              "Import a bank export into FAB's local ledger. FAB validates each row, preserves repeated legitimate transactions, ignores exact re-imports, and starts local reconciliation without sending data externally.",
              "Importeer een bankexport in het lokale FAB-grootboek. FAB valideert elke regel, behoudt legitieme herhaalde transacties, negeert exacte herimports en start lokale afstemming zonder gegevens extern te versturen.",
            )}
          </p>

          <label className="fab-bank-account-field">
            <span>{copy("Account identifier", "Rekeningidentificatie")}</span>
            <input
              value={accountIdentifier}
              maxLength={200}
              disabled={busy}
              onChange={(event) => setAccountIdentifier(event.target.value)}
              placeholder={copy("IBAN or stable account label", "IBAN of vaste rekeningnaam")}
            />
            <small>{copy("Use the same identifier for later exports from this account.", "Gebruik voor latere exports van deze rekening dezelfde identificatie.")}</small>
          </label>

          <input
            ref={inputRef}
            className="sr-only"
            type="file"
            accept={ACCEPTED_STATEMENTS}
            onChange={(event) => {
              chooseFile(event.currentTarget.files?.[0] || null);
              event.currentTarget.value = "";
            }}
          />
          <button
            type="button"
            className={`fab-drop-zone fab-bank-drop-zone ${dragging ? "is-dragging" : ""}`}
            disabled={busy}
            onClick={() => inputRef.current?.click()}
            onDragEnter={(event) => { event.preventDefault(); setDragging(true); }}
            onDragOver={(event) => event.preventDefault()}
            onDragLeave={() => setDragging(false)}
            onDrop={(event) => {
              event.preventDefault();
              setDragging(false);
              chooseFile(event.dataTransfer.files[0] || null);
            }}
          >
            <UploadCloud aria-hidden="true" />
            <strong>{selectedFile ? selectedFile.name : copy("Drop or choose one statement", "Sleep of kies een bankafschrift")}</strong>
            <span>{selectedFile
              ? `${formatBytes(selectedFile.size)} - ${formatLabel(inferBankStatementFormat(selectedFile.name))}`
              : copy("CSV, JSON, CAMT/XML, or MT940. Maximum 4 MB.", "CSV, JSON, CAMT/XML of MT940. Maximaal 4 MB.")}</span>
          </button>

          {lastResult && (
            <div className={`fab-bank-import-result tone-${statusTone(lastResult.status)}`} role="status">
              {count(lastResult.identityConflicts) > 0
                ? <AlertCircle aria-hidden="true" />
                : <CheckCircle2 aria-hidden="true" />}
              <div>
                <strong>{count(lastResult.identityConflicts) > 0
                  ? copy("Transaction identity conflicts need review", "Identiteitsconflicten in transacties vereisen controle")
                  : copy("Import recorded", "Import vastgelegd")}</strong>
                <span>
                  {count(lastResult.rowsImported)} {copy("new", "nieuw")}
                  {" / "}{count(lastResult.duplicates)} {copy("already present", "reeds aanwezig")}
                  {" / "}{count(lastResult.identityConflicts)} {copy("identity conflicts", "identiteitsconflicten")}
                  {" / "}{count(lastResult.skipped)} {copy("skipped", "overgeslagen")}
                </span>
                {count(lastResult.identityConflicts) > 0 && (
                  <small>
                    {copy("Earlier ledger rows were left unchanged. Conflicting statement rows:", "Bestaande grootboekregels zijn ongewijzigd gebleven. Conflicterende afschriftregels:")} {conflictRows.map((row) => text(row.row)).join(", ")}
                    {lastResult.identityConflictsTruncated === true ? " ..." : ""}
                  </small>
                )}
                <small>{copy("External submission", "Externe indiening")}: {status(lastResult.externalSubmission)}</small>
              </div>
            </div>
          )}

          {!connected && (
            <div className="fab-panel-state tone-bad" role="alert">
              <AlertCircle aria-hidden="true" />
              <div>
                <strong>{copy("Local API disconnected", "Lokale API niet verbonden")}</strong>
                <span>{copy("Bank imports require the authoritative local FAB ledger.", "Bankimports vereisen het gezaghebbende lokale FAB-grootboek.")}</span>
              </div>
            </div>
          )}
          {error && <div className="fab-inline-error" role="alert">{error}</div>}

          <div className="fab-detail-actions">
            <button
              className="fab-primary-button"
              type="button"
              disabled={!connected || busy || !selectedFile || Boolean(validation.error) || !accountIdentifier.trim()}
              onClick={() => { void importStatement(); }}
            >
              {busy ? <Loader2 className="is-spinning" aria-hidden="true" /> : <Landmark aria-hidden="true" />}
              {busy ? copy("Importing...", "Importeren...") : copy("Import and reconcile", "Importeren en afstemmen")}
            </button>
            <button className="fab-secondary-button" type="button" disabled={busy || !selectedFile} onClick={() => chooseFile(null)}>
              <X aria-hidden="true" /> {copy("Clear", "Wissen")}
            </button>
          </div>

          <div className="fab-drive-safety-note">
            <ShieldCheck aria-hidden="true" />
            <div>
              <strong>{copy("Local and idempotent", "Lokaal en idempotent")}</strong>
              <span>{copy("The original file is not uploaded to a bank or bookkeeping provider. Imported rows and reconciliation outcomes remain auditable in SQLite.", "Het originele bestand wordt niet naar een bank of boekhoudprovider geupload. Geimporteerde regels en afstemmingsresultaten blijven controleerbaar in SQLite.")}</span>
            </div>
          </div>

          <section className="fab-bank-import-history">
            <div className="fab-subsection-heading">
              <div><span>{copy("Ledger evidence", "Grootboekbewijs")}</span><h3>{copy("Recent imports", "Recente imports")}</h3></div>
            </div>
            {recentImports.length ? recentImports.map((item) => (
              <div className="fab-bank-import-history-row" key={text(item.id)}>
                <FileSpreadsheet aria-hidden="true" />
                <div>
                  <strong>{text(item.filename, copy("Bank statement", "Bankafschrift"))}</strong>
                  <span>{text(item.account_identifier, "default")} - {count(item.rows_imported)} {copy("rows", "regels")}</span>
                </div>
                <div>
                  <span className={`fab-status-chip tone-${statusTone(item.status)}`}>{status(item.status)}</span>
                  {count(item.identity_conflicts) > 0 && <small>{count(item.identity_conflicts)} {copy("identity conflicts", "identiteitsconflicten")}</small>}
                  <small>{exactDateTime(item.updated_at || item.created_at, dateLocale)}</small>
                </div>
              </div>
            )) : (
              <div className="fab-empty-state compact">
                <FileSpreadsheet aria-hidden="true" />
                <strong>{copy("No statement imports yet", "Nog geen bankafschriften geimporteerd")}</strong>
                <span>{copy("The first completed import will appear here.", "De eerste voltooide import verschijnt hier.")}</span>
              </div>
            )}
          </section>
        </div>
      </aside>
    </div>,
    document.body,
  );
}

function validationMessage(
  error: "empty" | "too_large" | "unsupported" | null,
  copy: (english: string, dutch: string) => string,
): string {
  if (error === "empty") return copy("The selected bank statement is empty.", "Het geselecteerde bankafschrift is leeg.");
  if (error === "too_large") return copy("The bank statement exceeds the 4 MB limit.", "Het bankafschrift overschrijdt de limiet van 4 MB.");
  if (error === "unsupported") return copy("Choose a CSV, JSON, CAMT/XML, or MT940 statement.", "Kies een CSV-, JSON-, CAMT/XML- of MT940-afschrift.");
  return "";
}

function formatLabel(format: FabBankStatementFormat | null): string {
  if (format === "camt") return "CAMT/XML";
  return format ? format.toUpperCase() : "Unsupported";
}

function formatBytes(value: number): string {
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
  return `${(value / (1024 * 1024)).toFixed(1)} MB`;
}
