import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { startVisiblePoll } from "@/hooks/visiblePoll";
import {
  BellRing,
  Bot,
  CheckCircle2,
  FileScan,
  FolderSearch2,
  Landmark,
  RefreshCw,
  RotateCcw,
  ScanSearch,
  ShieldCheck,
  Unplug,
  X,
} from "lucide-react";
import { durationBetween, exactDateTime, humanize, type FabCommandId, type FabRecord } from "./fabView";
import { useFabLocale } from "./fabLocale";

const commandGroups: Array<{ label: string; labelNl: string; commands: Array<{ id: FabCommandId; label: string; labelNl: string; description: string; descriptionNl: string; icon: typeof Bot }> }> = [
  {
    label: "Daily operations",
    labelNl: "Dagelijkse verwerking",
    commands: [
      { id: "run_safe_cycle", label: "Run safe cycle", labelNl: "Veilige cyclus uitvoeren", description: "Collect, process, classify, reconcile, and prepare local evidence.", descriptionNl: "Verzamel, verwerk, classificeer, stem af en bereid lokaal bewijs voor.", icon: Bot },
      { id: "rescan_intake", label: "Rescan intake", labelNl: "Inname opnieuw scannen", description: "Register files from configured local and scanner folders.", descriptionNl: "Registreer bestanden uit ingestelde lokale en scannermappen.", icon: FolderSearch2 },
      { id: "process_imported", label: "Process imported", labelNl: "Import verwerken", description: "Run OCR, validation, duplicate checks, and classification.", descriptionNl: "Voer OCR, validatie, duplicaatcontrole en classificatie uit.", icon: FileScan },
      { id: "reprocess_incomplete", label: "Recover unread scans", labelNl: "Onleesbare scans herstellen", description: "Back up the ledger, then retry only review-gated documents with blank OCR.", descriptionNl: "Maak een back-up en probeer alleen controledocumenten met lege OCR opnieuw.", icon: ScanSearch },
      { id: "reprocess_review_queue", label: "Reassess review queue", labelNl: "Controlewachtrij opnieuw beoordelen", description: "Back up the ledger, then apply the latest extraction and validation logic to retained OCR.", descriptionNl: "Maak een back-up en pas de nieuwste extractie- en validatielogica toe op bewaarde OCR.", icon: RefreshCw },
      { id: "sync_sources", label: "Sync sources", labelNl: "Bronnen synchroniseren", description: "Collect from configured Gmail, Drive, Photos, and Freshdesk sources.", descriptionNl: "Verzamel uit ingestelde Gmail-, Drive-, Photos- en Freshdesk-bronnen.", icon: RefreshCw },
    ],
  },
  {
    label: "Control and recovery",
    labelNl: "Controle en herstel",
    commands: [
      { id: "run_reconciliation", label: "Run reconciliation", labelNl: "Afstemming uitvoeren", description: "Match bank transactions against document-backed records.", descriptionNl: "Koppel banktransacties aan documentonderbouwde records.", icon: ScanSearch },
      { id: "run_due_recovery", label: "Run due recovery", labelNl: "Gepland herstel uitvoeren", description: "Retry the failed safe step, then resume only its proven local descendants.", descriptionNl: "Herhaal de veilige mislukte stap en hervat alleen bewezen lokale vervolgstappen.", icon: RotateCcw },
      { id: "refresh_notifications", label: "Refresh notifications", labelNl: "Meldingen vernieuwen", description: "Rebuild local exception and notification signals.", descriptionNl: "Bouw lokale uitzonderings- en meldingssignalen opnieuw op.", icon: BellRing },
    ],
  },
  {
    label: "Close and compliance",
    labelNl: "Afsluiting en compliance",
    commands: [
      { id: "run_due_reports", label: "Run due reports", labelNl: "Geplande rapporten uitvoeren", description: "Generate scheduled checksum-bound local report artifacts.", descriptionNl: "Genereer geplande lokale rapporten met controlegetal.", icon: Landmark },
      { id: "assess_compliance", label: "Assess compliance", labelNl: "Compliance beoordelen", description: "Prepare a provisional Dutch VAT and retention assessment.", descriptionNl: "Bereid een voorlopige Nederlandse btw- en bewaartermijnbeoordeling voor.", icon: ShieldCheck },
    ],
  },
];

type FabCommandDrawerProps = {
  open: boolean;
  connected: boolean;
  pendingCommand: FabCommandId | null;
  commandStartedAt: string | null;
  lastCommand: { id: FabCommandId; status: string; startedAt: string | null; finishedAt: string; summary: string[] } | null;
  onClose: () => void;
  onCommand: (commandId: FabCommandId, payload?: FabRecord) => void;
};

