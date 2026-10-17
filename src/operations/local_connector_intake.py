import importlib
import os
import re
import time
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Iterable, Optional
from uuid import uuid4

from src.document_fetchers.profiles import (
    CUSTOM_SCANNER_PROFILE_ID,
    HP_EPRINT_PROFILE_ID,
    HP_EPRINT_SENDER,
    LEGACY_025_COMMIT,
    LEGACY_025_FINANCIAL_KEYWORDS,
    LEGACY_025_PROFILE_ID,
    LEGACY_025_REPOSITORY,
)
from src.operations.local_bookkeeping_records import LocalBookkeepingRecordService
from src.operations.local_intake import LocalFolderIntake
from src.operations.local_ledger import LocalOperationsLedger
from src.operations.local_targets import resolve_document_target_system
from src.operations.runtime_lease import RuntimeLeaseHeartbeat
from src.security.google_oauth_store import GoogleOAuthTokenStore


CONNECTOR_SOURCES = ("gmail", "google_drive", "freshdesk", "google_photos")
CONNECTOR_INTAKE_LEASE_NAME = "local_connector_intake"
SOURCE_ALIASES = {
    "drive": "google_drive",
    "photos": "google_photos",
}
CONNECTOR_TARGET_SYSTEMS = {"waveapps_business", "waveapps_personal", "mijngeldzaken"}


def _default_fetcher_factories() -> Dict[str, Callable[[Dict[str, Any]], Any]]:
    return {
        "gmail": _gmail_fetcher,
        "google_drive": _drive_fetcher,
        "freshdesk": _freshdesk_fetcher,
    }


def _gmail_fetcher(config: Dict[str, Any]):
    fetcher_type = getattr(
        importlib.import_module("src.document_fetchers.gmail_fetcher"),
        "GmailFetcher",
    )
    return fetcher_type(config)


def _drive_fetcher(config: Dict[str, Any]):
    fetcher_type = getattr(
        importlib.import_module("src.document_fetchers.drive_fetcher"),
        "DriveFetcher",
    )
    return fetcher_type(config)


def _freshdesk_fetcher(config: Dict[str, Any]):
    fetcher_type = getattr(
        importlib.import_module("src.document_fetchers.freshdesk_fetcher"),
        "FreshdeskFetcher",
    )
    return fetcher_type(config)


