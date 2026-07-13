"use client";

import { useEffect, useState } from "react";
import { BarChart3, RefreshCw, TrendingUp } from "lucide-react";
import { getIncidentAnalytics } from "@/lib/api";
import type { IncidentAnalytics } from "@/lib/types";

export function IncidentAnalyticsPanel() {
  const [period, setPeriod] = useState<"day" | "week" | "month">("week");
  const [data, setData] = useState<IncidentAnalytics | null>(null);
  const periodLabel = period === "day" ? "Last 24 hours" : period === "week" ? "Last 7 days" : "Last 30 days";

  useEffect(() => {
    // Add a short debounce to avoid rapid consecutive fetches when users quickly switch periods.
    const timer = setTimeout(() => {
      // Reset data to show loading state while fetching.
      setData(null);
      getIncidentAnalytics(period).then(setData).catch(() => setData(null));
    }, 500); // 500ms buffer – adjust as needed for perceived responsiveness.

    return () => clearTimeout(timer);
  }, [period]);

  return (
    <section className="content-section analytics-panel">
      <div className="section-heading">
        <div>
          <p className="eyebrow">Resolution Intelligence</p>
          <h2>Incident patterns and recurring impact</h2>
          <p className="analytics-subtitle">
            {periodLabel} operational view with live incidents plus synthetic training data when history is sparse.
          </p>
        </div>
        <div className="period-tabs">
          {(["day", "week", "month"] as const).map((value) => (
            <button className={period === value ? "active" : ""} key={value} onClick={() => setPeriod(value)}>
              {value}
            </button>
          ))}
        </div>
      </div>

      {!data ? (
        <div className="empty-state">Analytics will appear when the API is available.</div>
      ) : (
        <>
          <div className="analytics-metrics">
            <div><BarChart3 /><strong>{data.total}</strong><span>Incidents in window</span></div>
            <div><RefreshCw /><strong>{data.recurring.length}</strong><span>Recurring patterns</span></div>
            <div><TrendingUp /><strong>${data.impact_per_minute.toLocaleString()}</strong><span>Impact / min</span></div>
          </div>

          <div className="analytics-summary-strip">
            <span><strong>{data.active}</strong> active investigations</span>
            <span><strong>{data.resolved}</strong> resolved in this window</span>
            <span><strong>{data.synthetic_count || 0}</strong> synthetic incidents backing the trend line</span>
          </div>

          <div className="analytics-columns">
            <div>
              <h3>Resolution buckets</h3>
              {data.resolution_buckets.map((item) => (
                <div className="bar-row" key={item.label}>
                  <span>{item.label}</span>
                  <div><i style={{ width: `${Math.max(8, item.count / Math.max(1, data.total) * 100)}%` }} /></div>
                  <strong>{item.count}</strong>
                </div>
              ))}
            </div>

            <div>
              <h3>Recurring incidents</h3>
              {data.recurring.length === 0 ? (
                <p className="muted">No repeated root-cause signatures in this window.</p>
              ) : (
                data.recurring.slice(0, 4).map((item) => (
                  <div className="recurrence-row" key={item.signature}>
                    <div><strong>{item.service}</strong><span>{item.resolution}</span></div>
                    <b>{item.count}×</b>
                    <small>${item.impact_per_minute}/min</small>
                  </div>
                ))
              )}
            </div>
          </div>

          <div className="analytics-service-strip">
            <h3>Top affected services</h3>
            <div className="service-chip-row">
              {data.service_buckets.slice(0, 6).map((item) => (
                <span key={item.label} className="service-chip">
                  <strong>{item.label}</strong>
                  <small>{item.count} incident{item.count === 1 ? "" : "s"}</small>
                </span>
              ))}
            </div>
          </div>
        </>
      )}
    </section>
  );
}
