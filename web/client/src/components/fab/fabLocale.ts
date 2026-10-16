import { useCallback } from "react";
import { useLanguage } from "@/contexts/LanguageContext";
import { humanize } from "./fabView";

const dutchStatuses: Record<string, string> = {
  ok: "In orde",
  ready: "Gereed",
  completed: "Voltooid",
  running: "Bezig",
  blocked: "Geblokkeerd",
  failed: "Mislukt",
  error: "Fout",
  disconnected: "Niet verbonden",
  connected: "Verbonden",
  active: "Actief",
  unavailable: "Niet beschikbaar",
  not_executed: "Niet uitgevoerd",
  not_configured: "Niet ingesteld",
  disabled: "Uitgeschakeld",
  needs_token: "Token vereist",
  needs_business_id: "Bedrijfs-ID vereist",
  needs_validation: "Validatie vereist",
  needs_mapping: "Toewijzing vereist",
  needs_receipt_executor: "Bewijsexecutor vereist",
  not_connected: "Niet verbonden",
  not_running: "Niet actief",
  invalid_runtime: "Ongeldige runtime",
  authentication_required: "Aanmelding vereist",
  wrong_business: "Verkeerd bedrijf",
  incompatible: "Niet compatibel",
  stale: "Verbinding verlopen",
  busy: "Bezig",
  credentials_required: "Referenties vereist",
  needs_attention: "Aandacht nodig",
  supervision_required: "Toezicht vereist",
  outcome_unknown: "Resultaat onbekend; controleer Wave",
  idle: "Inactief",
  high: "Hoog",
  medium: "Middel",
  low: "Laag",
};

export function useFabLocale() {
  const { lang, setLang } = useLanguage();
  const copy = useCallback((english: string, dutch: string) => lang === "nl" ? dutch : english, [lang]);
  const status = useCallback((value: unknown) => {
    const key = typeof value === "string" ? value.toLowerCase() : "";
    return lang === "nl" && dutchStatuses[key] ? dutchStatuses[key] : humanize(value);
  }, [lang]);
  return {
    lang,
    setLang,
    copy,
    status,
    dateLocale: lang === "nl" ? "nl-NL" : "en-NL",
  };
}
