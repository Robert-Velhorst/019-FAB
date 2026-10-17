import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { startVisiblePoll } from "@/hooks/visiblePoll";
import {
  AlertCircle,
  CheckCircle2,
  Cloud,
  ExternalLink,
  FileKey2,
  Loader2,
  RefreshCw,
  ShieldCheck,
  UploadCloud,
  X,
} from "lucide-react";
import { useFabLocale } from "./fabLocale";
import { bool, text, type FabRecord } from "./fabView";

const MAX_CREDENTIAL_BYTES = 64 * 1024;

type FabGoogleDriveSetupDrawerProps = {
  open: boolean;
  connected: boolean;
  authorization: FabRecord;
  busy: boolean;
  onClose: () => void;
  onInstallCredentials: (file: File, replace: boolean) => Promise<void>;
  onStartAuthorization: () => Promise<void>;
  onRefresh: () => Promise<void> | void;
};

export function FabGoogleDriveSetupDrawer({
  open,
  connected,
  authorization,
  busy,
  onClose,
  onInstallCredentials,
  onStartAuthorization,
  onRefresh,
}: FabGoogleDriveSetupDrawerProps) {
  const { copy } = useFabLocale();
  const [selectedFile, setSelectedFile] = useState<File | null>(null);
  const [replace, setReplace] = useState(false);
  const [error, setError] = useState("");
  const closeRef = useRef<HTMLButtonElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const restoreFocusRef = useRef<HTMLElement | null>(null);
  const credentialsPresent = bool(authorization.credentialsPresent);
  const tokenPresent = bool(authorization.tokenPresent);
  const reauthorizationRequired = bool(authorization.reauthorizationRequired);
  const driveAuthorized = tokenPresent && !reauthorizationRequired;
  const folderConfigured = bool(authorization.folderConfigured);
  const authorizationInProgress = bool(authorization.authorizationInProgress);

  useEffect(() => {
    if (!open) return;
    restoreFocusRef.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    document.body.classList.add("fab-dialog-open");
    closeRef.current?.focus();
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape" && !busy) onClose();
      if (event.key !== "Tab") return;
      const dialog = closeRef.current?.closest<HTMLElement>("[role=dialog]");
      const focusable = dialog ? Array.from(dialog.querySelectorAll<HTMLElement>("a, button:not(:disabled), input:not(:disabled)")) : [];
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
  }, [busy, onClose, open]);

  useEffect(() => {
    if (!open || !authorizationInProgress) return;
    return startVisiblePoll(onRefresh, {
      intervalMs: 10_000,
      initialDelayMs: 3_000,
      onError: (cause) => setError(cause instanceof Error ? cause.message : copy("Drive status could not be refreshed.", "Drivestatus kon niet worden vernieuwd.")),
    });
  }, [authorizationInProgress, copy, onRefresh, open]);

  if (!open) return null;

  function selectFile(file: File | null) {
    setSelectedFile(file);
    if (!file) return;
    if (!file.name.toLowerCase().endsWith(".json")) {
      setError(copy("Select the desktop OAuth JSON downloaded from Google Cloud.", "Selecteer de desktop-OAuth-JSON uit Google Cloud."));
    } else if (file.size > MAX_CREDENTIAL_BYTES) {
      setError(copy("The credential JSON exceeds 64 KB.", "De referentie-JSON is groter dan 64 KB."));
    } else {
      setError("");
    }
  }

  async function installCredentials() {
    if (!selectedFile || busy) return;
    if (!selectedFile.name.toLowerCase().endsWith(".json") || selectedFile.size > MAX_CREDENTIAL_BYTES) return;
    if (credentialsPresent && !replace) {
      setError(copy("Confirm credential replacement before rotating the existing OAuth client.", "Bevestig vervanging voordat je de bestaande OAuth-client roteert."));
      return;
    }
    setError("");
    try {
      await onInstallCredentials(selectedFile, replace);
      setSelectedFile(null);
      setReplace(false);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : copy("Credential installation failed.", "Installatie van referenties is mislukt."));
    }
  }

  async function authorize() {
    setError("");
    try {
      await onStartAuthorization();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : copy("Authorization could not start.", "Autorisatie kon niet starten."));
    }
  }

  return createPortal(
    <div className="fab-command-overlay" role="presentation" onMouseDown={(event) => { if (!busy && event.target === event.currentTarget) onClose(); }}>
      <aside className="fab-detail-drawer fab-drive-setup-drawer" role="dialog" aria-modal="true" aria-labelledby="fab-drive-setup-title" aria-describedby="fab-drive-setup-description">
        <div className="fab-command-header">
          <div><span>{copy("Source connection", "Bronkoppeling")}</span><h2 id="fab-drive-setup-title">Google Drive</h2></div>
          <button ref={closeRef} className="fab-icon-button" onClick={onClose} disabled={busy} aria-label={copy("Close Drive setup", "Drive-instellingen sluiten")} title={copy("Close Drive setup", "Drive-instellingen sluiten")}><X aria-hidden="true" /></button>
        </div>
        <div className="fab-detail-body">
          <p id="fab-drive-setup-description">{copy("Connect the approved intake folder through a user-owned Google consent flow. FAB stores the resulting token locally and never reads browser cookies.", "Koppel de goedgekeurde innamemap via een door de gebruiker beheerde Google-toestemming. FAB bewaart het resulterende token lokaal en leest nooit browsercookies.")}</p>

          <div className="fab-financial-warning" role="note">
            <AlertCircle aria-hidden="true" />
            <div>
              <strong>{copy("This Google token has broad Drive access", "Dit Google-token heeft brede Drive-toegang")}</strong>
              <span>{copy("Google grants access to all files in the connected account, not only the configured folder. FAB limits normal intake to that folder and moves a source only after verified Wave attachment readback with a matching SHA-256. For stronger isolation, connect a dedicated Google account that can access only the bookkeeping source and archive folders.", "Google verleent toegang tot alle bestanden in het gekoppelde account, niet alleen de ingestelde map. FAB beperkt de normale inname tot die map en verplaatst een bronbestand pas na geverifieerde teruglezing van de Wave-bijlage met overeenkomende SHA-256. Gebruik voor betere isolatie een apart Google-account dat alleen toegang heeft tot de boekhoudmappen voor bronbestanden en archief.")}</span>
            </div>
          </div>

          <div className="fab-drive-setup-progress">
            <SetupStep complete={folderConfigured} icon={Cloud} title={copy("Approved folder", "Goedgekeurde map")} detail={folderConfigured ? text(authorization.folderId, copy("Configured", "Ingesteld")) : copy("Folder configuration is missing.", "Mapconfiguratie ontbreekt.")} />
            <SetupStep complete={credentialsPresent} icon={FileKey2} title={copy("Desktop OAuth client", "Desktop-OAuth-client")} detail={credentialsPresent ? copy("Credential file installed", "Referentiebestand geïnstalleerd") : copy("Credential file required", "Referentiebestand vereist")} />
            <SetupStep complete={driveAuthorized} active={authorizationInProgress} icon={ShieldCheck} title={copy("Google consent", "Google-toestemming")} detail={authorizationInProgress ? copy("Complete consent in the browser window", "Voltooi toestemming in het browservenster") : reauthorizationRequired ? copy("Fresh consent required; reuse the installed OAuth client", "Nieuwe toestemming vereist; gebruik de geinstalleerde OAuth-client opnieuw") : tokenPresent ? copy("Local token present", "Lokaal token aanwezig") : copy("Authorization required", "Autorisatie vereist")} />
          </div>

          <section className="fab-drive-credential-panel">
            <div className="fab-subsection-heading"><div><span>{copy("Step 1", "Stap 1")}</span><h3>{credentialsPresent ? copy("OAuth client installed", "OAuth-client geinstalleerd") : copy("Install OAuth client", "OAuth-client installeren")}</h3></div></div>
            <p>{credentialsPresent
              ? copy("The installed client can be reused for fresh consent. Choose another JSON only when you intentionally rotate the OAuth client.", "De geinstalleerde client kan opnieuw worden gebruikt voor nieuwe toestemming. Kies alleen een andere JSON wanneer je de OAuth-client bewust roteert.")
              : copy("Create an OAuth client of type Desktop app in Google Cloud, download its JSON, then install it here.", "Maak in Google Cloud een OAuth-client van het type Desktop-app, download de JSON en installeer die hier.")}</p>
            <a className="fab-secondary-button compact" href="https://console.cloud.google.com/apis/credentials" target="_blank" rel="noreferrer"><ExternalLink aria-hidden="true" /> Google Cloud</a>
            <input ref={inputRef} className="sr-only" type="file" accept=".json,application/json" onChange={(event) => { selectFile(event.currentTarget.files?.[0] || null); event.currentTarget.value = ""; }} />
            <button className="fab-drive-file-picker" type="button" onClick={() => inputRef.current?.click()} disabled={!connected || busy || authorizationInProgress}>
              <UploadCloud aria-hidden="true" />
              <span><strong>{selectedFile ? selectedFile.name : copy("Choose desktop OAuth JSON", "Kies desktop-OAuth-JSON")}</strong><small>{selectedFile ? formatBytes(selectedFile.size) : copy("Maximum 64 KB", "Maximaal 64 KB")}</small></span>
            </button>
            {credentialsPresent && selectedFile && <label className="fab-review-checkbox"><input type="checkbox" checked={replace} onChange={(event) => setReplace(event.target.checked)} /><span>{copy("Replace the existing OAuth client credentials", "Vervang de bestaande OAuth-clientreferenties")}</span></label>}
            <button className="fab-primary-button" type="button" onClick={() => { void installCredentials(); }} disabled={!connected || busy || !selectedFile || authorizationInProgress || (credentialsPresent && !replace)}>{busy ? <Loader2 className="is-spinning" aria-hidden="true" /> : <FileKey2 aria-hidden="true" />} {credentialsPresent ? copy("Install replacement", "Vervanging installeren") : copy("Install credentials", "Referenties installeren")}</button>
          </section>

          <section className="fab-drive-credential-panel">
            <div className="fab-subsection-heading"><div><span>{copy("Step 2", "Stap 2")}</span><h3>{copy("Authorize the approved folder", "Autoriseer de goedgekeurde map")}</h3></div></div>
            <p>{copy("Starting authorization opens Google in your default browser. Sign in with the account that can access the configured source and archive folders.", "Autorisatie opent Google in je standaardbrowser. Meld je aan met een account dat toegang heeft tot de ingestelde bron- en archiefmappen.")}</p>
            <div className="fab-detail-actions">
              <button className="fab-primary-button" type="button" onClick={() => { void authorize(); }} disabled={!connected || busy || !credentialsPresent || !folderConfigured || authorizationInProgress}>{authorizationInProgress ? <Loader2 className="is-spinning" aria-hidden="true" /> : <ShieldCheck aria-hidden="true" />} {authorizationInProgress ? copy("Waiting for Google...", "Wachten op Google...") : reauthorizationRequired ? copy("Reauthorize Drive", "Drive opnieuw autoriseren") : tokenPresent ? copy("Verify Drive access", "Drive-toegang verifiëren") : copy("Authorize Drive", "Drive autoriseren")}</button>
              <button className="fab-secondary-button" type="button" onClick={() => { void onRefresh(); }} disabled={busy}><RefreshCw aria-hidden="true" /> {copy("Refresh status", "Status vernieuwen")}</button>
            </div>
          </section>

          <div className="fab-drive-safety-note"><ShieldCheck aria-hidden="true" /><div><strong>{copy("Archive protection remains enforced", "Archiefbeveiliging blijft actief")}</strong><span>{copy("A Drive file can move only after FAB verifies the Wave entry and downloads the exact attachment back with the same SHA-256 hash.", "Een Drive-bestand kan pas worden verplaatst nadat FAB de Wave-boeking verifieert en exact dezelfde bijlage terugdownloadt met dezelfde SHA-256-hash.")}</span></div></div>
          {!connected && <div className="fab-panel-state tone-bad" role="alert"><AlertCircle aria-hidden="true" /><div><strong>{copy("Local API disconnected", "Lokale API niet verbonden")}</strong><span>{copy("Drive setup requires the authoritative local FAB API.", "Drive-installatie vereist de gezaghebbende lokale FAB-API.")}</span></div></div>}
          {(error || text(authorization.error, "")) && <div className="fab-inline-error" role="alert">{error || text(authorization.error)}</div>}
        </div>
      </aside>
    </div>,
    document.body,
  );
}

type SetupStepProps = {
  complete: boolean;
  active?: boolean;
  icon: typeof Cloud;
  title: string;
  detail: string;
};

function SetupStep({ complete, active, icon: Icon, title, detail }: SetupStepProps) {
  return <div className={`fab-drive-setup-step tone-${complete ? "good" : active ? "info" : "neutral"}`}><span>{complete ? <CheckCircle2 aria-hidden="true" /> : active ? <Loader2 className="is-spinning" aria-hidden="true" /> : <Icon aria-hidden="true" />}</span><div><strong>{title}</strong><small>{detail}</small></div></div>;
}

function formatBytes(value: number): string {
  return value < 1024 ? `${value} B` : `${(value / 1024).toFixed(1)} KB`;
}
