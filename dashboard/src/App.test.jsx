import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import App from "./App";

const payload = {
  mode: "demo",
  scanner: {
    state: "demo",
    online: false,
    last_cycle_at: "2026-09-20T14:31:50Z",
    cycle_count: 12847,
    failure_count: 0,
    consecutive_failures: 0,
    last_error: null,
  },
  summary: {
    cycle_count: 12847,
    duel_event_count: 63,
    binding_count: 9,
    evaluator_runs: 9,
    failure_count: 0,
    active_opportunities: 2,
    cycle_duration_seconds: 8.4,
  },
  coverage: [
    { sport: "CS2", duel_events: 17, bindings: 3, evaluator_runs: 3, status: "healthy" },
    { sport: "Dota 2", duel_events: 12, bindings: 2, evaluator_runs: 2, status: "healthy" },
    { sport: "Valorant", duel_events: 15, bindings: 2, evaluator_runs: 2, status: "healthy" },
    { sport: "League of Legends", duel_events: 19, bindings: 2, evaluator_runs: 2, status: "healthy" },
  ],
  active_opportunities: [
    {
      id: "lol",
      sport: "League of Legends",
      team_a: "Invictus Gaming",
      team_b: "JD Gaming",
      tournament: "LPL 2026",
      start_time: "2026-09-20T16:00:00Z",
      duel_side: "JD Gaming",
      duel_odds: 2.38,
      polymarket_side: "Invictus Gaming",
      polymarket_price: 0.405,
      edge_percent: 3.18,
      discovered_at: "2026-09-20T14:29:58Z",
      status: "active",
      best_ask_size: 120,
      taker_fee: 0.01,
      freshness_seconds: 12,
    },
    {
      id: "cs2",
      sport: "CS2",
      team_a: "Vitality",
      team_b: "FURIA",
      tournament: "BLAST Open",
      start_time: "2026-09-20T18:30:00Z",
      duel_side: "FURIA",
      duel_odds: 2.12,
      polymarket_side: "Vitality",
      polymarket_price: 0.458,
      edge_percent: 1.42,
      discovered_at: "2026-09-20T14:26:31Z",
      status: "active",
      best_ask_size: 80,
      taker_fee: 0.01,
      freshness_seconds: 14,
    },
  ],
  recent_opportunities: [],
};

describe("dashboard", () => {
  beforeEach(() => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({ ok: true, json: async () => payload }),
    );
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("marks sample data and renders read-only opportunities", async () => {
    render(<App />);

    expect(await screen.findByText("Arbitrage monitor")).toBeInTheDocument();
    expect(screen.getByText("DEMO DATA")).toBeInTheDocument();
    expect(screen.getAllByText("Invictus Gaming").length).toBeGreaterThan(0);
    expect(screen.getByText("No betting or trading actions.")).toBeInTheDocument();
  });

  it("filters opportunities by sport without changing scanner state", async () => {
    render(<App />);
    await screen.findByText("Arbitrage monitor");

    fireEvent.click(screen.getByRole("button", { name: "CS2" }));

    await waitFor(() => {
      expect(screen.queryByLabelText("Inspect Invictus Gaming versus JD Gaming")).not.toBeInTheDocument();
    });
    expect(screen.getByLabelText("Inspect Vitality versus FURIA")).toBeInTheDocument();
  });

  it("opens the coverage view", async () => {
    render(<App />);
    await screen.findByText("Arbitrage monitor");

    fireEvent.click(screen.getByRole("button", { name: "Coverage" }));

    expect(screen.getByText("Scanner coverage")).toBeInTheDocument();
    expect(screen.getAllByText("Duel prematch")).toHaveLength(4);
  });

  it("opens a dedicated detail view and restores overview filters on return", async () => {
    render(<App />);
    await screen.findByText("Arbitrage monitor");
    expect(screen.queryByLabelText("Opportunity Detail")).not.toBeInTheDocument();
    expect(screen.queryByText("Coverage preview")).not.toBeInTheDocument();
    expect(screen.queryByText("realtime-v3")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "CS2", exact: true }));
    fireEvent.change(screen.getByRole("textbox", { name: "Search events" }), { target: { value: "FURIA" } });
    document.documentElement.scrollTop = 640;
    fireEvent.click(screen.getByLabelText("Inspect Vitality versus FURIA"));
    expect(screen.getByLabelText("Opportunity Detail")).toBeInTheDocument();
    expect(document.documentElement.scrollTop).toBe(0);
    expect(screen.getByRole("heading", { name: "Vitality vs FURIA" })).toBeInTheDocument();
    expect(screen.queryByText("Active opportunities")).not.toBeInTheDocument();
    expect(screen.getByText("Best ask / executable price")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Back to Overview" }));
    expect(screen.getByRole("textbox", { name: "Search events" })).toHaveValue("FURIA");
    expect(screen.getByRole("button", { name: "CS2", exact: true })).toHaveAttribute("aria-pressed", "true");
    expect(screen.queryByLabelText("Inspect Invictus Gaming versus JD Gaming")).not.toBeInTheDocument();
  });

  it("opens expired history after View all without showing it as active", async () => {
    const history = Array.from({ length: 4 }, (_, index) => ({
      ...payload.active_opportunities[1], id: `history-${index}`,
      team_a: `Historical team ${index}`, status: "expired",
    }));
    fetch.mockResolvedValue({ ok: true, json: async () => ({ ...payload, recent_opportunities: history }) });
    render(<App />);
    await screen.findByText("Arbitrage monitor");
    expect(screen.queryByRole("button", { name: "View detection Historical team 3 versus FURIA" })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "View all (4)" }));
    fireEvent.click(screen.getByRole("button", { name: "View detection Historical team 3 versus FURIA" }));
    expect(screen.getByRole("heading", { name: "Historical team 3 vs FURIA" })).toBeInTheDocument();
    expect(screen.getByText("Expired")).toBeInTheDocument();
    expect(screen.queryByText("Open · fresh")).not.toBeInTheDocument();
  });

  it("keeps search empty-state recovery available", async () => {
    render(<App />);
    await screen.findByText("Arbitrage monitor");
    fireEvent.change(screen.getByRole("textbox", { name: "Search events" }), { target: { value: "no-such-team" } });
    expect(screen.getByText("No opportunities in this view")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Clear filters" }));
    expect(screen.getByLabelText("Inspect Vitality versus FURIA")).toBeInTheDocument();
  });

  it("shows an honest live empty state without demo labels", async () => {
    fetch.mockResolvedValue({
      ok: true,
      json: async () => ({
        ...payload,
        mode: "live",
        active_opportunities: [],
        recent_opportunities: [],
        summary: { ...payload.summary, active_opportunities: 0 },
      }),
    });

    render(<App />);

    expect(await screen.findByText("No active arbitrage opportunities")).toBeInTheDocument();
    expect(screen.queryByText("DEMO DATA")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Clear filters" })).not.toBeInTheDocument();
  });
});
