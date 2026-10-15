import {
  ArrowUpRight,
  Bot,
  Building2,
  CheckCircle2,
  CircleDollarSign,
  Cloud,
  Database,
  HardDrive,
  Inbox,
  Landmark,
  Mail,
  MonitorUp,
  RefreshCw,
  ScanText,
  Settings2,
  Unplug,
  UsersRound,
} from "lucide-react";
import { FabDataStatus, FabPanelStateMessage } from "./FabDataState";
import { useFabLocale } from "./fabLocale";
import { fabOperatorLink } from "./fabOperatorLink";
import { compactHumanize, matchesSearch, panelState, statusTone, text, timeAgo, type FabCommandId, type FabRecord, type FabResourceState } from "./fabView";

const connectorIcons: Record<string, typeof Cloud> = {
  local_folder: HardDrive,
  gmail: Mail,
  google_drive: Cloud,
  google_photos: Cloud,
  freshdesk: Inbox,
  tesseract_ocr: ScanText,
  waveapps_business: Building2,
  waveapps_personal: UsersRound,
  wave_receipt_executor: MonitorUp,
  mijngeldzaken: Landmark,
  banking_api: CircleDollarSign,
  hai: Bot,
  ngrok_cloud: Cloud,
};

type FabConnectionsProps = {
  connections: FabRecord[];
  search: string;
  commandPending: boolean;
  resource?: FabResourceState;
  onCommand: (commandId: FabCommandId, payload?: FabRecord) => void;
  onSetupConnection: (connectionId: string) => void;
};

