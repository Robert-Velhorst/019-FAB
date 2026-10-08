import { useCallback, useEffect, useState } from "react";
import { AlertCircle, ChevronDown, LockKeyhole } from "lucide-react";
import { toast } from "sonner";
import { useAuth } from "@/_core/hooks/useAuth";
import { FabCommandDrawer } from "@/components/fab/FabCommandDrawer";
import { FabActivationChecklist } from "@/components/fab/FabActivationChecklist";
import { FabConnections } from "@/components/fab/FabConnections";
import { FabControlOverview } from "@/components/fab/FabControlOverview";
import { FabDeliveryQueue } from "@/components/fab/FabDeliveryQueue";
import { FabAutomationPanel } from "@/components/fab/FabAutomationPanel";
import { FabBackupCenter } from "@/components/fab/FabBackupCenter";
import { FabBankImportDrawer } from "@/components/fab/FabBankImportDrawer";
import { FabExceptionsPanel } from "@/components/fab/FabExceptionsPanel";
import { FabGmailSetupDrawer } from "@/components/fab/FabGmailSetupDrawer";
import { FabGoogleDriveSetupDrawer } from "@/components/fab/FabGoogleDriveSetupDrawer";
import { FabIntakeDrawer } from "@/components/fab/FabIntakeDrawer";
import { FabOperationsPanels } from "@/components/fab/FabOperationsPanels";
import { FabOperatorShell } from "@/components/fab/FabOperatorShell";
import { FabReportingCenter } from "@/components/fab/FabReportingCenter";
import { FabReviewWorkspace, type FabReviewResolution } from "@/components/fab/FabReviewWorkspace";
import { FabWaveReceiptExecutorDrawer } from "@/components/fab/FabWaveReceiptExecutorDrawer";
import { FabWaveSetupDrawer, type FabWaveSetupSaveInput } from "@/components/fab/FabWaveSetupDrawer";
import type { FabCommandId, FabRecord } from "@/components/fab/fabView";
import type { FabBankStatementFormat } from "@/components/fab/fabBankStatement";
import { asRecord, count, humanize, records, text } from "@/components/fab/fabView";
import { useFabLocale } from "@/components/fab/fabLocale";
import { trpc } from "@/lib/trpc";
import "@/components/fab/fab-operator.css";

type CommandPayload = {
  limit?: number;
  sources?: Array<"gmail" | "google_drive" | "freshdesk" | "google_photos">;
  dryRun?: boolean;
  fromDate?: string;
  toDate?: string;
  targetSystem?: string;
  reason?: string;
  confirmation?: string;
};

type CompletedCommand = {
  id: FabCommandId;
  status: string;
  startedAt: string | null;
  finishedAt: string;
  summary: string[];
};