export function FabCommandDrawer({ open, connected, pendingCommand, commandStartedAt, lastCommand, onClose, onCommand }: FabCommandDrawerProps) {
  const { lang, copy, status, dateLocale } = useFabLocale();
  const closeRef = useRef<HTMLButtonElement>(null);
  const restoreFocusRef = useRef<HTMLElement | null>(null);
  const [now, setNow] = useState(Date.now());

  useEffect(() => {
    if (!open || !pendingCommand) return;
    setNow(Date.now());
    return startVisiblePoll(() => setNow(Date.now()), { intervalMs: 1_000 });
  }, [open, pendingCommand]);

  useEffect(() => {
    if (!open) return;
    restoreFocusRef.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    document.body.classList.add("fab-dialog-open");
    closeRef.current?.focus();
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
      if (event.key !== "Tab") return;
      const dialog = closeRef.current?.closest<HTMLElement>("[role=dialog]");
      const focusable = dialog ? Array.from(dialog.querySelectorAll<HTMLElement>("button:not(:disabled), a[href]")) : [];
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
  }, [open, onClose]);

  if (!open) return null;

  return createPortal(
    <div className="fab-command-overlay" role="presentation" onMouseDown={(event) => { if (event.target === event.currentTarget) onClose(); }}>
      <aside className="fab-command-drawer" role="dialog" aria-modal="true" aria-labelledby="fab-command-title" aria-describedby="fab-command-description">
        <div className="fab-command-header">
          <div><span>{copy("Operator controls", "Operatorbediening")}</span><h2 id="fab-command-title">{copy("Command centre", "Opdrachtencentrum")}</h2></div>
          <button ref={closeRef} className="fab-icon-button" onClick={onClose} aria-label={copy("Close commands", "Opdrachten sluiten")} title={copy("Close commands", "Opdrachten sluiten")}><X aria-hidden="true" /></button>
        </div>
        <div id="fab-command-description" className="fab-command-policy"><ShieldCheck aria-hidden="true" /><span>{copy("Commands mutate only FAB's local operating state. Approvals, exports, and external submissions remain excluded.", "Opdrachten wijzigen alleen de lokale operationele staat van FAB. Goedkeuringen, exports en externe indieningen blijven uitgesloten.")}</span></div>

        {!connected && <div className="fab-command-offline" role="alert"><Unplug aria-hidden="true" /><div><strong>{copy("Local API disconnected", "Lokale API niet verbonden")}</strong><span>{copy("Commands remain visible for inspection but cannot run. Reconnect FAB, then retry.", "Opdrachten blijven zichtbaar maar kunnen niet draaien. Verbind FAB opnieuw en probeer het opnieuw.")}</span></div></div>}
        {pendingCommand && <div className="fab-command-progress" role="status"><RefreshCw className="is-spinning" aria-hidden="true" /><div><strong>{humanize(pendingCommand)} {copy("is running", "wordt uitgevoerd")}</strong><span>{copy("Started", "Gestart")} {exactDateTime(commandStartedAt, dateLocale)}{commandStartedAt ? ` · ${copy("elapsed", "verstreken")} ${durationBetween(commandStartedAt, new Date(now).toISOString())}` : ""}. {copy("The dashboard will refresh when the API returns a final result.", "Het dashboard vernieuwt zodra de API een eindresultaat teruggeeft.")}</span></div></div>}
        {!pendingCommand && lastCommand && <div className="fab-command-progress is-complete" role="status"><CheckCircle2 aria-hidden="true" /><div><strong>{humanize(lastCommand.id)}: {status(lastCommand.status)}</strong><span>{copy("Finished", "Voltooid")} {exactDateTime(lastCommand.finishedAt, dateLocale)}{lastCommand.startedAt ? ` ${copy("after", "na")} ${durationBetween(lastCommand.startedAt, lastCommand.finishedAt)}` : ""}.</span>{lastCommand.summary.length > 0 && <small>{lastCommand.summary.join(" · ")}</small>}</div></div>}

        <div className="fab-command-list">
          {commandGroups.map((group) => (
            <section className="fab-command-group" key={group.label} aria-labelledby={`fab-command-${group.label.replaceAll(" ", "-")}`}>
              <h3 id={`fab-command-${group.label.replaceAll(" ", "-")}`}>{lang === "nl" ? group.labelNl : group.label}</h3>
              {group.commands.map(({ id, label, labelNl, description, descriptionNl, icon: Icon }) => (
                <button key={id} className="fab-command-row" onClick={() => onCommand(id)} disabled={!connected || Boolean(pendingCommand)}>
                  <span className="fab-command-icon"><Icon className={pendingCommand === id ? "is-spinning" : ""} aria-hidden="true" /></span>
                  <span><strong>{lang === "nl" ? labelNl : label}</strong><small>{lang === "nl" ? descriptionNl : description}</small></span>
                  <span className="fab-command-risk">{copy("Local safe", "Lokaal veilig")}</span>
                </button>
              ))}
            </section>
          ))}
        </div>
        <div className="fab-hai-note"><Bot aria-hidden="true" /><span><strong>{copy("HAI-ready contract", "HAI-gereed contract")}</strong>{copy("The same governed command catalog is exposed through the local HAI connector with explicit allowlisting and idempotent request IDs.", "Dezelfde beheerde opdrachtencatalogus is beschikbaar via de lokale HAI-koppeling met expliciete toestemming en idempotente aanvraag-ID's.")}</span></div>
      </aside>
    </div>,
    document.body,
  );
}
