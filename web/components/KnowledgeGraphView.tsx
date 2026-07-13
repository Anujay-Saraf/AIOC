"use client";

import { useEffect, useMemo, useState } from "react";
import { Maximize2, Network, ZoomIn, ZoomOut } from "lucide-react";
import { getKnowledgeGraph } from "@/lib/api";
import type { KnowledgeGraph } from "@/lib/types";

const COLORS: Record<string, string> = {
  incident: "#ff6b5f",
  service: "#42d3bd",
  cause: "#f5b942",
  resolution: "#8b7cf6",
  evidence: "#0ea5e9",
  entity: "#64748b"
};

export function KnowledgeGraphView() {
  const [graph, setGraph] = useState<KnowledgeGraph>({ nodes: [], edges: [] });
  const [zoom, setZoom] = useState(1);
  const [selected, setSelected] = useState<KnowledgeGraph["nodes"][number] | null>(null);

  useEffect(() => {
    getKnowledgeGraph().then(setGraph).catch(() => undefined);
  }, []);

  const positioned = useMemo(() => graph.nodes.map((node, index) => {
    const angle = index / Math.max(1, graph.nodes.length) * Math.PI * 2;
    const ring = node.type === "incident" ? 135 : node.type === "service" ? 235 : 315;
    return { ...node, x: 450 + Math.cos(angle) * ring, y: 350 + Math.sin(angle) * ring };
  }), [graph]);
  const byId = useMemo(() => Object.fromEntries(positioned.map((node) => [node.id, node])), [positioned]);

  return (
    <div className="screen-stack">
      <section className="hero-panel graph-hero">
        <div>
          <p className="eyebrow">Incident Ontology</p>
          <h1>See how signals, causes and resolutions connect.</h1>
          <p className="hero-copy">Explore recurring patterns, hover for context, and zoom into relationships across the incident graph.</p>
        </div>
        <Network size={64} />
      </section>

      <section className="content-section graph-section">
        <div className="graph-toolbar">
          <div className="graph-legend">
            {Object.entries(COLORS).map(([key, color]) => <span key={key}><i style={{ background: color }} />{key}</span>)}
            {graph.backend && <span><i style={{ background: "#0f172a" }} />{graph.backend}</span>}
          </div>
          <div className="button-row">
            <button className="ghost-button" onClick={() => setZoom((value) => Math.max(.5, value - .15))}><ZoomOut size={16} /></button>
            <button className="ghost-button" onClick={() => setZoom(1)}><Maximize2 size={16} /> Reset</button>
            <button className="ghost-button" onClick={() => setZoom((value) => Math.min(2.4, value + .15))}><ZoomIn size={16} /></button>
          </div>
        </div>

        <div className="graph-canvas" onWheel={(event) => {
          event.preventDefault();
          setZoom((value) => Math.min(2.4, Math.max(.5, value + (event.deltaY < 0 ? .1 : -.1))));
        }}>
          {positioned.length === 0 ? (
            <div className="graph-empty">Trigger incidents to build the live ontology.</div>
          ) : (
            <svg viewBox="0 0 900 700">
              <g style={{ transform: `scale(${zoom})`, transformOrigin: "450px 350px" }}>
                {graph.edges.map((edge, index) => {
                  const source = byId[edge.source];
                  const target = byId[edge.target];
                  return source && target ? (
                    <g key={index}>
                      <line x1={source.x} y1={source.y} x2={target.x} y2={target.y} />
                      <text x={(source.x + target.x) / 2} y={(source.y + target.y) / 2}>{edge.relation}</text>
                    </g>
                  ) : null;
                })}
                {positioned.map((node) => (
                  <g className="graph-node" key={node.id} transform={`translate(${node.x},${node.y})`} onClick={() => setSelected(node)}>
                    <circle r={node.type === "incident" ? 27 : 21} fill={COLORS[node.type] || COLORS.entity} />
                    <text y={42}>{node.label.length > 28 ? `${node.label.slice(0, 28)}...` : node.label}</text>
                    <title>{node.detail || node.label}</title>
                  </g>
                ))}
              </g>
            </svg>
          )}
          {selected && (
            <aside className="node-inspector">
              <button onClick={() => setSelected(null)}>x</button>
              <span>{selected.type}</span>
              <h3>{selected.label}</h3>
              <p>{selected.detail || selected.status || "Connected operational entity"}</p>
              {selected.impact_per_minute !== undefined && <p>${selected.impact_per_minute}/min impact</p>}
              <code>{selected.id}</code>
              <pre className="node-metadata">{JSON.stringify(
                Object.fromEntries(
                  Object.entries(selected).filter(
                    ([key]) => !["id", "type", "label", "detail", "status", "impact_per_minute"].includes(key),
                  ),
                ),
                null,
                2,
              )}</pre>
            </aside>
          )}
        </div>
      </section>
    </div>
  );
}
