import { useEffect, useRef, useState, type ReactNode } from "react";
import {
  Activity,
  ChevronLeft,
  DatabaseBackup,
  FileWarning,
  FileCheck2,
  History,
  LayoutDashboard,
  Landmark,
  LogOut,
  Menu,
  PanelLeftClose,
  PanelLeftOpen,
  PlugZap,
  SendToBack,
  RefreshCw,
  RotateCcw,
  Search,
  Settings2,
  X,
} from "lucide-react";
import { useFabLocale } from "./fabLocale";

const navigation = [
  { id: "control-room", label: "Overview", labelNl: "Overzicht", icon: LayoutDashboard },
  { id: "exceptions", label: "Exceptions", labelNl: "Uitzonderingen", icon: FileWarning },
  { id: "review-workspace", label: "Review queue", labelNl: "Controlewachtrij", icon: FileCheck2 },
  { id: "automation", label: "Automation", labelNl: "Automatisering", icon: Activity },
  { id: "reporting", label: "Reporting", labelNl: "Rapportage", icon: Landmark },
  { id: "audit", label: "Activity", labelNl: "Activiteit", icon: History },
  { id: "recovery", label: "Recovery", labelNl: "Herstel", icon: RotateCcw },
  { id: "backups", label: "Backups", labelNl: "Back-ups", icon: DatabaseBackup },
  { id: "delivery", label: "Delivery", labelNl: "Levering", icon: SendToBack },
  { id: "connections", label: "Connections", labelNl: "Koppelingen", icon: PlugZap },
];

type FabOperatorShellProps = {
  children: ReactNode;
  connected: boolean;
  connectionStatus: string;
  organization: string;
  operatorLabel: string;
  managedOperator?: boolean;
  search: string;
  onSearchChange: (value: string) => void;
  onRefresh: () => void;
  refreshing: boolean;
  onOpenCommands: () => void;
  reviewCount?: number | null;
};

