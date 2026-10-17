import { useEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { startVisiblePoll } from "@/hooks/visiblePoll";
import {
  AlertCircle,
  Bot,
  CheckCircle2,
  ClipboardCopy,
  ExternalLink,
  MonitorUp,
  RefreshCw,
  ShieldCheck,
  X,
} from "lucide-react";
import { useFabLocale } from "./fabLocale";
import { fabOperatorLink } from "./fabOperatorLink";
import { bool, statusTone, text, type FabRecord } from "./fabView";

type FabWaveReceiptExecutorDrawerProps = {
  open: boolean;
  connected: boolean;
  executor: FabRecord;
  localApiEndpoint: string;
  onClose: () => void;
  onRefresh: () => Promise<void> | void;
};

export function FabWaveReceiptExecutorDrawer({
  open,
  connected,
  executor,
  localApiEndpoint,
  onClose,
  onRefresh,
}: FabWaveReceiptExecutorDrawerProps) {
  const { copy, status: localizedStatus } = useFabLocale();
  const [copied, setCopied] = useState(false);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState("");
  const closeRef = useRef<HTMLButtonElement>(null);
  const restoreFocusRef = useRef<HTMLElement | null>(null);
  const status = text(executor.status, "not_connected");
  const ready = bool(executor.ready);
  const configuredBusinessId = text(executor.configuredBusinessId || executor.businessId, "");
  const configuredBusinessUuid = text(executor.configuredBusinessUuid, "");
  const executorId = text(executor.executorId, "");
  const sessionId = text(executor.sessionId, "");
  const requiredCapabilities = strings(executor.requiredCapabilities);
  const capabilities = strings(executor.capabilities);
  const missingCapabilities = strings(executor.missingCapabilities);
  const sessionPaired = Boolean(executorId && sessionId);
  const heartbeatFresh = sessionPaired && !["stale", "not_connected", "stopped"].includes(status);
  const capabilitiesComplete = requiredCapabilities.length > 0 && missingCapabilities.length === 0;
  const businessMatches = !sessionPaired || bool(executor.businessMatches);
  const manifestPath = text(executor.haiManifestPath, "/api/hai/manifest");
  const manifestUrl = endpointUrl(localApiEndpoint, manifestPath);
  const statusUrl = endpointUrl(localApiEndpoint, "/api/wave/receipt-executor/status");
  const waveUrl = configuredBusinessUuid
    ? `https://next.waveapps.com/${encodeURIComponent(configuredBusinessUuid)}/dashboard/`
    : "https://my.waveapps.com/login/";
  const pairingBrief = useMemo(() => JSON.stringify({
    version: text(executor.version, "fab-wave-receipt-executor-session-v1"),
    fabApiBaseUrl: localApiEndpoint,
    configuredBusinessId: configuredBusinessId || null,
    configuredBusinessUuid: configuredBusinessUuid || null,
    credentialPolicy: text(executor.credentialPolicy, "browser_session_owned_by_user_never_stored_in_fab"),
    requiredCapabilities,
    haiManifestUrl: manifestUrl,
    statusUrl,
    pairing: executor.pairing || {
      session: { method: "POST", path: "/api/wave/receipt-executor/session" },
      claim: { method: "POST", path: "/api/wave/receipt-executor/claim" },
      release: { method: "POST", path: "/api/wave/receipt-executor/release" },
      attachmentReadback: {
        method: "POST",
        pathTemplate: "/api/drive-wave/documents/{documentId}/attachment-readback",
      },
    },
  }), [configuredBusinessId, configuredBusinessUuid, executor.credentialPolicy, executor.pairing, executor.version, localApiEndpoint, manifestUrl, requiredCapabilities, statusUrl]);

  useEffect(() => {
    if (!open) return;
    restoreFocusRef.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    document.body.classList.add("fab-dialog-open");
    closeRef.current?.focus();
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
      if (event.key !== "Tab") return;
      const dialog = closeRef.current?.closest<HTMLElement>("[role=dialog]");
      const focusable = dialog ? Array.from(dialog.querySelectorAll<HTMLElement>("a, button:not(:disabled)")) : [];
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

  useEffect(() => {
    if (!open || !sessionPaired || ready) return;
    return startVisiblePoll(onRefresh, {
      intervalMs: 10_000,
      initialDelayMs: 5_000,
      onError: (cause) => setError(cause instanceof Error ? cause.message : copy("Executor status could not be refreshed.", "Executorstatus kon niet worden vernieuwd.")),
    });
  }, [copy, onRefresh, open, ready, sessionPaired]);

  if (!open) return null;

  async function copyPairingBrief() {
    setError("");
    try {
      await navigator.clipboard.writeText(pairingBrief);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 2_000);
    } catch {
      setError(copy("The pairing brief could not be copied.", "De koppelingsinstructie kon niet worden gekopieerd."));
    }
  }

  async function refresh() {
    setError("");
    setRefreshing(true);
    try {
      await onRefresh();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : copy("Executor status could not be refreshed.", "Executorstatus kon niet worden vernieuwd."));
    } finally {
      setRefreshing(false);
    }
  }

  return createPortal(
    <div className="fab-command-overlay" role="presentation" onMouseDown={(event) => { if (event.target === event.currentTarget) onClose(); }}>
      <aside className="fab-detail-drawer fab-receipt-executor-drawer" role="dialog" aria-modal="true" aria-labelledby="fab-receipt-executor-title" aria-describedby="fab-receipt-executor-description">
        <div className="fab-command-header">
          <div><span>{copy("Controlled downstream execution", "Beheerste vervolguitvoering")}</span><h2 id="fab-receipt-executor-title">{copy("Wave receipt session", "Wave-bewijssessie")}</h2></div>
          <button ref={closeRef} className="fab-icon-button" type="button" onClick={onClose} aria-label={copy("Close receipt session setup", "Bewijssessie sluiten")} title={copy("Close receipt session setup", "Bewijssessie sluiten")}><X aria-hidden="true" /></button>
        </div>
        <div className="fab-detail-body">
          <p id="fab-receipt-executor-description">{copy("Pair one user-owned Wave browser with FAB so attachment upload, transaction review, and exact download verification can be executed without storing Wave login data.", "Koppel een Wave-browser van de gebruiker aan FAB voor bijlage-upload, boekingscontrole en exacte downloadverificatie zonder Wave-inloggegevens op te slaan.")}</p>

          <div className="fab-wave-status-line">
            <span className={`fab-status-chip tone-${statusTone(status)}`}>{localizedStatus(status)}</span>
            <small>{text(executor.nextAction, copy("Connect the supervised receipt executor.", "Koppel de gecontroleerde bewijsuitvoerder."))}</small>
          </div>

          <div className="fab-drive-setup-progress">
            <SetupStep complete={Boolean(configuredBusinessId)} icon={ShieldCheck} title={copy("Wave business binding", "Wave-bedrijfskoppeling")} detail={configuredBusinessId || copy("Complete Wave setup first", "Voltooi eerst de Wave-instellingen")} />
            <SetupStep complete={heartbeatFresh} active={sessionPaired && !heartbeatFresh} icon={MonitorUp} title={copy("Executor heartbeat", "Executor-hartslag")} detail={sessionPaired ? text(executor.lastSeenAt, localizedStatus(status)) : copy("No executor paired", "Geen executor gekoppeld")} />
            <SetupStep complete={capabilitiesComplete} icon={Bot} title={copy("Required capabilities", "Vereiste mogelijkheden")} detail={capabilitiesComplete ? copy("Upload, download, review, and observed fields available", "Upload, download, controle en waargenomen velden beschikbaar") : `${missingCapabilities.length || requiredCapabilities.length} ${copy("capabilities missing", "mogelijkheden ontbreken")}`} />
            <SetupStep complete={ready && businessMatches} icon={CheckCircle2} title={copy("Delivery readiness", "Verwerkingsgereedheid")} detail={ready && businessMatches ? copy("Receipt work orders can be claimed", "Bewijsopdrachten kunnen worden opgepakt") : localizedStatus(status)} />
          </div>

          <section className="fab-drive-credential-panel">
            <div className="fab-subsection-heading"><div><span>{copy("Pairing", "Koppeling")}</span><h3>{copy("Connect HAI or a supervised browser", "Koppel HAI of een gecontroleerde browser")}</h3></div></div>
            <p>{copy("Give the pairing brief to the supervising executor. It registers only identifiers, heartbeat state, and capabilities; Wave cookies, tokens, and passwords are rejected.", "Geef de koppelingsinstructie aan de begeleidende executor. Alleen identificaties, hartslagstatus en mogelijkheden worden geregistreerd; Wave-cookies, tokens en wachtwoorden worden geweigerd.")}</p>
            <div className="fab-detail-actions">
              <button className="fab-primary-button" type="button" onClick={() => { void copyPairingBrief(); }} disabled={!connected}><ClipboardCopy aria-hidden="true" /> {copied ? copy("Pairing brief copied", "Koppelingsinstructie gekopieerd") : copy("Copy pairing brief", "Koppelingsinstructie kopieren")}</button>
              <a className="fab-secondary-button" href={fabOperatorLink(manifestPath)} target="_blank" rel="noreferrer"><Bot aria-hidden="true" /> {copy("Open HAI manifest", "HAI-manifest openen")}</a>
              <a className="fab-secondary-button" href={waveUrl} target="_blank" rel="noreferrer"><ExternalLink aria-hidden="true" /> {copy("Open Wave", "Wave openen")}</a>
            </div>
          </section>

          {sessionPaired && <section className="fab-executor-session-panel" aria-label={copy("Current executor session", "Huidige executorsessie")}>
            <div><span>{copy("Executor", "Executor")}</span><strong>{executorId}</strong></div>
            <div><span>{copy("Session", "Sessie")}</span><strong>{sessionId}</strong></div>
            <div><span>{copy("Browser", "Browser")}</span><strong>{text(executor.browser, "-")}</strong></div>
            <div><span>{copy("Current document", "Huidig document")}</span><strong>{text(executor.currentDocumentId, "-")}</strong></div>
          </section>}

          {!capabilitiesComplete && requiredCapabilities.length > 0 && <div className="fab-executor-capabilities">
            {requiredCapabilities.map((capability) => <span key={capability} className={`fab-status-chip tone-${capabilities.includes(capability) ? "good" : "warn"}`}>{capability.replaceAll("_", " ")}</span>)}
          </div>}

          <div className="fab-detail-actions">
            <button className="fab-secondary-button" type="button" onClick={() => { void refresh(); }} disabled={refreshing}><RefreshCw className={refreshing ? "is-spinning" : ""} aria-hidden="true" /> {copy("Refresh status", "Status vernieuwen")}</button>
            <a className="fab-secondary-button" href={fabOperatorLink("/api/wave/receipt-executor/status")} target="_blank" rel="noreferrer"><ExternalLink aria-hidden="true" /> {copy("Inspect contract", "Contract bekijken")}</a>
          </div>

          <div className="fab-drive-safety-note"><ShieldCheck aria-hidden="true" /><div><strong>{copy("No source file is archived on session status alone", "Geen bronbestand wordt alleen op sessiestatus gearchiveerd")}</strong><span>{copy("FAB still requires the reviewed Wave transaction and a downloaded attachment whose SHA-256 equals the retained source file.", "FAB vereist nog steeds de gecontroleerde Wave-boeking en een gedownloade bijlage waarvan de SHA-256 gelijk is aan het behouden bronbestand.")}</span></div></div>
          {!connected && <div className="fab-panel-state tone-bad" role="alert"><AlertCircle aria-hidden="true" /><div><strong>{copy("Local API disconnected", "Lokale API niet verbonden")}</strong><span>{copy("Receipt executor pairing requires the authoritative local FAB API.", "Koppeling van de bewijsuitvoerder vereist de gezaghebbende lokale FAB-API.")}</span></div></div>}
          {error && <div className="fab-inline-error" role="alert">{error}</div>}
        </div>
      </aside>
    </div>,
    document.body,
  );
}

type SetupStepProps = {
  complete: boolean;
  active?: boolean;
  icon: typeof MonitorUp;
  title: string;
  detail: string;
};

function SetupStep({ complete, active, icon: Icon, title, detail }: SetupStepProps) {
  return <div className={`fab-drive-setup-step tone-${complete ? "good" : active ? "info" : "neutral"}`}><span>{complete ? <CheckCircle2 aria-hidden="true" /> : <Icon aria-hidden="true" />}</span><div><strong>{title}</strong><small>{detail}</small></div></div>;
}

function strings(value: unknown): string[] {
  if (!Array.isArray(value)) return [];
  return value.flatMap((item) => typeof item === "string" && item.trim() ? [item.trim()] : []);
}

function endpointUrl(endpoint: string, path: string): string {
  return `${endpoint.replace(/\/+$/, "")}/${path.replace(/^\/+/, "")}`;
}