export function FabConnections({ connections, search, commandPending, resource, onCommand, onSetupConnection }: FabConnectionsProps) {
  const { copy, status: localizedStatus, dateLocale } = useFabLocale();
  const visibleConnections = connections.filter((item) => matchesSearch(item, search));
  const syncableConnections = connections.filter((item) => item.canSync === true);
  const activeConnections = connections.filter((item) => !["disabled", "not_configured"].includes(text(item.status)));
  const readyCount = activeConnections.filter((item) => item.ready === true || text(item.status) === "ready").length;
  const setupCount = activeConnections.length - readyCount;
  const state = panelState(resource, connections.length);

  return (
    <section id="connections" className="fab-section fab-connections-section">
      <div className="fab-section-heading">
        <div><span>{copy("Source and downstream controls", "Bron- en vervolgkoppelingen")}</span><h2>{copy("Connections", "Koppelingen")}</h2></div>
        <div className="fab-connection-heading-actions">
          <FabDataStatus resource={resource} state={state} />
          <span className={`fab-status-chip tone-${setupCount === 0 ? "good" : "warn"}`}>{resource?.state === "live" || resource?.state === "stale" ? `${readyCount} ${copy("ready", "gereed")}${setupCount ? ` / ${setupCount} ${copy("setup", "instellen")}` : ""}` : `- ${copy("ready", "gereed")}`}</span>
          {syncableConnections.length ? (
            <button className="fab-secondary-button compact" onClick={() => onCommand("sync_sources")} disabled={commandPending}>
              <RefreshCw aria-hidden="true" /> {copy("Sync sources", "Bronnen synchroniseren")}
            </button>
          ) : (
            <a className="fab-secondary-button compact" href={fabOperatorLink("/#settings")} target="_blank" rel="noreferrer">
              <Settings2 aria-hidden="true" /> {copy("Review setup", "Instellingen bekijken")}
            </a>
          )}
        </div>
      </div>
      <div className="fab-connection-list">
        {resource?.state === "stale" && <FabPanelStateMessage resource={resource} title={copy("Connections", "Koppelingen")} />}
        {(resource?.state === "live" || resource?.state === "stale") && visibleConnections.map((connection) => {
          const id = text(connection.id, "unknown");
          const Icon = connectorIcons[id] || Database;
          const status = text(connection.status, connection.ready ? "ready" : connection.configured ? "attention" : "not_configured");
          const canSync = connection.canSync === true && ["gmail", "google_drive", "google_photos", "freshdesk"].includes(id);
          const ready = connection.ready === true || status === "ready";
          const setupTarget = connectionSetupTarget(id);
          const connectorProfile = connection.connectorProfile && typeof connection.connectorProfile === "object"
            ? connection.connectorProfile as FabRecord
            : null;
          const cloudDashboardUrl = id === "ngrok_cloud" ? cleanHttpsOrigin(connection.dashboardUrl) : null;
          const connectionDetails = id === "freshdesk" && connection.enabled === true && connectorProfile?.enabled === true
            ? copy(
                "Read-only financial tickets, verified PDF evidence, and unchanged source tickets.",
                "Alleen-lezen financiele tickets, geverifieerd PDF-bewijs en ongewijzigde brontickets.",
              )
            : text(ready ? connection.details : connection.nextAction || connection.details, "No connector details recorded.");
          return (
            <div className="fab-connection-row" key={id}>
              <div className={`fab-connection-icon tone-${statusTone(status)}`}><Icon aria-hidden="true" /></div>
              <div className="fab-connection-name"><strong>{text(connection.label, compactHumanize(id))}</strong><span>{connectionDetails}</span></div>
              <div className="fab-connection-last"><span>{copy("Last ledger signal", "Laatste grootboeksignaal")}</span><strong>{timeAgo(connection.lastSyncAt, dateLocale)}</strong></div>
              <span className={`fab-status-chip tone-${statusTone(status)}`}>{localizedStatus(status)}</span>
              {canSync ? (
                <button className="fab-icon-button" onClick={() => onCommand("sync_sources", { sources: [id] })} disabled={commandPending} aria-label={`Sync ${text(connection.label, id)}`} title={`Sync ${text(connection.label, id)}`}><RefreshCw aria-hidden="true" /></button>
              ) : ["gmail", "google_drive", "waveapps_business", "wave_receipt_executor", "banking_api"].includes(id) ? (
                <button className="fab-icon-button" type="button" onClick={() => onSetupConnection(id)} disabled={commandPending} aria-label={copy(`Set up ${text(connection.label, compactHumanize(id))}`, `${text(connection.label, compactHumanize(id))} instellen`)} title={copy(`Set up ${text(connection.label, compactHumanize(id))}`, `${text(connection.label, compactHumanize(id))} instellen`)}><Settings2 aria-hidden="true" /></button>
              ) : ready && ["local_folder", "tesseract_ocr"].includes(id) ? (
                <span className="fab-connection-static tone-good" title="This local capability is ready"><CheckCircle2 aria-hidden="true" /></span>
              ) : cloudDashboardUrl ? (
                <a className="fab-icon-button" href={cloudDashboardUrl} target="_blank" rel="noreferrer" aria-label={copy("Open remote FAB dashboard", "FAB-dashboard op afstand openen")} title={copy("Open remote FAB dashboard", "FAB-dashboard op afstand openen")}>
                  <ArrowUpRight aria-hidden="true" />
                </a>
              ) : (
                <a className="fab-icon-button" href={setupTarget} target="_blank" rel="noreferrer" aria-label={`Review ${text(connection.label, id)} setup`} title={`Review ${text(connection.label, id)} setup`}>
                  {ready ? <ArrowUpRight aria-hidden="true" /> : <Settings2 aria-hidden="true" />}
                </a>
              )}
            </div>
          );
        })}
        {resource?.state !== "live" && resource?.state !== "stale" && <FabPanelStateMessage resource={resource} title={copy("Connections", "Koppelingen")} />}
        {resource?.state === "live" && !visibleConnections.length && <div className="fab-empty-state"><Unplug aria-hidden="true" /><strong>{connections.length ? copy("No matching connections", "Geen overeenkomende koppelingen") : copy("No connections returned", "Geen koppelingen teruggegeven")}</strong><span>{connections.length ? copy("Adjust the active search.", "Pas de zoekopdracht aan.") : copy("The live settings service returned no configured connection records.", "De actuele instellingenservice gaf geen ingestelde koppelingen terug.")}</span></div>}
      </div>
    </section>
  );
}

function cleanHttpsOrigin(value: unknown): string | null {
  if (typeof value !== "string") return null;
  try {
    const url = new URL(value);
    if (url.protocol !== "https:" || url.username || url.password || url.pathname !== "/" || url.search || url.hash) {
      return null;
    }
    return url.origin;
  } catch {
    return null;
  }
}

function connectionSetupTarget(id: string): string {
  const anchor = ["gmail", "google_drive", "google_photos", "freshdesk"].includes(id)
    ? "sources"
    : id === "wave_receipt_executor"
      ? "api/wave/receipt-executor/status"
    : id === "mijngeldzaken"
      ? "mijngeldzaken"
      : id.startsWith("waveapps_")
        ? "wave"
        : id === "banking_api"
          ? "reconciliation"
          : id === "hai"
            ? "api/hai/manifest"
            : id === "ngrok_cloud"
              ? "api/cloud/status"
            : "settings";
  return fabOperatorLink(anchor.startsWith("api/") ? `/${anchor}` : `/#${anchor}`);
}
