import {
  Activity,
  ArrowLeft,
  AlertTriangle,
  ArrowDownRight,
  ArrowUpRight,
  Check,
  ChevronRight,
  Clock3,
  Database,
  Gauge,
  HeartPulse,
  LayoutDashboard,
  Link2,
  ListFilter,
  RefreshCw,
  Search,
  ShieldCheck,
  TimerReset,
  WifiOff,
  X,
} from "lucide-react";
import { useCallback, useEffect, useMemo, useState } from "react";
import { loadDashboard } from "./api";
import { SportMark } from "./components/SportMark";

const SPORTS = ["All", "CS2", "Dota 2", "Valorant", "League of Legends"];

function number(value, maximumFractionDigits = 0) {
  return new Intl.NumberFormat("en-US", { maximumFractionDigits }).format(value ?? 0);
}

function time(value, includeDate = false) {
  if (!value) return "Unavailable";
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return "Unavailable";
  return new Intl.DateTimeFormat("en", {
    ...(includeDate ? { month: "short", day: "numeric" } : {}),
    hour: "2-digit",
    minute: "2-digit",
    timeZoneName: "short",
  }).format(parsed);
}

function relative(value) {
  if (!value) return "Never";
  const milliseconds = Date.now() - new Date(value).getTime();
  if (!Number.isFinite(milliseconds)) return "Unavailable";
  const seconds = Math.max(0, Math.round(milliseconds / 1000));
  if (seconds < 60) return `${seconds}s ago`;
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${minutes}m ago`;
  return `${Math.round(minutes / 60)}h ago`;
}

function matches(opportunity, sport, query) {
  const sportMatch = sport === "All" || opportunity.sport === sport;
  const haystack = [
    opportunity.team_a,
    opportunity.team_b,
    opportunity.tournament,
    opportunity.duel_side,
    opportunity.polymarket_side,
  ]
    .join(" ")
    .toLowerCase();
  return sportMatch && (!query.trim() || haystack.includes(query.trim().toLowerCase()));
}

function StatusDot({ state }) {
  return <span aria-hidden="true" className={`status-dot status-dot--${state}`} />;
}

function StatusPill({ status }) {
  return (
    <span className={`status-pill status-pill--${status}`}>
      <StatusDot state={status === "active" ? "online" : "offline"} />
      {status === "active" ? "Active" : status === "suspended" ? "Suspended" : "Expired"}
    </span>
  );
}

function Brand() {
  return <span className="brand-lockup"><svg className="sigma-mark" viewBox="0 0 40 40" fill="none" aria-hidden="true"><path d="M29 8H10l12 12-12 12h19" stroke="currentColor" strokeWidth="3" strokeLinejoin="round"/><path d="m22 20 10-9M22 20l10 9" stroke="currentColor" strokeWidth="2" strokeLinecap="round"/></svg><span>Sigma Fork</span></span>;
}

function Navigation({ page, setPage }) {
  return <header className="app-nav">
    <button className="brand" onClick={() => setPage("overview")} aria-label="Sigma Fork home"><Brand /></button>
    <nav aria-label="Dashboard sections">
      <button aria-current={page === "overview" ? "page" : undefined} onClick={() => setPage("overview")}>Overview</button>
      <button aria-current={page === "coverage" ? "page" : undefined} onClick={() => setPage("coverage")}>Coverage</button>
      {page === "detail" && <span className="nav-current" aria-current="page">Opportunity Detail</span>}
    </nav>
    <span className="read-only-chip"><ShieldCheck size={16} />Read-only</span>
  </header>;
}

function Header({ data, onRefresh, refreshing, compact = false }) {
  const scanner = data.scanner;
  const stateLabel =
    scanner.state === "online"
      ? "Scanner online"
      : scanner.state === "demo"
        ? "Demo preview"
        : scanner.state === "degraded"
          ? "Scanner degraded"
          : "Scanner offline";
  return (
    <header className={compact ? "topbar topbar--compact" : "topbar"}>
      {!compact && <div>
        <h1>Arbitrage monitor</h1>
        <p className="product-subtitle">Duel ↔ Polymarket arbitrage monitor</p>
      </div>}
      <div className="topbar__signals">
        <span className={`scanner-state scanner-state--${scanner.state}`}>
          <StatusDot state={scanner.state} />
          {stateLabel}
        </span>
        <span className="last-cycle"><Clock3 size={16} />Last cycle {relative(scanner.last_cycle_at)}</span>
        {data.mode === "demo" && <span className="demo-badge">DEMO DATA</span>}
        <button className="icon-button" type="button" onClick={onRefresh} aria-label="Refresh dashboard">
          <RefreshCw className={refreshing ? "spinning" : ""} size={18} />
        </button>
      </div>
    </header>
  );
}

function SignalRail({ data }) {
  const items = [
    { icon: Database, label: "Cycle", value: number(data.summary.cycle_count) },
    { icon: Gauge, label: "Duel events", value: number(data.summary.duel_event_count) },
    { icon: Link2, label: "Bindings", value: number(data.summary.binding_count) },
    {
      icon: data.summary.failure_count ? AlertTriangle : Check,
      label: "Failures",
      value: number(data.summary.failure_count),
      warn: Boolean(data.summary.failure_count),
    },
    {
      icon: TimerReset,
      label: "Cycle duration",
      value: data.summary.cycle_duration_seconds
        ? `${number(data.summary.cycle_duration_seconds, 1)}s`
        : "—",
    },
  ];
  return (
    <section className="signal-rail" aria-label="Scanner cycle summary">
      {items.map(({ icon: Icon, label, value, warn }) => (
        <div className={`metric ${warn ? "metric--warn" : ""}`} key={label}>
          <Icon aria-hidden="true" size={19} />
          <div><span>{label}</span><strong>{value}</strong></div>
        </div>
      ))}
    </section>
  );
}

function Filters({ sport, setSport, query, setQuery }) {
  return (
    <div className="filters">
      <div className="sport-tabs" role="group" aria-label="Filter by sport">
        {SPORTS.map((item) => (
          <button
            className={sport === item ? "sport-tab sport-tab--active" : "sport-tab"}
            key={item}
            aria-pressed={sport === item}
            onClick={() => setSport(item)}
            type="button"
          >
            {item !== "All" && <SportMark sport={item} size={16} />}
            {item}
          </button>
        ))}
      </div>
      <label className="search-box">
        <Search aria-hidden="true" size={17} />
        <span className="sr-only">Search events</span>
        <input
          value={query}
          onChange={(event) => setQuery(event.target.value)}
          placeholder="Search teams or tournaments"
        />
        {query && (
          <button type="button" onClick={() => setQuery("")} aria-label="Clear search">
            <X size={15} />
          </button>
        )}
      </label>
    </div>
  );
}

function Edge({ value }) {
  const positive = value > 0;
  return (
    <span className={`edge ${positive ? "edge--positive" : "edge--negative"}`}>
      {positive ? <ArrowUpRight size={15} /> : <ArrowDownRight size={15} />}
      {positive ? "+" : ""}{number(value, 2)}%
    </span>
  );
}

function EmptyState({ clear, filtered }) {
  return (
    <div className="empty-state">
      <ListFilter aria-hidden="true" size={29} />
      <strong>{filtered ? "No opportunities in this view" : "No active arbitrage opportunities"}</strong>
      <p>{filtered ? "Try another sport or clear the search. Scanner rules remain unchanged." : "The scanner has not reported an alert-eligible opportunity."}</p>
      {filtered && <button type="button" onClick={clear}>Clear filters</button>}
    </div>
  );
}

function OpportunityTable({ opportunities, selectedId, setSelectedId, clear, filtered }) {
  return (
    <section className="data-panel active-panel">
      <div className="section-heading">
        <div><h2>Active opportunities</h2><span><StatusDot state="online" />{opportunities.length} detected</span></div>
        <span className="sort-label">Sorted by edge <ArrowDownRight size={14} /></span>
      </div>
      {opportunities.length === 0 ? (
        <EmptyState clear={clear} filtered={filtered} />
      ) : (
        <div className="opportunity-table" role="table" aria-label="Active arbitrage opportunities">
          <div className="opportunity-table__header" role="row">
            <span>Event</span><span>Tournament / start</span><span>Duel</span><span>Polymarket ask</span><span>Post-fee edge</span><span>Status</span>
          </div>
          {opportunities.map((item) => (
            <button
              className={selectedId === item.id ? "opportunity-row opportunity-row--selected" : "opportunity-row"}
              key={item.id}
              type="button"
              onClick={() => setSelectedId(item.id)}
              role="row"
              aria-label={`Inspect ${item.team_a} versus ${item.team_b}`}
            >
              <span className="event-cell" role="cell">
                <span className={`sport-mark sport-mark--${item.sport.replaceAll(" ", "-").toLowerCase()}`}><SportMark sport={item.sport} /></span>
                <span><strong>{item.team_a}</strong><small>vs {item.team_b}</small></span>
              </span>
              <span className="stack-cell" role="cell"><strong>{item.tournament}</strong><small>{time(item.start_time, true)}</small></span>
              <span className="stack-cell price-cell" role="cell"><strong>{item.duel_side}</strong><small>{number(item.duel_odds, 3)}</small></span>
              <span className="stack-cell price-cell" role="cell"><strong>{item.polymarket_side}</strong><small>${number(item.polymarket_price, 4)}</small></span>
              <span role="cell"><Edge value={item.edge_percent} /></span>
              <span role="cell"><StatusPill status={item.status} /></span>
              <ChevronRight className="row-chevron" size={17} />
            </button>
          ))}
        </div>
      )}
    </section>
  );
}

function RecentList({ opportunities, onOpen }) {
  const [expanded, setExpanded] = useState(false);
  const visible = expanded ? opportunities : opportunities.slice(0, 3);
  return (
    <section className="data-panel recent-panel">
      <div className="section-heading">
        <div><h2>Recent detections</h2><span>{opportunities.length} in this view</span></div>
        {opportunities.length > 3 && <button className="text-button" onClick={() => setExpanded(!expanded)} aria-expanded={expanded}>{expanded ? "Show less" : `View all (${opportunities.length})`}</button>}
      </div>
      <div className="recent-list">
        {visible.map((item) => (
          <button type="button" className="recent-row" key={item.id} onClick={() => onOpen(item.id)} aria-label={`View detection ${item.team_a} versus ${item.team_b}`}>
            <span className="recent-row__time"><strong>{relative(item.discovered_at)}</strong><small>{time(item.discovered_at)}</small></span>
            <StatusPill status={item.status} />
            <span className="recent-row__event"><SportMark sport={item.sport} size={17} /><strong>{item.team_a} <i>vs</i> {item.team_b}</strong></span>
            <span className="recent-row__tournament">{item.tournament}</span>
            <Edge value={item.edge_percent} />
          </button>
        ))}
        {opportunities.length === 0 && <p className="quiet-empty">No recent detections match these filters.</p>}
      </div>
    </section>
  );
}

function OpportunityDetail({ opportunity: item, demo, onBack }) {
  return <section className="opportunity-detail" aria-label="Opportunity Detail">
    <button type="button" className="back-button" onClick={onBack}><ArrowLeft size={18} />Back to Overview</button>
    {!item ? <div className="empty-state"><Activity size={28} /><h2>Opportunity no longer available</h2><p>This entry is no longer present in the current history.</p></div> : <>
      <div className="detail-title">
        <div className="detail-kicker"><SportMark sport={item.sport} />{item.sport}<StatusPill status={item.status} />{demo && <span className="demo-badge">DEMO DATA</span>}</div>
        <h1>{item.team_a} <span>vs</span> {item.team_b}</h1>
        <p>{item.tournament}</p>
      </div>
      <dl className="match-meta">
        <div><dt>Match starts</dt><dd>{time(item.start_time, true)}</dd></div>
        <div><dt>Detected</dt><dd>{time(item.discovered_at, true)}</dd></div>
        <div><dt>Market</dt><dd>Match Winner</dd></div>
      </dl>
      <div className="arbitrage-pair">
        <article className="provider-leg"><p className="provider-label">Duel</p><h2>{item.duel_side}</h2><strong className="hero-price">{number(item.duel_odds, 3)}</strong><span>Decimal odds</span></article>
        <article className="provider-leg"><p className="provider-label">Polymarket</p><h2>{item.polymarket_side}</h2><strong className="hero-price">${number(item.polymarket_price, 4)}</strong><span>Best ask / executable price</span></article>
        <article className="edge-feature"><p>Post-fee edge</p><Edge value={item.edge_percent} /><span>{item.status === "active" ? "Reported opportunity" : "Historical opportunity"}{demo ? " · sample data" : ""}</span></article>
      </div>
      <section className="detail-evidence" aria-label="Opportunity evidence">
        <h2>Market evidence</h2>
        <dl className="evidence-values">
          <div><dt>Best-ask liquidity</dt><dd>{number(item.best_ask_size, 2)} <small>shares</small></dd></div>
          <div><dt>Reported taker fee</dt><dd>{number(item.taker_fee, 6)} <small>included in edge</small></dd></div>
          <div><dt>Age at observation</dt><dd>{number(item.freshness_seconds, 1)} <small>seconds</small></dd></div>
        </dl>
        <div className="orientation"><h3>Outcome orientation</h3><p><span>Duel</span>{item.duel_side} to win</p><p><span>Polymarket</span>{item.polymarket_side} to win</p></div>
      </section>
      <p className="detail-readonly"><ShieldCheck size={18} />Read-only opportunity evidence. No order placement, account access, or bet controls.</p>
    </>}
  </section>;
}

function CoveragePage({ data }) {
  return (
    <div className="coverage-page">
      <div className="page-intro">
        <div><h2>Scanner coverage</h2><p>Provider collection and matching throughput by supported esport. A zero-event sport is idle, not failed.</p></div>
        <span className="read-only-chip"><ShieldCheck size={15} /> Sanitized operational data</span>
      </div>
      <div className="coverage-detail-grid">
        {data.coverage.map((item) => (
          <article className="coverage-detail" key={item.sport}>
            <header><span className={`sport-mark sport-mark--${item.sport.replaceAll(" ", "-").toLowerCase()}`}><SportMark sport={item.sport} size={22} /></span><div><h3>{item.sport}</h3><span><StatusDot state={item.status === "healthy" ? "online" : "idle"} />{item.status}</span></div></header>
            <div className="coverage-detail__metrics">
              <div><strong>{item.duel_events}</strong><span>Duel prematch</span></div>
              <div><strong>{item.bindings}</strong><span>Strict bindings</span></div>
              <div><strong>{item.evaluator_runs}</strong><span>Evaluator runs</span></div>
            </div>
            <div className={`coverage-bar coverage-bar--${Math.min(10, Math.ceil(item.duel_events / 5))}`}><span /></div>
          </article>
        ))}
      </div>
      <section className="data-panel health-detail">
        <div className="section-heading"><div><h2>Service health</h2><span>Scanner process only — no sensitive configuration</span></div></div>
        <div className="health-detail__grid">
          <div><HeartPulse size={19} /><span>State</span><strong>{data.scanner.state}</strong></div>
          <div><Clock3 size={19} /><span>Last successful cycle</span><strong>{relative(data.scanner.last_cycle_at)}</strong></div>
          <div><TimerReset size={19} /><span>Cycle duration</span><strong>{data.summary.cycle_duration_seconds ? `${number(data.summary.cycle_duration_seconds, 1)}s` : "Unavailable"}</strong></div>
          <div><AlertTriangle size={19} /><span>Consecutive failures</span><strong>{data.scanner.consecutive_failures}</strong></div>
        </div>
        <div className="last-error"><span>Last error</span><strong>{data.scanner.last_error || "No current scanner error"}</strong></div>
      </section>
    </div>
  );
}

function LoadingScreen() {
  return (
    <div className="app-shell app-shell--loading" aria-label="Loading dashboard">
      <div className="app-nav"><Brand /></div>
      <main className="main"><div className="loading-title" /><div className="loading-rail" /><div className="loading-grid"><i /><i /><i /><i /><i /><i /></div></main>
    </div>
  );
}

export default function App() {
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);
  const [refreshing, setRefreshing] = useState(false);
  const [page, setPage] = useState("overview");
  const [sport, setSport] = useState("All");
  const [query, setQuery] = useState("");
  const [selectedId, setSelectedId] = useState(null);

  const refresh = useCallback(async () => {
    const controller = new AbortController();
    setRefreshing(true);
    try {
      const next = await loadDashboard(controller.signal);
      setData(next);
      setError(null);
      setSelectedId((current) => current ?? next.active_opportunities?.[0]?.id ?? null);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Dashboard state is unavailable");
    } finally {
      setRefreshing(false);
    }
    return () => controller.abort();
  }, []);

  useEffect(() => {
    refresh();
    const interval = window.setInterval(refresh, 30_000);
    return () => window.clearInterval(interval);
  }, [refresh]);

  useEffect(() => {
    const scrollRoot = document.scrollingElement ?? document.documentElement;
    scrollRoot.scrollTop = 0;
  }, [page, selectedId]);

  const active = useMemo(
    () => (data?.active_opportunities ?? []).filter((item) => matches(item, sport, query)).sort((a, b) => b.edge_percent - a.edge_percent),
    [data, query, sport],
  );
  const recent = useMemo(
    () => (data?.recent_opportunities ?? []).filter((item) => matches(item, sport, query)),
    [data, query, sport],
  );
  const selected = [...(data?.active_opportunities ?? []), ...(data?.recent_opportunities ?? [])]
    .find((item) => item.id === selectedId) ?? null;
  const openOpportunity = (id) => { setSelectedId(id); setPage("detail"); };
  const clearFilters = () => { setSport("All"); setQuery(""); };

  if (!data && !error) return <LoadingScreen />;
  if (!data) {
    return (
      <main className="fatal-state"><WifiOff size={34} /><h1>Dashboard unavailable</h1><p>{error}</p><button type="button" onClick={refresh}>Try again</button></main>
    );
  }

  return (
    <div className="app-shell">
      <Navigation page={page} setPage={setPage} />
      <main className="main">
        <Header data={data} onRefresh={refresh} refreshing={refreshing} compact={page === "detail"} />
        {error && <div className="inline-error" role="alert"><AlertTriangle size={16} />{error}; showing the last safe snapshot.</div>}
        {page !== "detail" && <SignalRail data={data} />}
        {page === "overview" ? (
          <>
            <Filters sport={sport} setSport={setSport} query={query} setQuery={setQuery} />
            <div className="overview-grid">
              <div className="overview-flow">
                <OpportunityTable opportunities={active} selectedId={selectedId} setSelectedId={openOpportunity} clear={clearFilters} filtered={sport !== "All" || Boolean(query.trim())} />
                <RecentList opportunities={recent} onOpen={openOpportunity} />
              </div>
            </div>
          </>
        ) : page === "detail" ? (
          <OpportunityDetail opportunity={selected} demo={data.mode === "demo"} onBack={() => setPage("overview")} />
        ) : (
          <CoveragePage data={data} />
        )}
        <footer><span>Sigma Fork</span><span>No betting or trading actions.</span></footer>
      </main>
    </div>
  );
}