export default function AdminOperations() {
  const { copy } = useFabLocale();
  const { user, loading: authLoading } = useAuth();
  const [search, setSearch] = useState("");
  const [commandDrawerOpen, setCommandDrawerOpen] = useState(false);
  const [intakeDrawerOpen, setIntakeDrawerOpen] = useState(false);
  const [bankImportOpen, setBankImportOpen] = useState(false);
  const [gmailSetupOpen, setGmailSetupOpen] = useState(false);
  const [driveSetupOpen, setDriveSetupOpen] = useState(false);
  const [waveSetupOpen, setWaveSetupOpen] = useState(false);
  const [waveReceiptExecutorOpen, setWaveReceiptExecutorOpen] = useState(false);
  const [pendingCommand, setPendingCommand] = useState<FabCommandId | null>(null);
  const [commandStartedAt, setCommandStartedAt] = useState<string | null>(null);
  const [lastCommand, setLastCommand] = useState<CompletedCommand | null>(null);
  const [uploading, setUploading] = useState(false);
  const [reviewWorkItems, setReviewWorkItems] = useState<FabRecord[]>([]);
  const [reviewPagination, setReviewPagination] = useState<FabRecord>({});
  const [loadingMoreReviews, setLoadingMoreReviews] = useState(false);
  const isAdmin = user?.role === "admin";
  const utils = trpc.useUtils();
  const operatorAccess = trpc.fab.access.useQuery(undefined, {
    retry: false,
    refetchOnWindowFocus: false,
  });
  const hasOperatorAccess = isAdmin || operatorAccess.data?.allowed === true;

  const controlCenter = trpc.fab.controlCenter.useQuery(undefined, {
    enabled: hasOperatorAccess,
    refetchInterval: 60_000,
    refetchOnWindowFocus: false,
  });
  const refreshControlCenter = trpc.fab.refreshControlCenter.useMutation({
    onSuccess: (result) => {
      utils.fab.controlCenter.setData(undefined, result);
    },
  });
  const refresh = useCallback(async () => {
    await refreshControlCenter.mutateAsync();
  }, [refreshControlCenter]);
  const runCommand = trpc.fab.runCommand.useMutation({
    onSuccess: async (result) => {
      const commandResult = asRecord(result.result);
      const status = text(result.status, text(commandResult.status, "completed"));
      if (pendingCommand) {
        setLastCommand({
          id: pendingCommand,
          status,
          startedAt: commandStartedAt,
          finishedAt: new Date().toISOString(),
          summary: summarizeCommandResult(commandResult),
        });
      }
      toast.success(`${pendingCommand ? humanize(pendingCommand) : "Command"}: ${humanize(status)}`);
      setPendingCommand(null);
      setCommandStartedAt(null);
      await controlCenter.refetch();
    },
    onError: (error) => {
      toast.error(error.message || copy("FAB command failed", "FAB-opdracht mislukt"));
      if (pendingCommand) {
        setLastCommand({
          id: pendingCommand,
          status: "failed",
          startedAt: commandStartedAt,
          finishedAt: new Date().toISOString(),
          summary: [],
        });
      }
      setPendingCommand(null);
      setCommandStartedAt(null);
    },
  });
  const createBackup = trpc.fab.createBackup.useMutation({
    onSuccess: async (result) => {
      const evidence = asRecord(asRecord(result.manifest).sourceEvidence);
      toast.success(copy(
        `Verified recovery package created for ${count(evidence.includedDocuments)} documents.`,
        `Geverifieerd herstelpakket gemaakt voor ${count(evidence.includedDocuments)} documenten.`,
      ));
      await controlCenter.refetch();
    },
    onError: (error) => {
      toast.error(error.message || copy(
        "Verified recovery package could not be created.",
        "Geverifieerd herstelpakket kon niet worden gemaakt.",
      ));
    },
  });
  const createSupportBundle = trpc.fab.createSupportBundle.useMutation({
    onSuccess: (result) => {
      toast.success(copy(
        `Sanitized support bundle created: ${text(result.bundleFilename)}`,
        `Opgeschoond supportpakket gemaakt: ${text(result.bundleFilename)}`,
      ));
    },
    onError: (error) => {
      toast.error(error.message || copy(
        "Support bundle could not be created.",
        "Supportpakket kon niet worden gemaakt.",
      ));
    },
  });
  const uploadIntake = trpc.fab.uploadIntake.useMutation();
  const importBankStatement = trpc.fab.importBankStatement.useMutation();
  const installGmailCredentials = trpc.fab.installGmailCredentials.useMutation();
  const startGmailAuthorization = trpc.fab.startGmailAuthorization.useMutation();
  const installGoogleDriveCredentials = trpc.fab.installGoogleDriveCredentials.useMutation();
  const startGoogleDriveAuthorization = trpc.fab.startGoogleDriveAuthorization.useMutation();
  const saveWaveSetup = trpc.fab.saveWaveSetup.useMutation();
  const validateWaveSetup = trpc.fab.validateWaveSetup.useMutation();
  const resolveReview = trpc.fab.resolveReview.useMutation();

  const executeCommand = useCallback((commandId: FabCommandId, payload: FabRecord = {}) => {
    if (!controlCenter.data?.connection.connected || pendingCommand) return;
    setPendingCommand(commandId);
    setCommandStartedAt(new Date().toISOString());
    runCommand.mutate({ commandId, payload: payload as CommandPayload });
  }, [controlCenter.data?.connection.connected, pendingCommand, runCommand]);

  const uploadDocument = useCallback(async (file: File): Promise<FabRecord> => {
    if (!controlCenter.data?.connection.connected) throw new Error(copy("FAB local API is disconnected.", "De lokale FAB-API is niet verbonden."));
    return uploadIntake.mutateAsync({
      filename: file.name,
      mimeType: file.type || "application/octet-stream",
      contentBase64: await readFileBase64(file),
    });
  }, [controlCenter.data?.connection.connected, uploadIntake]);

  const finishIntake = useCallback(async (uploadedCount: number) => {
    toast.success(`${uploadedCount} ${copy(uploadedCount === 1 ? "document added to FAB intake." : "documents added to FAB intake.", uploadedCount === 1 ? "document toegevoegd aan FAB-inname." : "documenten toegevoegd aan FAB-inname.")}`);
    await controlCenter.refetch();
    executeCommand("process_imported");
  }, [controlCenter, executeCommand]);

  const uploadBankStatement = useCallback(async (
    file: File,
    format: FabBankStatementFormat,
    accountIdentifier: string,
  ): Promise<FabRecord> => {
    if (!controlCenter.data?.connection.connected) {
      throw new Error(copy("FAB local API is disconnected.", "De lokale FAB-API is niet verbonden."));
    }
    return importBankStatement.mutateAsync({
      filename: file.name,
      format,
      accountIdentifier,
      contentBase64: await readFileBase64(file),
    });
  }, [controlCenter.data?.connection.connected, copy, importBankStatement]);

  const finishBankImport = useCallback(async (result: FabRecord) => {
    const imported = count(result.rowsImported);
    const duplicates = count(result.duplicates);
    const identityConflicts = count(result.identityConflicts);
    const skipped = count(result.skipped);
    const message = identityConflicts > 0
      ? copy(
        `Import needs review: ${identityConflicts} changed transaction identity conflict(s). Existing ledger rows were preserved.`,
        `Import vereist controle: ${identityConflicts} transacties met gewijzigde identiteitsgegevens. Bestaande grootboekregels zijn behouden.`,
      )
      : copy(
        `Bank statement recorded: ${imported} new, ${duplicates} already present, ${skipped} skipped.`,
        `Bankafschrift vastgelegd: ${imported} nieuw, ${duplicates} reeds aanwezig, ${skipped} overgeslagen.`,
      );
    if (identityConflicts > 0) toast.warning(message);
    else toast.success(message);
    await controlCenter.refetch();
    if (imported > 0) executeCommand("run_reconciliation");
  }, [controlCenter, copy, executeCommand]);

  const installDriveCredentials = useCallback(async (file: File, replace: boolean) => {
    await installGoogleDriveCredentials.mutateAsync({
      filename: file.name,
      contentBase64: await readFileBase64(file),
      replace,
    });
    toast.success(copy("Google OAuth client installed locally.", "Google OAuth-client lokaal geïnstalleerd."));
    await controlCenter.refetch();
  }, [controlCenter, copy, installGoogleDriveCredentials]);

  const installScannerCredentials = useCallback(async (file: File, replace: boolean) => {
    await installGmailCredentials.mutateAsync({
      filename: file.name,
      contentBase64: await readFileBase64(file),
      replace,
    });
    toast.success(copy("Gmail OAuth client installed locally.", "Gmail OAuth-client lokaal geinstalleerd."));
    await controlCenter.refetch();
  }, [controlCenter, copy, installGmailCredentials]);

  const authorizeScanner = useCallback(async () => {
    await startGmailAuthorization.mutateAsync();
    toast.info(copy("Read-only Gmail authorization opened in your default browser.", "Alleen-lezen Gmail-autorisatie is geopend in je standaardbrowser."));
    await controlCenter.refetch();
  }, [controlCenter, copy, startGmailAuthorization]);

  const authorizeDrive = useCallback(async () => {
    await startGoogleDriveAuthorization.mutateAsync();
    toast.info(copy("Google authorization opened in your default browser.", "Google-autorisatie is geopend in je standaardbrowser."));
    await controlCenter.refetch();
  }, [controlCenter, copy, startGoogleDriveAuthorization]);

  const saveWaveConnection = useCallback(async (input: FabWaveSetupSaveInput) => {
    const result = await saveWaveSetup.mutateAsync(input);
    toast.success(result.ready === true
      ? copy("Wave is connected and mapped.", "Wave is gekoppeld en toegewezen.")
      : copy("Wave setup was saved locally.", "Wave-instellingen zijn lokaal opgeslagen."));
    await controlCenter.refetch();
  }, [controlCenter, copy, saveWaveSetup]);

  const validateWaveConnection = useCallback(async () => {
    const result = await validateWaveSetup.mutateAsync({ targetSystem: "waveapps_business" });
    const discovery = result.discovery && typeof result.discovery === "object" ? result.discovery as FabRecord : {};
    const accounts = Array.isArray(discovery.accounts) ? discovery.accounts.length : 0;
    toast.success(`${copy("Wave business validated", "Wave-bedrijf gevalideerd")}: ${accounts} ${copy("accounts", "rekeningen")}`);
    await controlCenter.refetch();
  }, [controlCenter, copy, validateWaveSetup]);

  const resolveReviewItem = useCallback(async (input: FabReviewResolution) => {
    try {
      const result = await resolveReview.mutateAsync(input);
      const batch = asRecord(result.batchPropagation);
      const propagated = count(batch.appliedDocuments);
      const remainingDuplicateCandidates = Array.isArray(result.remainingDuplicateCandidateIds)
        ? result.remainingDuplicateCandidateIds.length
        : 0;
      toast.success(text(result.status, "") === "candidate_rejected"
        ? copy(
          `Candidate rejected; ${remainingDuplicateCandidates} duplicate match${remainingDuplicateCandidates === 1 ? "" : "es"} still require review.`,
          `Kandidaat afgewezen; ${remainingDuplicateCandidates} duplicaatmatch${remainingDuplicateCandidates === 1 ? "" : "es"} moet${remainingDuplicateCandidates === 1 ? "" : "en"} nog worden gecontroleerd.`,
        )
        : propagated > 0
        ? copy(
          `Verified details saved and category applied to ${propagated} other exact vendor match${propagated === 1 ? "" : "es"}.`,
          `Geverifieerde gegevens opgeslagen en categorie toegepast op ${propagated} andere exacte leveranciersmatch${propagated === 1 ? "" : "es"}.`,
        )
        : `${copy("Review updated", "Controle bijgewerkt")}: ${humanize(text(result.processingStatus, text(result.status)))}`);
      await controlCenter.refetch();
    } catch (error) {
      const message = error instanceof Error ? error.message : copy("Review update failed", "Bijwerken van controle mislukt");
      toast.error(message);
      throw error;
    }
  }, [controlCenter, copy, resolveReview]);

  const operatorLabel = user?.name || user?.email || operatorAccess.data?.operatorLabel || "Operator";
  const data = controlCenter.data;
  const connected = Boolean(data?.connection.connected);

  useEffect(() => {
    setReviewWorkItems(data?.reviews.workItems || []);
    setReviewPagination(data?.reviews.pagination || {});
  }, [data?.connection.checkedAt, data?.reviews.pagination, data?.reviews.workItems]);

  const loadMoreReviews = useCallback(async () => {
    if (loadingMoreReviews || reviewPagination.hasMore !== true) return;
    const nextOffset = count(reviewPagination.nextOffset);
    const limit = Math.max(1, count(reviewPagination.limit) || 50);
    setLoadingMoreReviews(true);
    try {
      const page = await utils.fab.reviewPage.fetch({ offset: nextOffset, limit });
      const nextItems = records(page.workItems);
      setReviewWorkItems((current) => {
        const merged = new Map(current.map((item) => [text(item.id), item]));
        nextItems.forEach((item) => merged.set(text(item.id), item));
        return Array.from(merged.values());
      });
      setReviewPagination(asRecord(page.pagination));
    } catch (error) {
      toast.error(error instanceof Error
        ? error.message
        : copy("More reviews could not be loaded.", "Meer controles konden niet worden geladen."));
    } finally {
      setLoadingMoreReviews(false);
    }
  }, [copy, loadingMoreReviews, reviewPagination, utils.fab.reviewPage]);

  useEffect(() => {
    document.documentElement.classList.add("fab-operator-active");
    return () => document.documentElement.classList.remove("fab-operator-active");
  }, []);

  return (
    <FabOperatorShell
      connected={connected}
      connectionStatus={authLoading ? copy("Checking access", "Toegang controleren") : text(data?.connection.status, copy("Local API offline", "Lokale API offline"))}
      organization="FAB Local Ledger"
      operatorLabel={operatorLabel}
      managedOperator={user?.loginMethod === "fab-operator-secret"}
      search={search}
      onSearchChange={setSearch}
      onRefresh={() => { void refresh(); }}
      refreshing={controlCenter.isFetching || refreshControlCenter.isPending}
      onOpenCommands={() => setCommandDrawerOpen(true)}
      reviewCount={data?.metrics.pendingReviewDocuments}
    >
      {!authLoading && !operatorAccess.isLoading && !hasOperatorAccess ? (
        <div className="fab-access-state">
          <LockKeyhole aria-hidden="true" />
          <h1>{copy("Operator access required", "Operatortoegang vereist")}</h1>
          <p>{copy("Sign in with an administrator account to operate the authoritative FAB ledger.", "Log in met een beheerdersaccount om het gezaghebbende FAB-grootboek te bedienen.")}</p>
          <a className="fab-primary-button" href="/operator/login">{copy("Sign in", "Inloggen")}</a>
        </div>
      ) : controlCenter.isLoading || authLoading || operatorAccess.isLoading ? (
        <FabLoadingState />
      ) : (
        <>
          {!connected && (
            <div className="fab-system-banner tone-bad">
              <AlertCircle aria-hidden="true" />
              <div><strong>{copy("FAB local API is disconnected", "De lokale FAB-API is niet verbonden")}</strong><span>{text(data?.connection.error, copy("Start the local FAB API and verify the server-side URL and token.", "Start de lokale FAB-API en controleer de server-URL en het token."))}</span></div>
              <button className="fab-secondary-button compact" onClick={() => { void refresh(); }}>{copy("Retry", "Opnieuw proberen")}</button>
            </div>
          )}
          {Boolean(data?.partialErrors.length) && connected && (
            <details className="fab-system-details tone-warn">
              <summary><AlertCircle aria-hidden="true" /><span><strong>{copy("Some control-center resources are retained or unavailable", "Sommige bronnen zijn bewaard of niet beschikbaar")}</strong><small>{copy("Inspect", "Bekijk")} {data?.partialErrors.length} {copy(data?.partialErrors.length === 1 ? "technical detail" : "technical details", data?.partialErrors.length === 1 ? "technisch detail" : "technische details")}</small></span><ChevronDown aria-hidden="true" /></summary>
              <div>{data?.partialErrors.map((item) => <p key={item.resource}><strong>{humanize(item.resource)} - {humanize(item.state)}</strong><span>{item.error}{item.updatedAt ? ` Last valid response: ${item.updatedAt}.` : ""}</span></p>)}</div>
            </details>
          )}
          <FabControlOverview
            connected={connected}
            metrics={data?.metrics || { documents: null, pendingReview: null, pendingReviewDocuments: null, unreconciled: null, unreconciledDocuments: null, unreconciledBankTransactions: null, exceptions: null, failedDocuments: null }}
            health={data?.health || {}}
            autonomy={data?.autonomy || {}}
            closeReadiness={data?.closeReadiness || {}}
            metricResource={data?.resourceStates.metrics}
            healthResource={data?.resourceStates.health}
            exceptionResource={data?.resourceStates.exceptions}
            closeResource={data?.resourceStates.closeReadiness}
            decisionContext={data?.decisionContext || {
              lastSafeCycleAt: null,
              latestWorkflowStatus: null,
              dataThroughDate: null,
              sourceCount: null,
              readySourceCount: null,
              latestSourceSyncAt: null,
              unreconciledAmountByCurrency: null,
              oldestReviewAgeHours: null,
              highPriorityExceptions: null,
              ledgerReadyForApproval: null,
            }}
            workflowResource={data?.resourceStates.workflows}
            sourceResource={data?.resourceStates.sources}
            ledgerResource={data?.resourceStates.masterLedger}
            bankResource={data?.resourceStates.bankTransactions}
            reviewResource={data?.resourceStates.reviewQueue}
            checkedAt={data?.connection.checkedAt}
            latencyMs={data?.connection.latencyMs}
            commandPending={Boolean(pendingCommand) || uploading || importBankStatement.isPending}
            pendingCommand={pendingCommand}
            uploading={uploading}
            bankImporting={importBankStatement.isPending}
            onCommand={executeCommand}
            onOpenIntake={() => setIntakeDrawerOpen(true)}
            onOpenBankImport={() => setBankImportOpen(true)}
            onOpenCommands={() => setCommandDrawerOpen(true)}
          />
          <div className="fab-priority-grid">
            <FabExceptionsPanel
              exceptions={data?.exceptions || []}
              exceptionSummary={data?.exceptionSummary || {}}
              resource={data?.resourceStates.exceptions}
              closeReadiness={data?.closeReadiness || {}}
              closeResource={data?.resourceStates.closeReadiness}
              search={search}
              onOpenReview={() => document.getElementById("review-workspace")?.scrollIntoView({ behavior: "smooth", block: "start" })}
            />
            <FabAutomationPanel
              autonomy={data?.autonomy || {}}
              workflows={data?.workflows || []}
              autonomyResource={data?.resourceStates.autonomy}
              workflowResource={data?.resourceStates.workflows}
              pendingCommand={pendingCommand}
              connected={connected}
              onCommand={executeCommand}
            />
          </div>
          {connected && <FabActivationChecklist
            waveSetup={data?.waveSetup || {}}
            gmailAuthorization={data?.gmailAuthorization || {}}
            driveAuthorization={data?.driveAuthorization || {}}
            waveReceiptExecutor={data?.waveReceiptExecutor || {}}
            reviewSummary={data?.reviews.summary || {}}
            onOpenWave={() => setWaveSetupOpen(true)}
            onOpenGmail={() => setGmailSetupOpen(true)}
            onOpenDrive={() => setDriveSetupOpen(true)}
            onOpenReceiptExecutor={() => setWaveReceiptExecutorOpen(true)}
            onOpenReviews={() => document.getElementById("review-workspace")?.scrollIntoView({ behavior: "smooth", block: "start" })}
          />}
          <FabReviewWorkspace
            workItems={reviewWorkItems}
            categoryOptions={data?.reviews.categoryOptions || []}
            summary={data?.reviews.summary || {}}
            pagination={reviewPagination}
            resource={data?.resourceStates.reviewQueue}
            search={search}
            resolvingReviewId={resolveReview.isPending ? resolveReview.variables?.reviewItemId || null : null}
            onResolve={resolveReviewItem}
            loadingMore={loadingMoreReviews}
            onLoadMore={loadMoreReviews}
          />
          <FabOperationsPanels
            recovery={data?.recovery || {}}
            activity={data?.activity || []}
            workflows={data?.workflows || []}
            recoveryResource={data?.resourceStates.recovery}
            activityResource={data?.resourceStates.activity}
            workflowResource={data?.resourceStates.workflows}
            search={search}
          />
          <FabReportingCenter
            reporting={data?.reporting || { scheduleStatus: {}, reportRuns: [], externalSubmission: null }}
            compliance={data?.compliance || { summary: {}, assessments: [], statutoryStatus: null, filingStatus: null, externalFiling: null }}
            reportingResource={data?.resourceStates.reportRuns}
            complianceResource={data?.resourceStates.compliance}
            connected={connected}
            pendingCommand={pendingCommand}
            onCommand={executeCommand}
          />
          <FabBackupCenter
            backups={data?.backups || { backups: [], schedule: {}, restorePolicy: {}, verificationMode: null }}
            resource={data?.resourceStates.backups}
            connected={connected}
            pending={createBackup.isPending}
            supportPending={createSupportBundle.isPending}
            onCreate={() => createBackup.mutate()}
            onCreateSupportBundle={() => createSupportBundle.mutate()}
          />
          <FabDeliveryQueue
            delivery={data?.delivery || { status: {}, summary: {}, workOrders: [], count: null }}
            resource={data?.resourceStates.driveWaveWorkOrders}
            search={search}
          />
          <FabConnections
            connections={data?.connections || []}
            search={search}
            commandPending={Boolean(pendingCommand) || importBankStatement.isPending}
            resource={data?.resourceStates.settings}
            onCommand={executeCommand}
            onSetupConnection={(connectionId) => {
              if (connectionId === "gmail") setGmailSetupOpen(true);
              if (connectionId === "google_drive") setDriveSetupOpen(true);
              if (connectionId === "waveapps_business") setWaveSetupOpen(true);
              if (connectionId === "wave_receipt_executor") setWaveReceiptExecutorOpen(true);
              if (connectionId === "banking_api") setBankImportOpen(true);
            }}
          />
        </>
      )}
      <FabCommandDrawer
        open={commandDrawerOpen}
        connected={connected}
        pendingCommand={pendingCommand}
        commandStartedAt={commandStartedAt}
        lastCommand={lastCommand}
        onClose={() => setCommandDrawerOpen(false)}
        onCommand={executeCommand}
      />
      <FabIntakeDrawer
        open={intakeDrawerOpen}
        connected={connected}
        onClose={() => setIntakeDrawerOpen(false)}
        onUploadFile={uploadDocument}
        onFinished={finishIntake}
        onBusyChange={setUploading}
      />
      <FabBankImportDrawer
        open={bankImportOpen}
        connected={connected}
        busy={importBankStatement.isPending}
        recentImports={data?.banking?.recentImports || []}
        onClose={() => setBankImportOpen(false)}
        onImport={uploadBankStatement}
        onFinished={finishBankImport}
      />
      <FabGmailSetupDrawer
        open={gmailSetupOpen}
        connected={connected}
        authorization={data?.gmailAuthorization || {}}
        busy={installGmailCredentials.isPending || startGmailAuthorization.isPending}
        onClose={() => setGmailSetupOpen(false)}
        onInstallCredentials={installScannerCredentials}
        onStartAuthorization={authorizeScanner}
        onRefresh={refresh}
      />
      <FabGoogleDriveSetupDrawer
        open={driveSetupOpen}
        connected={connected}
        authorization={data?.driveAuthorization || {}}
        busy={installGoogleDriveCredentials.isPending || startGoogleDriveAuthorization.isPending}
        onClose={() => setDriveSetupOpen(false)}
        onInstallCredentials={installDriveCredentials}
        onStartAuthorization={authorizeDrive}
        onRefresh={refresh}
      />
      <FabWaveSetupDrawer
        open={waveSetupOpen}
        connected={connected}
        setup={data?.waveSetup || {}}
        busy={saveWaveSetup.isPending || validateWaveSetup.isPending}
        onClose={() => setWaveSetupOpen(false)}
        onSave={saveWaveConnection}
        onValidate={validateWaveConnection}
        onRefresh={refresh}
      />
      <FabWaveReceiptExecutorDrawer
        open={waveReceiptExecutorOpen}
        connected={connected}
        executor={data?.waveReceiptExecutor || {}}
        localApiEndpoint={data?.connection.endpoint || "http://127.0.0.1:5001"}
        onClose={() => setWaveReceiptExecutorOpen(false)}
        onRefresh={refresh}
      />
    </FabOperatorShell>
  );
}

