import {
  Coffee,
  FilmSlate,
  ForkKnife,
  ShoppingCartSimple,
  UsersThree,
  WarningCircle,
} from "@phosphor-icons/react";
import { useCallback, useEffect, useMemo, useState } from "react";

const iconByKey = {
  coffee: Coffee,
  "film-slate": FilmSlate,
  "fork-knife": ForkKnife,
  "shopping-cart": ShoppingCartSimple,
  "users-three": UsersThree,
};

const demoAsOf = new URLSearchParams(window.location.search).get("as_of") || "2026-08-19";

function formatMoney(value, currency) {
  const amount = Number(value);
  const hasCents = Math.abs(amount % 1) > 0.0001;
  return new Intl.NumberFormat("en-US", {
    style: "currency",
    currency,
    minimumFractionDigits: hasCents ? 2 : 0,
    maximumFractionDigits: 2,
  }).format(amount);
}

function formatDateRange(start, end) {
  const parseDate = (value) => new Date(`${value}T12:00:00`);
  const format = (value) =>
    new Intl.DateTimeFormat("en-US", { month: "short", day: "numeric" }).format(
      parseDate(value),
    );
  return `${format(start)}–${format(end)}`;
}

function BudgetRow({ category }) {
  const Icon = iconByKey[category.icon] || ForkKnife;
  const spent = Number(category.spent);
  const budget = Number(category.budget);
  const over = Number(category.over);
  const progress = budget > 0 ? spent / budget : 0;
  const isOver = over > 0;
  const fillPercent = Math.min(progress * 100, 100);
  const markerPercent = isOver && progress > 0 ? Math.min((1 / progress) * 100, 96) : null;
  const detailParts = [];

  if (category.pending_count > 0) {
    detailParts.push(`${category.pending_count} pending`);
  }
  if (category.refund_count > 0) {
    detailParts.push(`${category.refund_count} refund`);
  }

  return (
    <article
      className={`budget-row ${isOver ? "budget-row--over" : ""}`}
      data-testid={`budget-row-${category.key}`}
      title={detailParts.length ? detailParts.join(" · ") : "All transactions posted"}
    >
      <div className="category-cell">
        <span className="category-icon" aria-hidden="true">
          <Icon size={30} weight="regular" />
        </span>
        <h2>{category.name}</h2>
      </div>

      <p className="window-cell">
        <span>Last {category.window_days} days</span>
        <span className="dot" aria-hidden="true">·</span>
        <span>{formatDateRange(category.window_start, category.window_end)}</span>
      </p>

      <div className="spend-cell">
        <p className="spend-label">
          <strong>{formatMoney(category.spent, category.currency)}</strong>
          <span> of {formatMoney(category.budget, category.currency)}</span>
        </p>
        <div
          className="progress-track"
          role="progressbar"
          aria-label={`${category.name} budget usage`}
          aria-valuemin="0"
          aria-valuemax={Math.max(budget, spent)}
          aria-valuenow={spent}
          aria-valuetext={`${formatMoney(spent, category.currency)} spent of ${formatMoney(
            budget,
            category.currency,
          )}`}
        >
          <span className="progress-fill" style={{ width: `${fillPercent}%` }} />
          {markerPercent !== null ? (
            <span className="budget-marker" style={{ left: `${markerPercent}%` }} />
          ) : null}
        </div>
      </div>

      <p className="balance-cell">
        {isOver
          ? `${formatMoney(over, category.currency)} over`
          : `${formatMoney(category.remaining, category.currency)} left`}
      </p>
    </article>
  );
}

function LoadingRows() {
  return Array.from({ length: 5 }, (_, index) => (
    <div className="loading-row" key={index} aria-hidden="true">
      <span className="loading-circle" />
      <span className="loading-line loading-line--name" />
      <span className="loading-line loading-line--meta" />
      <span className="loading-line loading-line--progress" />
    </div>
  ));
}

export function App() {
  const [dashboard, setDashboard] = useState(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);

  const loadDashboard = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const response = await fetch(
        `/api/v1/dashboard/budgets?as_of=${encodeURIComponent(demoAsOf)}`,
        { headers: { Accept: "application/json" } },
      );
      if (!response.ok) {
        const payload = await response.json().catch(() => ({}));
        throw new Error(payload.detail || payload.message || `API returned ${response.status}`);
      }
      setDashboard(await response.json());
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : "Unable to load budgets");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    loadDashboard();
  }, [loadDashboard]);

  const categories = useMemo(
    () => [...(dashboard?.categories || [])].sort((a, b) => a.sort_order - b.sort_order),
    [dashboard],
  );

  return (
    <main className="dashboard-page">
      <section className="budget-widget" aria-labelledby="budget-widget-title">
        <h1 id="budget-widget-title" className="sr-only">Rolling budget overview</h1>
        <p className="sr-only" aria-live="polite">
          {loading
            ? "Loading budget data"
            : error
              ? `Budget data failed to load: ${error}`
              : `${categories.length} budget categories loaded`}
        </p>

        {loading ? <LoadingRows /> : null}

        {!loading && error ? (
          <div className="state-card" role="alert">
            <WarningCircle size={34} weight="regular" aria-hidden="true" />
            <div>
              <h2>Budget data is unavailable</h2>
              <p>{error}</p>
            </div>
            <button type="button" onClick={loadDashboard}>Try again</button>
          </div>
        ) : null}

        {!loading && !error && categories.length === 0 ? (
          <div className="state-card">
            <div>
              <h2>No tracked spending yet</h2>
              <p>The next successful refresh will populate this dashboard.</p>
            </div>
          </div>
        ) : null}

        {!loading && !error
          ? categories.map((category) => (
              <BudgetRow category={category} key={category.key} />
            ))
          : null}
      </section>
    </main>
  );
}