export function FabOperatorShell({
  children,
  connected,
  connectionStatus,
  organization,
  operatorLabel,
  managedOperator = false,
  search,
  onSearchChange,
  onRefresh,
  refreshing,
  onOpenCommands,
  reviewCount,
}: FabOperatorShellProps) {
  const { lang, setLang, copy } = useFabLocale();
  const [navOpen, setNavOpen] = useState(false);
  const [sidebarCollapsed, setSidebarCollapsed] = useState(false);
  const [activeSection, setActiveSection] = useState("control-room");
  const menuButtonRef = useRef<HTMLButtonElement>(null);
  const closeButtonRef = useRef<HTMLButtonElement>(null);
  const manualNavigationRef = useRef<{ id: string; until: number } | null>(null);

  useEffect(() => {
    if (!navOpen) return;
    document.body.classList.add("fab-dialog-open");
    closeButtonRef.current?.focus();
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") setNavOpen(false);
      if (event.key !== "Tab") return;
      const sidebar = closeButtonRef.current?.closest<HTMLElement>("#fab-sidebar");
      const focusable = sidebar ? Array.from(sidebar.querySelectorAll<HTMLElement>("button:not(:disabled), a[href]")) : [];
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
      menuButtonRef.current?.focus();
    };
  }, [navOpen]);

  useEffect(() => {
    const sections = navigation.map(({ id }) => document.getElementById(id)).filter((element): element is HTMLElement => Boolean(element));
    if (!sections.length || typeof IntersectionObserver === "undefined") return;
    const observer = new IntersectionObserver((entries) => {
      if (manualNavigationRef.current && Date.now() < manualNavigationRef.current.until) return;
      const visible = entries.filter((entry) => entry.isIntersecting).sort((a, b) => b.intersectionRatio - a.intersectionRatio)[0];
      if (visible?.target.id) setActiveSection(visible.target.id);
    }, { rootMargin: "-15% 0px -70%", threshold: [0, 0.2, 0.6] });
    sections.forEach((section) => observer.observe(section));
    return () => observer.disconnect();
  }, [children]);

  function navigate(sectionId: string) {
    manualNavigationRef.current = { id: sectionId, until: Date.now() + 1_200 };
    setActiveSection(sectionId);
    document.getElementById(sectionId)?.scrollIntoView({ behavior: "smooth", block: "start" });
    setNavOpen(false);
  }

  return (
    <div className={`fab-operator ${sidebarCollapsed ? "is-sidebar-collapsed" : ""}`}>
      <aside id="fab-sidebar" className={`fab-sidebar ${navOpen ? "is-open" : ""}`} aria-label={copy("FAB navigation", "FAB-navigatie")} role={navOpen ? "dialog" : undefined} aria-modal={navOpen || undefined}>
        <div className="fab-brand">
          <div className="fab-brand-mark"><FabBrandMark /></div>
          <div className="fab-brand-copy"><strong>FAB</strong><span>Bookkeeping OS</span></div>
          <button className="fab-icon-button fab-sidebar-collapse" onClick={() => setSidebarCollapsed((value) => !value)} aria-label={sidebarCollapsed ? copy("Expand sidebar", "Zijbalk uitklappen") : copy("Collapse sidebar", "Zijbalk inklappen")} title={sidebarCollapsed ? copy("Expand sidebar", "Zijbalk uitklappen") : copy("Collapse sidebar", "Zijbalk inklappen")}>
            {sidebarCollapsed ? <PanelLeftOpen aria-hidden="true" /> : <PanelLeftClose aria-hidden="true" />}
          </button>
          <button ref={closeButtonRef} className="fab-icon-button fab-nav-close" onClick={() => setNavOpen(false)} aria-label={copy("Close navigation", "Navigatie sluiten")} title={copy("Close navigation", "Navigatie sluiten")}>
            <ChevronLeft aria-hidden="true" />
          </button>
        </div>
        <nav className="fab-nav">
          <span className="fab-nav-label">{copy("Workspace", "Werkruimte")}</span>
          {navigation.map(({ id, label, labelNl, icon: Icon }) => (
            <button key={id} className={activeSection === id ? "is-active" : ""} onClick={() => navigate(id)} aria-current={activeSection === id ? "location" : undefined} title={sidebarCollapsed ? (lang === "nl" ? labelNl : label) : undefined}>
              <Icon aria-hidden="true" /><span>{lang === "nl" ? labelNl : label}</span>
              {id === "review-workspace" && reviewCount !== null && reviewCount !== undefined && reviewCount > 0
                ? <strong className="fab-nav-badge" aria-label={`${reviewCount} ${copy("documents waiting", "documenten wachten")}`}>{reviewCount > 999 ? "999+" : reviewCount}</strong>
                : null}
            </button>
          ))}
        </nav>
        <div className="fab-sidebar-footer">
          <div className="fab-language-switch" aria-label={copy("Language", "Taal")}><button className={lang === "en" ? "is-active" : ""} onClick={() => setLang("en")} aria-pressed={lang === "en"}>EN</button><button className={lang === "nl" ? "is-active" : ""} onClick={() => setLang("nl")} aria-pressed={lang === "nl"}>NL</button></div>
          <button onClick={() => { setNavOpen(false); onOpenCommands(); }}><Settings2 aria-hidden="true" /><span>{copy("Safe commands", "Veilige opdrachten")}</span></button>
          {managedOperator && <button onClick={() => { window.location.href = "/operator/logout"; }}><LogOut aria-hidden="true" /><span>{copy("Sign out", "Uitloggen")}</span></button>}
          <div className="fab-operator-id"><Activity aria-hidden="true" /><span><strong>{operatorLabel}</strong><small>{copy("Authenticated operator", "Geverifieerde operator")}</small></span></div>
        </div>
      </aside>

      {navOpen && <button className="fab-nav-scrim" onClick={() => setNavOpen(false)} aria-label={copy("Close navigation overlay", "Navigatie-overlay sluiten")} />}

      <div className="fab-workspace">
        <header className="fab-topbar">
          <button ref={menuButtonRef} className="fab-icon-button fab-menu-button" onClick={() => setNavOpen(true)} aria-label={copy("Open navigation", "Navigatie openen")} title={copy("Open navigation", "Navigatie openen")} aria-expanded={navOpen} aria-controls="fab-sidebar">
            <Menu aria-hidden="true" />
          </button>
          <div className="fab-organization">
            <span>{(() => { const current = navigation.find((item) => item.id === activeSection); return current ? (lang === "nl" ? current.labelNl : current.label) : copy("Workspace", "Werkruimte"); })()}</span>
            <strong>{organization}</strong>
          </div>
          <div className={`fab-connection-pill tone-${connected ? "good" : "bad"}`}>
            <span className="fab-status-dot" />
            {connected ? copy("Local API connected", "Lokale API verbonden") : connectionStatus}
          </div>
          <label className="fab-search">
            <Search aria-hidden="true" />
            <span className="sr-only">{copy("Search control center", "Zoek in het controlecentrum")}</span>
            <input value={search} onChange={(event) => onSearchChange(event.target.value)} placeholder={copy("Search exceptions, sources, activity", "Zoek uitzonderingen, bronnen, activiteit")} />
            {search && <button type="button" onClick={() => onSearchChange("")} aria-label={copy("Clear search", "Zoekopdracht wissen")} title={copy("Clear search", "Zoekopdracht wissen")}><X aria-hidden="true" /></button>}
          </label>
          <button className="fab-icon-button" onClick={onRefresh} disabled={refreshing} aria-label={copy("Refresh control center", "Controlecentrum vernieuwen")} title={copy("Refresh control center", "Controlecentrum vernieuwen")}>
            <RefreshCw className={refreshing ? "is-spinning" : ""} aria-hidden="true" />
          </button>
          <div className="fab-avatar" aria-label={`${copy("Signed in as", "Ingelogd als")} ${operatorLabel}`}>{operatorLabel.slice(0, 2).toUpperCase()}</div>
        </header>
        <main>{children}</main>
      </div>
    </div>
  );
}

function FabBrandMark() {
  return (
    <svg viewBox="0 0 24 24" role="img" aria-label="FAB">
      <path d="M4 4h7v5H4zM13 4h7v5h-7zM4 11h7v9H4zM13 11h7v4h-7zM13 17h7v3h-7z" fill="currentColor" stroke="none" />
    </svg>
  );
}