class LocalConnectorIntakeService:
    """Sync configured read-only document connectors into FAB's local ledger."""

    def __init__(
        self,
        ledger: LocalOperationsLedger,
        config: Optional[Dict[str, Any]] = None,
        fetcher_factories: Optional[Dict[str, Callable[[Dict[str, Any]], Any]]] = None,
    ):
        self.ledger = ledger
        self.config = config or {}
        self.fetcher_factories = fetcher_factories or _default_fetcher_factories()

    def plan(self) -> Dict[str, Any]:
        sources = [self._source_plan(source) for source in CONNECTOR_SOURCES]
        return {
            "sources": sources,
            "enabledSources": [item["source"] for item in sources if item["enabled"]],
            "syncableSources": [item["source"] for item in sources if item["canSync"]],
            "canSync": any(item["canSync"] for item in sources),
            "externalSubmission": "not_executed",
        }

    def sync(
        self,
        sources: Optional[Iterable[str]] = None,
        actor: str = "local_connector_intake",
        trigger_source: str = "connector_intake",
        workflow_metadata: Optional[Dict[str, Any]] = None,
        step_attempts: Optional[Dict[str, int]] = None,
    ) -> Dict[str, Any]:
        owner_token = uuid4().hex
        lease_ttl_seconds = _positive_float_config(
            self.config,
            "fab_connector_intake_lease_seconds",
            "operations_connector_intake_lease_seconds",
            "connector_intake_lease_seconds",
            default=21600.0,
        )
        lease = self.ledger.acquire_runtime_lease(
            CONNECTOR_INTAKE_LEASE_NAME,
            owner_token,
            ttl_seconds=lease_ttl_seconds,
            metadata={"actor": actor, "trigger": trigger_source},
        )
        if not lease.get("acquired"):
            self.ledger.record_audit_event({
                "action": "local_connector_intake.sync_skipped_already_running",
                "entityType": "runtime_lease",
                "entityId": CONNECTOR_INTAKE_LEASE_NAME,
                "details": {
                    "actor": actor,
                    "triggerSource": trigger_source,
                    "lease": lease.get("lease"),
                    "externalSubmission": "not_executed",
                },
            })
            return {
                "success": False,
                "status": "already_running",
                "workflowRunId": None,
                "triggerSource": trigger_source,
                "results": [],
                "summary": _empty_summary(),
                "runtimeLease": lease.get("lease"),
                "externalSubmission": "not_executed",
            }

        result = None
        try:
            with RuntimeLeaseHeartbeat(
                self.ledger,
                CONNECTOR_INTAKE_LEASE_NAME,
                owner_token,
                lease_ttl_seconds,
                thread_name="fab-connector-intake-lease-heartbeat",
            ):
                result = self._sync_once(
                    sources=sources,
                    actor=actor,
                    trigger_source=trigger_source,
                    workflow_metadata=workflow_metadata,
                    step_attempts=step_attempts,
                )
                return result
        finally:
            released = self.ledger.release_runtime_lease(
                CONNECTOR_INTAKE_LEASE_NAME,
                owner_token,
            )
            released_lease = {
                **(lease.get("lease") or {}),
                "active": False if released else (lease.get("lease") or {}).get("active"),
                "released": released,
            }
            if isinstance(result, dict):
                result["runtimeLease"] = released_lease
            if not released:
                self.ledger.record_audit_event({
                    "action": "local_connector_intake.lease_release_failed",
                    "entityType": "runtime_lease",
                    "entityId": CONNECTOR_INTAKE_LEASE_NAME,
                    "details": {
                        "actor": actor,
                        "triggerSource": trigger_source,
                        "externalSubmission": "not_executed",
                    },
                })

    def _sync_once(
        self,
        sources: Optional[Iterable[str]] = None,
        actor: str = "local_connector_intake",
        trigger_source: str = "connector_intake",
        workflow_metadata: Optional[Dict[str, Any]] = None,
        step_attempts: Optional[Dict[str, int]] = None,
    ) -> Dict[str, Any]:
        requested = _normalize_sources(sources)
        selected = requested or [item["source"] for item in self.plan()["sources"] if item["enabled"]]
        if not selected:
            return {
                "success": True,
                "status": "no_sources_enabled",
                "workflowRunId": None,
                "results": [],
                "summary": _empty_summary(),
                "externalSubmission": "not_executed",
            }

        unknown = [source for source in selected if source not in CONNECTOR_SOURCES]
        if unknown:
            raise ValueError(f"Unsupported connector source(s): {', '.join(sorted(unknown))}")

        started_at = _now()
        run_metadata = {
            "requestedSources": selected,
            "actor": actor,
            "externalSubmission": "not_executed",
        }
        run_metadata.update(workflow_metadata or {})
        workflow_run_id = self.ledger.create_workflow_run({
            "status": "running",
            "triggerSource": trigger_source,
            "startedAt": started_at,
            "metadata": run_metadata,
        })
        results = []
        for step_order, source in enumerate(selected, start=1):
            source_plan = self._source_plan(source)
            step_metadata = {
                "label": source_plan.get("label"),
                "mode": source_plan.get("mode"),
                "connectorProfile": source_plan.get("connectorProfile"),
                "configured": source_plan.get("configured"),
                "enabled": source_plan.get("enabled"),
                "canSync": source_plan.get("canSync"),
                "targetSystem": source_plan.get("targetSystem"),
                "externalSubmission": "not_executed",
            }
            workflow_step_id = self.ledger.create_workflow_step({
                "workflowRunId": workflow_run_id,
                "stepKey": f"source:{source}",
                "stage": "collect",
                "status": "pending",
                "attempt": max(1, int((step_attempts or {}).get(source, 1))),
                "stepOrder": step_order,
                "metadata": step_metadata,
            })
            started = time.perf_counter()
            self.ledger.update_workflow_step(workflow_step_id, {
                "status": "running",
                "startedAt": _now(),
            })
            try:
                result = self._sync_source(source)
            except Exception as exc:
                error = _safe_error(exc, self.config) or type(exc).__name__
                result = {
                    "source": source,
                    "sourceAccountId": None,
                    "status": "failed",
                    "seen": 0,
                    "registered": 0,
                    "duplicates": 0,
                    "revisions": 0,
                    "alreadyRegistered": 0,
                    "skipped": 0,
                    "error": error,
                    "externalSubmission": "not_executed",
                }
            duration_ms = int((time.perf_counter() - started) * 1000)
            self.ledger.update_workflow_step(workflow_step_id, {
                "status": _connector_step_status(result.get("status")),
                "finishedAt": _now(),
                "durationMs": duration_ms,
                "errorMessage": result.get("error"),
                "metadata": {
                    **step_metadata,
                    "result": _compact_connector_step_result(result),
                },
            })
            results.append(result)
        summary = _summarize(results)
        failures = [item for item in results if item["status"] in {"failed", "partial"}]
        attention = [
            item for item in results
            if item["status"] in {
                "needs_authorization",
                "needs_configuration",
                "disabled",
                "supervision_required",
            }
        ]
        if failures:
            status = "failed" if len(failures) == len(results) else "completed_with_errors"
        elif attention:
            status = "attention_required"
        else:
            status = "completed"
        finished_at = _now()
        self.ledger.update_workflow_run(workflow_run_id, {
            "status": status,
            "documentsImported": summary["registered"],
            "documentsProcessed": 0,
            "documentsNeedingReview": summary["duplicates"] + summary["revisions"],
            "errorMessage": "; ".join(item.get("error") or item["status"] for item in failures) or None,
            "finishedAt": finished_at,
            "metadata": {
                **(workflow_metadata or {}),
                "actor": actor,
                "requestedSources": selected,
                "summary": summary,
                "sourceStatuses": [
                    {"source": item["source"], "status": item["status"]}
                    for item in results
                ],
                "externalSubmission": "not_executed",
            },
        })
        self.ledger.record_audit_event({
            "action": "local_connector_intake.sync_completed",
            "entityType": "workflow_run",
            "entityId": str(workflow_run_id),
            "details": {
                "actor": actor,
                "status": status,
                "sources": [item["source"] for item in results],
                "sourceStatuses": [
                    {"source": item["source"], "status": item["status"]}
                    for item in results
                ],
                "summary": summary,
                "startedAt": started_at,
                "finishedAt": finished_at,
                "externalSubmission": "not_executed",
            },
        })
        return {
            "success": not failures,
            "status": status,
            "workflowRunId": workflow_run_id,
            "triggerSource": trigger_source,
            "results": results,
            "summary": summary,
            "externalSubmission": "not_executed",
        }

    def _sync_source(self, source: str) -> Dict[str, Any]:
        plan = self._source_plan(source)
        existing_source = next((
            item
            for item in self.ledger.list_source_accounts(source_type=source, limit=100)
            if str(item.get("source_identifier") or "") == str(plan["sourceIdentifier"])
        ), None)
        previous_metadata = (
            dict(existing_source.get("metadata") or {})
            if isinstance(existing_source, dict) and isinstance(existing_source.get("metadata"), dict)
            else {}
        )
        source_account_id = self.ledger.upsert_source_account({
            "sourceType": source,
            "sourceIdentifier": plan["sourceIdentifier"],
            "label": plan["label"],
            "status": "syncing" if plan["canSync"] else plan["status"],
            "lastScanAt": _now(),
            "metadata": {
                **previous_metadata,
                "configured": plan["configured"],
                "enabled": plan["enabled"],
                "mode": plan["mode"],
                "scannerProfile": plan.get("scannerProfile"),
                "connectorProfile": plan.get("connectorProfile"),
                "nextAction": plan.get("nextAction"),
                "externalSubmission": "not_executed",
                **({"targetSystem": plan["targetSystem"]} if plan.get("targetSystem") else {}),
            },
        })
        if not plan["canSync"]:
            return {
                "source": source,
                "sourceAccountId": source_account_id,
                "status": plan["status"],
                "seen": 0,
                "registered": 0,
                "duplicates": 0,
                "revisions": 0,
                "alreadyRegistered": 0,
                "skipped": 0,
                "nextAction": plan.get("nextAction"),
                "externalSubmission": "not_executed",
            }

        scan_started_at = _now()
        try:
            fetcher_config = dict(self.config)
            if source == "gmail":
                checkpoint = _epoch_seconds(previous_metadata.get("lastSuccessfulSyncAt"))
                overlap_seconds = int(_positive_float_config(
                    self.config,
                    "gmail_incremental_overlap_seconds",
                    default=86400.0,
                ))
                if checkpoint:
                    fetcher_config["gmail_incremental_after_epoch"] = max(
                        1,
                        checkpoint - overlap_seconds,
                    )
            fetcher = self.fetcher_factories[source](fetcher_config)
        except Exception as exc:
            return self._source_failure(
                plan,
                source_account_id,
                exc,
                scan_started_at,
                previous_metadata=previous_metadata,
            )

        try:
            documents = fetcher.fetch_documents()
        except Exception as exc:
            return self._source_failure(
                plan,
                source_account_id,
                exc,
                scan_started_at,
                previous_metadata=previous_metadata,
            )
        if not isinstance(documents, list):
            return self._source_failure(
                plan,
                source_account_id,
                TypeError("Connector returned a non-list document payload"),
                scan_started_at,
                previous_metadata=previous_metadata,
            )
        registrar = LocalFolderIntake(self.ledger, allowed_extensions={"*"}, source=source)
        counters = {
            "seen": len(documents),
            "registered": 0,
            "duplicates": 0,
            "revisions": 0,
            "alreadyRegistered": 0,
            "skipped": 0,
            "targetBackfills": 0,
        }
        registered_documents = []
        registration_errors = []
        for document in documents:
            if not isinstance(document, dict):
                counters["skipped"] += 1
                continue
            root = self._download_root(source, document)
            routed_document = _with_default_target_system(document, plan.get("targetSystem"))
            try:
                result = registrar.register_fetched_document(
                    routed_document,
                    source_account_id=source_account_id,
                    root=root,
                )
            except Exception as exc:
                counters["skipped"] += 1
                registration_errors.append(_safe_error(exc, self.config))
                continue
            if result.get("skipped"):
                counters["skipped"] += 1
                skipped = result["skipped"]
                registration_errors.append(
                    f"{skipped.get('reason') or 'registration_failed'}: {skipped.get('path') or 'unknown path'}"
                )
                continue
            result_status = result.get("status")
            if result_status == "already_registered":
                counters["alreadyRegistered"] += 1
            else:
                counters["registered"] += 1
                if result_status == "duplicate":
                    counters["duplicates"] += 1
                elif result_status == "revision":
                    counters["revisions"] += 1
            registered_documents.append(result.get("document"))
            registered_document = result.get("document") or {}
            document_id = registered_document.get("id")
            if registered_document.get("targetBackfilled"):
                counters["targetBackfills"] += 1
            if (
                document_id
                and registered_document.get("targetBackfilled")
                and self.ledger.get_bookkeeping_record_by_document(int(document_id))
            ):
                LocalBookkeepingRecordService(self.ledger, self.config).upsert_from_document(
                    int(document_id)
                )

        counters["targetBackfills"] += self._backfill_source_target(
            source,
            source_account_id,
            plan.get("targetSystem"),
        )

        diagnostics = getattr(fetcher, "last_run", {})
        fetch_error = getattr(fetcher, "last_error", None) or getattr(fetcher, "auth_error", None)
        reauthorization_required = (
            source in {"gmail", "google_drive"}
            and _oauth_reauthorization_required(fetch_error)
        )
        if reauthorization_required:
            self._mark_reauthorization_required(source)
        errors = [item for item in [_safe_error(fetch_error, self.config)] if item]
        errors.extend(item for item in registration_errors if item)
        successful_evidence = counters["registered"] + counters["alreadyRegistered"]
        status = (
            "needs_authorization"
            if reauthorization_required
            else "partial"
            if errors and successful_evidence
            else "failed"
            if errors
            else "ready"
        )
        error = "; ".join(errors)[:500] if errors else None
        next_action = _next_action(source, status) if reauthorization_required else None
        self.ledger.upsert_source_account({
            "sourceType": source,
            "sourceIdentifier": plan["sourceIdentifier"],
            "label": plan["label"],
            "status": status,
            "lastScanAt": scan_started_at,
            "lastSeenAt": _now() if documents else None,
            "documentsSeen": counters["seen"],
            "documentsImported": counters["registered"],
            "duplicatesDetected": counters["duplicates"],
            "metadata": {
                **previous_metadata,
                "configured": True,
                "enabled": True,
                "mode": plan["mode"],
                "scannerProfile": plan.get("scannerProfile"),
                "connectorProfile": plan.get("connectorProfile"),
                "diagnostics": diagnostics,
                "run": counters,
                "error": error,
                "nextAction": next_action,
                "lastSuccessfulSyncAt": _now() if status == "ready" else previous_metadata.get("lastSuccessfulSyncAt"),
                "externalSubmission": "not_executed",
                **({"targetSystem": plan["targetSystem"]} if plan.get("targetSystem") else {}),
            },
        })
        if errors:
            self.ledger.record_audit_event({
                "action": "local_connector_intake.source_failed",
                "entityType": "source_account",
                "entityId": str(source_account_id),
                "details": {
                    "source": source,
                    "status": status,
                    "error": error,
                    "run": counters,
                    "externalSubmission": "not_executed",
                },
            })
        return {
            "source": source,
            "sourceAccountId": source_account_id,
            "status": status,
            **counters,
            "documents": registered_documents,
            "diagnostics": diagnostics,
            "error": error,
            "nextAction": next_action,
            "externalSubmission": "not_executed",
        }

    def _source_failure(
        self,
        plan: Dict[str, Any],
        source_account_id: int,
        error: Exception,
        scan_started_at: str,
        previous_metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        message = _safe_error(error, self.config)
        reauthorization_required = (
            plan["source"] in {"gmail", "google_drive"}
            and _oauth_reauthorization_required(error)
        )
        if reauthorization_required:
            self._mark_reauthorization_required(plan["source"])
        status = "needs_authorization" if reauthorization_required else "failed"
        next_action = _next_action(plan["source"], status) if reauthorization_required else None
        self.ledger.upsert_source_account({
            "sourceType": plan["source"],
            "sourceIdentifier": plan["sourceIdentifier"],
            "label": plan["label"],
            "status": status,
            "lastScanAt": scan_started_at,
            "metadata": {
                **(previous_metadata or {}),
                "configured": plan["configured"],
                "enabled": plan["enabled"],
                "mode": plan["mode"],
                "error": message,
                "nextAction": next_action,
                "externalSubmission": "not_executed",
            },
        })
        self.ledger.record_audit_event({
            "action": "local_connector_intake.source_failed",
            "entityType": "source_account",
            "entityId": str(source_account_id),
            "details": {
                "source": plan["source"],
                "status": status,
                "error": message,
                "externalSubmission": "not_executed",
            },
        })
        return {
            "source": plan["source"],
            "sourceAccountId": source_account_id,
            "status": status,
            "seen": 0,
            "registered": 0,
            "duplicates": 0,
            "revisions": 0,
            "alreadyRegistered": 0,
            "skipped": 0,
            "error": message,
            "nextAction": next_action,
            "externalSubmission": "not_executed",
        }

    def _mark_reauthorization_required(self, source: str) -> None:
        token_keys = {
            "gmail": ("gmail_token_file", "gmail_token_path"),
            "google_drive": ("google_drive_token_file", "drive_token_path"),
        }.get(source, ())
        configured_path = _configured_path_value(self.config, *token_keys)
        if not configured_path:
            return
        GoogleOAuthTokenStore(configured_path, []).mark_reauthorization(
            "oauth_token_revoked_or_expired"
        )

    def _source_plan(self, source: str) -> Dict[str, Any]:
        configured = self._configured(source)
        target_system = _configured_target_system(self.config, source)
        target_system_valid = target_system in CONNECTOR_TARGET_SYSTEMS if target_system else True
        enabled = _configured_bool(
            self.config,
            f"{source}_enabled",
            f"source_{source}_enabled",
            default=False,
        )
        source_identifier = self._source_identifier(source)
        gmail_scanner_mode = _configured_bool(
            self.config,
            "gmail_scanner_mode",
            default=False,
        )
        gmail_scanner_policy_ready = bool(
            _list_config_value(self.config.get("gmail_trusted_senders"))
        )
        label = {
            "gmail": "Gmail scanner inbox" if gmail_scanner_mode else "Gmail",
            "google_drive": "Google Drive",
            "freshdesk": "Freshdesk",
            "google_photos": "Google Photos Picker",
        }[source]
        if source == "google_photos":
            if not enabled:
                status = "disabled"
            elif not configured:
                status = "needs_configuration"
            else:
                status = "supervision_required"
            return {
                "source": source,
                "sourceIdentifier": source_identifier,
                "label": label,
                "configured": configured,
                "enabled": enabled,
                "canSync": False,
                "status": status,
                "mode": "picker_required",
                "nextAction": (
                    "Configure a Picker-scoped OAuth token before starting a supervised selection session."
                    if status == "needs_configuration"
                    else "Start a supervised Google Photos selection from Sources; background whole-library access is no longer available."
                    if status == "supervision_required"
                    else "Enable the supervised Google Photos Picker integration when needed."
                ),
                "targetSystem": target_system if target_system_valid else None,
            }
        if not enabled:
            status = "disabled"
        elif source == "gmail" and gmail_scanner_mode and not gmail_scanner_policy_ready:
            status = "needs_configuration"
        elif source in {"gmail", "google_drive"} and self._reauthorization_required(source):
            status = "needs_authorization"
        elif not target_system_valid:
            status = "needs_configuration"
        elif not configured:
            status = "needs_configuration"
        else:
            status = "ready"
        return {
            "source": source,
            "sourceIdentifier": source_identifier,
            "label": label,
            "configured": configured,
            "enabled": enabled,
            "canSync": status == "ready",
            "status": status,
            "mode": (
                "scanner_mailbox_read_only"
                if source == "gmail" and gmail_scanner_mode
                else "financial_ticket_read_only"
                if source == "freshdesk" and _configured_bool(
                    self.config,
                    "freshdesk_financial_filter_enabled",
                    default=False,
                )
                else "read_only_connector"
            ),
            "scannerProfile": self._gmail_scanner_profile() if source == "gmail" else None,
            "connectorProfile": self._freshdesk_profile() if source == "freshdesk" else None,
            "nextAction": (
                f"Set {source}.target_system to waveapps_business, waveapps_personal, or mijngeldzaken."
                if not target_system_valid
                else _next_action(source, status)
            ),
            "targetSystem": target_system if target_system_valid else None,
        }

    def _backfill_source_target(
        self,
        source: str,
        source_account_id: int,
        target_system: Any,
    ) -> int:
        target = str(target_system or "").strip()
        if not target:
            return 0
        backfilled = 0
        after_id = 0
        while True:
            documents = self.ledger.list_documents_for_source_account(
                source_account_id,
                after_id=after_id,
                limit=500,
            )
            if not documents:
                break
            for document in documents:
                document_id = int(document["id"])
                after_id = max(after_id, document_id)
                if _document_target_system(document):
                    continue
                metadata = dict(document.get("metadata") or {})
                metadata["targetSystem"] = target
                self.ledger.update_document(document_id, {"metadata": metadata})
                if self.ledger.get_bookkeeping_record_by_document(document_id):
                    LocalBookkeepingRecordService(self.ledger, self.config).upsert_from_document(
                        document_id
                    )
                self.ledger.record_audit_event({
                    "action": "local_connector_intake.target_system_backfilled",
                    "entityType": "bookkeeping_document",
                    "entityId": str(document_id),
                    "details": {
                        "source": source,
                        "sourceAccountId": source_account_id,
                        "targetSystem": target,
                        "externalSubmission": "not_executed",
                    },
                })
                backfilled += 1
            if len(documents) < 500:
                break
        return backfilled

    def _configured(self, source: str) -> bool:
        if source == "gmail":
            return _existing_config_path(self.config, "gmail_credentials_file", "gmail_credentials_path") and _existing_config_path(
                self.config,
                "gmail_token_file",
                "gmail_token_path",
                google_oauth_token=True,
            )
        if source == "google_drive":
            return _existing_config_path(
                self.config,
                "google_drive_credentials_file",
                "drive_credentials_path",
            ) and _existing_config_path(
                self.config,
                "google_drive_token_file",
                "drive_token_path",
                google_oauth_token=True,
            ) and bool(
                str(
                    self.config.get("google_drive_folder_id")
                    or self.config.get("drive_folder_id")
                    or ""
                ).strip()
            )
        if source == "freshdesk":
            return bool(
                self.config.get("freshdesk_api_key")
                and (
                    self.config.get("freshdesk_domain")
                    or self.config.get("freshdesk_api_url")
                )
            )
        token_path = _config_path(
            self.config,
            "google_photos_picker_token_file",
            "google_photos_token_file",
            "photos_token_path",
        )
        return _existing_config_path(
            self.config,
            "google_photos_credentials_file",
            "photos_credentials_path",
        ) and bool(token_path and token_path.lower().endswith(".json") and os.path.isfile(token_path))

    def _reauthorization_required(self, source: str) -> bool:
        token_keys = {
            "gmail": ("gmail_token_file", "gmail_token_path"),
            "google_drive": ("google_drive_token_file", "drive_token_path"),
        }.get(source, ())
        configured_path = _configured_path_value(self.config, *token_keys)
        return bool(
            configured_path
            and GoogleOAuthTokenStore(configured_path, []).status()["reauthorizationRequired"]
        )

    def _gmail_scanner_profile(self) -> Dict[str, Any]:
        trusted_senders = _list_config_value(self.config.get("gmail_trusted_senders"))
        is_hp_eprint = trusted_senders == [HP_EPRINT_SENDER]
        return {
            "enabled": _configured_bool(self.config, "gmail_scanner_mode", default=False),
            "profileId": (
                HP_EPRINT_PROFILE_ID
                if is_hp_eprint
                else CUSTOM_SCANNER_PROFILE_ID
            ),
            "trustedSenders": trusted_senders,
            "query": str(
                self.config.get("gmail_query")
                or self.config.get("gmail_search_query")
                or "has:attachment"
            ),
            "documentPolicy": "pdf_only_magic_verified",
            "originalRetention": "email_unchanged",
            "deliveryPath": "gmail_to_fab_direct",
            "sourceProvenance": (
                {
                    "repository": "Noodzakelijk-Online/025-Scan-to-folder-automation",
                    "auditedCommit": "e3078d92c214aa3b17d98a8687f16e73f52f71ba",
                    "legacyTransport": "gmail_to_google_drive_apps_script",
                }
                if is_hp_eprint
                else None
            ),
        }

    def _freshdesk_profile(self) -> Dict[str, Any]:
        filter_enabled = _configured_bool(
            self.config,
            "freshdesk_financial_filter_enabled",
            default=False,
        )
        keywords = _list_config_value(
            self.config.get("freshdesk_financial_keywords")
        )
        if filter_enabled and not keywords:
            keywords = list(LEGACY_025_FINANCIAL_KEYWORDS)
        include_description = _configured_bool(
            self.config,
            "freshdesk_include_ticket_description",
            default=filter_enabled,
        )
        pdf_only = _configured_bool(
            self.config,
            "freshdesk_pdf_only",
            default=filter_enabled,
        )
        return {
            "enabled": filter_enabled,
            "profileId": (
                LEGACY_025_PROFILE_ID if filter_enabled else "generic_read_only"
            ),
            "financialKeywords": keywords,
            "ticketStatuses": (
                _list_config_value(self.config.get("freshdesk_ticket_statuses"))
                or ["2", "3"]
            ),
            "includeTicketDescription": include_description,
            "descriptionPolicy": (
                "non_posting_supporting_evidence"
                if include_description
                else "not_collected"
            ),
            "attachmentPolicy": (
                "pdf_only_magic_verified"
                if pdf_only
                else "bounded_immutable_attachment"
            ),
            "originalRetention": "ticket_unchanged",
            "ticketMutation": "not_executed",
            "driveCopy": "not_executed",
            "deliveryPath": "freshdesk_to_fab_direct",
            "sourceProvenance": (
                {
                    "repository": LEGACY_025_REPOSITORY,
                    "auditedCommit": LEGACY_025_COMMIT,
                    "legacyTransport": "freshdesk_to_google_drive",
                }
                if filter_enabled
                else None
            ),
        }

    def _source_identifier(self, source: str) -> str:
        if source == "gmail":
            return str(self.config.get("gmail_user_id") or "me")
        if source == "google_drive":
            return str(self.config.get("drive_folder_id") or self.config.get("google_drive_folder_id") or "all-files")
        if source == "freshdesk":
            return str(
                self.config.get("freshdesk_domain")
                or self.config.get("freshdesk_api_url")
                or "unconfigured"
            )
        return "supervised-picker"

    def _download_root(self, source: str, _document: Dict[str, Any]) -> str:
        keys = {
            "gmail": ("gmail_attachment_download_dir", "gmail_download_dir"),
            "google_drive": ("google_drive_download_dir", "drive_download_dir"),
            "freshdesk": ("freshdesk_download_dir",),
        }.get(source, ())
        section = self.config.get(source)
        section = section if isinstance(section, dict) else {}
        candidates = [self.config.get(key) for key in keys]
        candidates.extend((section.get("download_dir"), self.config.get("attachments_save_dir")))
        for value in candidates:
            if str(value or "").strip():
                return os.path.abspath(os.path.expanduser(os.path.expandvars(str(value))))
        default_directories = {
            "gmail": "gmail",
            "google_drive": "google-drive",
            "freshdesk": "freshdesk",
            "google_photos": "google-photos-picker",
        }
        directory = default_directories.get(source, "source")
        return os.path.abspath(os.path.join("data", "source_downloads", directory))


def _normalize_sources(values: Optional[Iterable[str]]) -> list:
    if values is None:
        return []
    if isinstance(values, str):
        values = values.replace(";", ",").split(",")
    normalized = []
    for value in values:
        source = SOURCE_ALIASES.get(str(value or "").strip().lower(), str(value or "").strip().lower())
        if source and source not in normalized:
            normalized.append(source)
    return normalized


def _configured_target_system(config: Dict[str, Any], source: str) -> str:
    nested = config.get(source) if isinstance(config.get(source), dict) else {}
    value = config.get(f"{source}_target_system") or nested.get("target_system") or ""
    return str(value).strip().lower().replace("-", "_").replace(" ", "_")


def _with_default_target_system(document: Dict[str, Any], target_system: Any) -> Dict[str, Any]:
    result = dict(document)
    metadata = dict(result.get("metadata") or {}) if isinstance(result.get("metadata"), dict) else {}
    existing = resolve_document_target_system(result)
    if not existing and str(target_system or "").strip():
        metadata["targetSystem"] = str(target_system).strip()
        result["targetSystem"] = str(target_system).strip()
    result["metadata"] = metadata
    return result


def _document_target_system(document: Dict[str, Any]) -> str:
    return resolve_document_target_system(document)


def _list_config_value(value: Any) -> list:
    if isinstance(value, (list, tuple, set)):
        values = value
    else:
        values = str(value or "").replace(";", ",").split(",")
    return list(dict.fromkeys(
        str(item or "").strip().lower()
        for item in values
        if str(item or "").strip()
    ))


def _empty_summary() -> Dict[str, int]:
    return {
        "sources": 0,
        "seen": 0,
        "registered": 0,
        "duplicates": 0,
        "revisions": 0,
        "alreadyRegistered": 0,
        "skipped": 0,
        "targetBackfills": 0,
        "failedSources": 0,
    }


def _summarize(results: list) -> Dict[str, int]:
    summary = _empty_summary()
    summary["sources"] = len(results)
    for item in results:
        for key in (
            "seen", "registered", "duplicates", "revisions", "alreadyRegistered", "skipped", "targetBackfills"
        ):
            summary[key] += int(item.get(key) or 0)
        if item.get("status") in {"failed", "partial"}:
            summary["failedSources"] += 1
    return summary


def _connector_step_status(status: Any) -> str:
    normalized = str(status or "failed").strip().lower()
    if normalized in {"ready", "completed"}:
        return "completed"
    if normalized in {"failed", "partial", "error", "completed_with_errors"}:
        return "failed"
    return "skipped"


def _compact_connector_step_result(result: Dict[str, Any]) -> Dict[str, Any]:
    keys = (
        "source",
        "sourceAccountId",
        "status",
        "seen",
        "registered",
        "duplicates",
        "revisions",
        "alreadyRegistered",
        "skipped",
        "targetBackfills",
        "nextAction",
        "externalSubmission",
    )
    return {
        key: result.get(key)
        for key in keys
        if result.get(key) is not None
    }


def _configured_bool(config: Dict[str, Any], *keys: str, default: bool) -> bool:
    for key in keys:
        if key in config and config.get(key) not in (None, ""):
            value = config.get(key)
            if isinstance(value, str):
                return value.strip().lower() not in {"0", "false", "no", "off", ""}
            return bool(value)
    return default


def _positive_float_config(
    config: Dict[str, Any],
    *keys: str,
    default: float,
) -> float:
    value = next((config.get(key) for key in keys if config.get(key) not in (None, "")), default)
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _existing_config_path(
    config: Dict[str, Any],
    *keys: str,
    google_oauth_token: bool = False,
) -> bool:
    configured = _configured_path_value(config, *keys)
    if not configured:
        return False
    if google_oauth_token:
        return GoogleOAuthTokenStore(configured, []).status()["tokenPresent"]
    return os.path.isfile(configured)


def _config_path(config: Dict[str, Any], *keys: str) -> Optional[str]:
    path = _configured_path_value(config, *keys)
    return path if path and os.path.isfile(path) else None


def _configured_path_value(config: Dict[str, Any], *keys: str) -> Optional[str]:
    for key in keys:
        value = config.get(key)
        if str(value or "").strip():
            return os.path.abspath(os.path.expanduser(os.path.expandvars(str(value))))
    return None


def _next_action(source: str, status: str) -> Optional[str]:
    if status == "ready":
        return None
    if status == "disabled":
        return f"Enable {source} after its read-only credentials and approved source scope are configured."
    if source == "google_drive":
        return (
            "Open Google Drive setup in the FAB operator dashboard, complete the desktop "
            "OAuth consent flow, and confirm the approved folder_id."
        )
    if source == "gmail":
        return (
            "Open Gmail scanner setup in the FAB operator dashboard and complete the "
            "read-only desktop OAuth consent flow."
        )
    return f"Configure the read-only credentials and token required for {source}."


def _oauth_reauthorization_required(error: Any) -> bool:
    if not error:
        return False
    message = f"{type(error).__name__}: {error}".lower()
    return "invalid_grant" in message or "token has been expired or revoked" in message


def _safe_error(error: Any, config: Dict[str, Any]) -> Optional[str]:
    if not error:
        return None
    message = f"{type(error).__name__}: {error}"
    for key, value in config.items():
        if not re.search(r"(?i)(token|secret|password|api[_-]?key|authorization|credential)", str(key)):
            continue
        secret = str(value or "")
        if len(secret) >= 4:
            message = message.replace(secret, "[REDACTED]")
    message = re.sub(
        r"(?i)((?:access[_-]?token|refresh[_-]?token|token|password|secret|(?:x[_-]?)?api[_-]?key)\s*[:=]\s*)[^&,;\s]+",
        r"\1[REDACTED]",
        message,
    )
    message = re.sub(
        r"(?i)(authorization\s*[:=]\s*)(?:bearer\s+)?[^,;\s]+",
        r"\1[REDACTED]",
        message,
    )
    message = re.sub(
        r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]+",
        r"\1[REDACTED]",
        message,
    )
    return message[:500]


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _epoch_seconds(value: Any) -> int:
    text = str(value or "").strip()
    if not text:
        return 0
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return 0
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return max(0, int(parsed.timestamp()))