function readFileBase64(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onerror = () => reject(new Error(`Could not read ${file.name}`));
    reader.onload = () => {
      const result = typeof reader.result === "string" ? reader.result : "";
      const separator = result.indexOf(",");
      if (separator < 0) {
        reject(new Error(`Could not encode ${file.name}`));
        return;
      }
      resolve(result.slice(separator + 1));
    };
    reader.readAsDataURL(file);
  });
}

function summarizeCommandResult(result: FabRecord): string[] {
  const summaries: string[] = [];
  const fields: Array<[string, string]> = [
    ["documentsProcessed", "documents processed"],
    ["documents_processed", "documents processed"],
    ["documentsNeedingReview", "documents needing review"],
    ["documents_needing_review", "documents needing review"],
    ["executed", "steps executed"],
    ["skipped", "steps skipped"],
    ["failed", "steps failed"],
    ["reconciled", "matches reconciled"],
    ["created", "records created"],
  ];
  const seen = new Set<string>();
  for (const [field, label] of fields) {
    const value = result[field];
    if ((typeof value !== "number" && typeof value !== "string") || seen.has(label)) continue;
    const parsed = Number(value);
    if (!Number.isFinite(parsed)) continue;
    summaries.push(`${parsed} ${label}`);
    seen.add(label);
  }
  const workflowId = text(result.workflowRunId || result.workflow_run_id || result.workflowId, "");
  if (workflowId) summaries.unshift(`Workflow #${workflowId}`);
  return summaries.slice(0, 4);
}

function FabLoadingState() {
  return (
    <div className="fab-loading-state" aria-label="Loading FAB control center">
      <div className="fab-loading-heading"><span /><span /></div>
      <div className="fab-loading-metrics">{Array.from({ length: 4 }, (_, index) => <span key={index} />)}</div>
      <div className="fab-loading-panel" />
      <div className="fab-loading-panel short" />
    </div>
  );
}
