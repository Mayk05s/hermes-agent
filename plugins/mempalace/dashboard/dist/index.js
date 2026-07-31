/**
 * Hermes MemPalace dashboard plugin.
 *
 * Plain IIFE using the dashboard plugin SDK.
 */
(function () {
  "use strict";

  const SDK = window.__HERMES_PLUGIN_SDK__;
  if (!SDK || !window.__HERMES_PLUGINS__) return;

  const { React, fetchJSON } = SDK;
  const h = React.createElement;
  const { useCallback, useEffect, useMemo, useRef, useState } = SDK.hooks;
  const Button = SDK.components.Button || "button";
  const Input = SDK.components.Input || "input";
  const Badge = SDK.components.Badge || "span";

  const API = "/api/plugins/mempalace";

  function parseError(err) {
    const raw = err && err.message ? String(err.message) : String(err || "");
    const match = raw.match(/^(\d{3}):\s*(.*)$/s);
    const body = match ? match[2] : raw;
    try {
      const parsed = JSON.parse(body);
      return typeof parsed.detail === "string" ? parsed.detail : body;
    } catch (_e) {
      return body;
    }
  }

  function fmt(n) {
    if (n === null || n === undefined) return "0";
    return Number(n).toLocaleString();
  }

  function autoCleanText(data) {
    const raw = data && data.auto_clean;
    const nested = data && Array.isArray(data.profiles)
      ? data.profiles.flatMap((row) => Array.isArray(row.auto_clean) ? row.auto_clean : row.auto_clean ? [row.auto_clean] : [])
      : [];
    const rows = Array.isArray(raw) ? raw : raw ? [raw] : nested;
    const active = rows.filter((row) => row && row.enabled);
    if (!active.length) return "";
    const skipped = active.filter((row) => row.skipped);
    if (skipped.length) {
      const reason = skipped[0].skip_reason || "limit reached";
      return ` Auto-clean skipped for ${fmt(skipped.length)} scope(s): ${reason}.`;
    }
    const deletedEntities = active.reduce((sum, row) => sum + Number(row.deleted_entities || 0), 0);
    const deletedTriples = active.reduce((sum, row) => sum + Number(row.deleted_triples || 0), 0);
    const deletedOrphans = active.reduce((sum, row) => sum + Number(row.deleted_orphans || 0), 0);
    if (!deletedEntities && !deletedTriples && !deletedOrphans) {
      return " Auto-clean: no noise.";
    }
    return ` Auto-clean removed ${fmt(deletedEntities)} node(s), ${fmt(deletedTriples)} triple(s), ${fmt(deletedOrphans)} orphan literal(s).`;
  }

  function typeClass(type) {
    const t = String(type || "unknown").toLowerCase();
    if (t.includes("profile")) return "mp-node-profile";
    if (t.includes("topic")) return "mp-node-topic";
    if (t.includes("cluster") || t.includes("area")) return "mp-node-cluster";
    if (t.includes("person")) return "mp-node-person";
    if (t.includes("project")) return "mp-node-project";
    if (t.includes("organization") || t.includes("service")) return "mp-node-org";
    if (t.includes("medical") || t.includes("diagnosis") || t.includes("medication")) return "mp-node-med";
    return "mp-node-concept";
  }

  function shorten(text, max) {
    const s = String(text || "");
    return s.length > max ? s.slice(0, max - 1) + "..." : s;
  }

  function shortTime(value) {
    const raw = String(value || "");
    const match = raw.match(/T(\d{2}:\d{2}:\d{2})/);
    if (match) return match[1];
    return raw.replace("T", " ").replace(/(?:Z|\+00:00)$/, "").slice(0, 19) || "—";
  }

  function shortDateTime(value) {
    const raw = String(value || "");
    return raw.replace("T", " ").replace(/(?:Z|\+00:00)$/, "").slice(0, 19) || "—";
  }

  function relativeTime(value) {
    const raw = String(value || "");
    const ts = raw ? Date.parse(raw) : 0;
    if (!ts) return "—";
    const diffSeconds = Math.round((Date.now() - ts) / 1000);
    const future = diffSeconds < 0;
    const abs = Math.abs(diffSeconds);
    let text;
    if (abs < 60) {
      text = future ? "in <1 min" : "<1 min ago";
    } else if (abs < 3600) {
      const mins = Math.max(1, Math.round(abs / 60));
      text = future ? `in ${mins} min` : `${mins} min ago`;
    } else if (abs < 86400) {
      const hours = Math.max(1, Math.round(abs / 3600));
      text = future ? `in ${hours} h` : `${hours} h ago`;
    } else {
      const days = Math.max(1, Math.round(abs / 86400));
      text = future ? `in ${days} d` : `${days} d ago`;
    }
    return text;
  }

  function statusTone(status) {
    const value = String(status || "").toLowerCase();
    if (value.includes("error") || value.includes("fail") || value.includes("stale") || value.includes("stall")) return "error";
    if (value.includes("pending") || value.includes("queue") || value.includes("resume") || value.includes("needed") || value.includes("warn")) return "pending";
    if (value.includes("run") || value.includes("extract") || value.includes("validat")) return "running";
    return "done";
  }

  function statusLabel(status) {
    const tone = statusTone(status);
    if (tone === "error") return "Error";
    if (tone === "running") return "Running";
    const value = String(status || "").toLowerCase();
    if (value.includes("queue")) return "Queue";
    if (tone === "pending") return value.includes("warn") ? "Warning" : "Pending";
    if (value.includes("idle")) return "Idle";
    if (value.includes("skip")) return "Skipped";
    return "Done";
  }

  function isNoIssueValidationEvent(event) {
    const message = String((event && event.message) || "").toLowerCase();
    const task = String((event && event.model_task) || "").toLowerCase();
    const candidates = Number((event && (event.candidates ?? event.total_candidates)) || 0);
    return task === "mempalace_validator" && candidates === 0 && (message.includes("validation skipped") || message.includes("validation clean") || message.includes("no cleanup"));
  }

  function eventTone(event) {
    const message = String((event && event.message) || "").toLowerCase();
    const value = String((event && (event.status || event.level)) || "").toLowerCase();
    if (isNoIssueValidationEvent(event)) return "done";
    if (message.includes("error") || value.includes("error") || value.includes("fail")) return "error";
    if (message.includes("warning") || value.includes("warning") || value.includes("warn")) return "pending";
    if (message.includes("started")) return "running";
    if (message.includes("finished") || message.includes("processed") || message.includes("skipped") || value.includes("success")) return "done";
    return statusTone(value || "done");
  }

  function eventLabel(event) {
    const message = String((event && event.message) || "").toLowerCase();
    const value = String((event && (event.status || event.level)) || "").toLowerCase();
    if (isNoIssueValidationEvent(event)) return "No issues";
    if (message.includes("started")) return "Started";
    if (message.includes("finished")) return "Finished";
    if (message.includes("processed")) return "Batch";
    if (message.includes("skipped")) return "Skipped";
    if (value.includes("warning") || value.includes("warn")) return "Warning";
    return statusLabel(value || "done");
  }

  function eventMessage(event) {
    if (isNoIssueValidationEvent(event)) return "MemPalace validation clean: no cleanup candidates";
    return (event && event.message) || "—";
  }

  function titleCaseWords(value) {
    return String(value || "process")
      .replace(/_/g, " ")
      .split(/\s+/)
      .filter(Boolean)
      .map((word) => word.slice(0, 1).toUpperCase() + word.slice(1))
      .join(" ");
  }

  function hashText(text) {
    let hash = 0;
    const value = String(text || "");
    for (let i = 0; i < value.length; i += 1) {
      hash = (hash << 5) - hash + value.charCodeAt(i);
      hash |= 0;
    }
    return Math.abs(hash);
  }

  function topicLabel(name) {
    const raw = String(name || "");
    const aliases = {
      hermes_profile: "Hermes Profile",
      telegram_assistant: "Assistant",
      telegram_boxmap: "BoxMap",
      telegram_family: "Family",
      telegram_health: "Health",
      telegram_homeassistant: "Home Assistant",
      telegram_linkedin: "LinkedIn",
      telegram_main: "Main",
      telegram_planning: "Planning",
      telegram_system: "System",
      telegram_tripio: "Tripio",
      telegram_cv: "CV",
    };
    if (aliases[raw]) return aliases[raw];
    return raw
      .replace(/^telegram[_-]/, "")
      .replace(/[_-]+/g, " ")
      .replace(/\b\w/g, (letter) => letter.toUpperCase());
  }

  function buildTopicGraph(profile, palaces, stats) {
    const rows = [...(palaces || [])].sort((a, b) => {
      const weightA = Number(a.entity_count || 0) + Number(a.triple_count || 0) * 0.35;
      const weightB = Number(b.entity_count || 0) + Number(b.triple_count || 0) * 0.35;
      return weightB - weightA || String(a.palace || "").localeCompare(String(b.palace || ""));
    });
    const rootId = `profile:${profile}`;
    const totalEntities = rows.reduce((sum, row) => sum + Number(row.entity_count || 0), 0);
    const totalTriples = rows.reduce((sum, row) => sum + Number(row.triple_count || 0), 0);
    const nodes = [
      {
        id: rootId,
        label: profile || "profile",
        type: "profile",
        kind: "profile",
        degree: rows.length,
        description: `${fmt(rows.length)} palaces · ${fmt(totalEntities)} entities · ${fmt(totalTriples)} triples`,
        attributes: [],
      },
      ...rows.map((row) => ({
        id: `topic:${row.palace}`,
        label: topicLabel(row.palace),
        type: "topic",
        kind: "topic",
        palace: row.palace,
        source_id: row.palace,
        degree: Number(row.entity_count || 0) + Number(row.triple_count || 0),
        description: `${fmt(row.entity_count)} entities · ${fmt(row.triple_count)} triples`,
        entity_count: Number(row.entity_count || 0),
        triple_count: Number(row.triple_count || 0),
        node_count: Number(row.node_count || 0),
        edge_count: Number(row.edge_count || 0),
        attributes: (row.source_counts || []).slice(0, 6).map((item) => ({
          predicate: "source",
          value: `${item.source}: ${fmt(item.count)}`,
        })),
      })),
    ];
    const edges = rows.map((row) => ({
      id: `topic-edge:${row.palace}`,
      source: rootId,
      target: `topic:${row.palace}`,
      label: "contains",
      confidence: 1,
      palace: row.palace,
    }));
    return {
      profile,
      palace: "",
      topic: "",
      view_mode: "topics",
      nodes,
      edges,
      node_count: nodes.length,
      edge_count: edges.length,
      stats: stats || {},
    };
  }

  const CLUSTERS = [
    { key: "security", label: "Security", words: ["security", "охрана", "guard", "alarm", "сигнал", "lock", "замок", "motion", "door", "water open", "water close", "кран", "leak", "датчик протечки"] },
    { key: "automation", label: "Automation", words: ["automation", "automations", "script", "scene", "trigger", "input_boolean", "input_button", "helper", "tap", "schedule", "планирование", "routine", "event"] },
    { key: "devices", label: "Devices", words: ["dishwasher", "посудом", "projector", "yandex station", "яндекс стан", "бризер", "robot", "appliance", "бытовая техника", "device", "switch", "sensor"] },
    { key: "climate", label: "Climate", words: ["climate", "air", "бризер", "temperature", "humidity", "влажн", "термо", "fan", "heat", "cool"] },
    { key: "covers", label: "Covers", words: ["curtain", "cover", "штор", "жалюзи", "ролет", "blind"] },
    { key: "lighting", label: "Lighting", words: ["light", "lamp", "свет", "подсвет", "led", "brightness"] },
    { key: "health", label: "Health", words: ["health", "fitness", "sleep", "сон", "weight", "nutrition", "stats", "здоров"] },
    { key: "config", label: "Config/Files", words: ["yaml", "json", "config", "configuration", "database", "postgres", "db", "api", "mcp", "backend", "panel", "dashboard", "file", "homeassistant-config", "sync", "ssh", "claude"] },
    { key: "people", label: "People/Bots", words: ["mikhail", "михаил", "tripioo", "bot", "telegram", "наташа", "person"] },
    { key: "other", label: "Other", words: [] },
  ];

  const LOW_SIGNAL_PREDICATES = new Set([
    "status",
    "state",
    "enabled",
    "deleted",
    "reported",
    "updated",
    "closed",
    "ready",
    "done",
    "prepared",
    "uses_model",
    "model",
  ]);

  function attrSource(attr) {
    if (!attr) return "";
    return attr.source_file || attr.source_closet || attr.adapter_name || attr.extracted_at || "";
  }

  function qualityForNode(node, edges) {
    if (!node || node.kind || String(node.type || "").toLowerCase() === "topic") {
      return { noise: false, reason: "" };
    }
    const linkedEdges = (edges || []).filter((edge) => edge.source === node.id || edge.target === node.id);
    const attrs = node.attributes || [];
    const predicates = [
      ...linkedEdges.map((edge) => String(edge.label || "").toLowerCase()),
      ...attrs.map((attr) => String(attr.predicate || "").toLowerCase()),
    ].filter(Boolean);
    const hasStrongRelation = linkedEdges.some((edge) => !LOW_SIGNAL_PREDICATES.has(String(edge.label || "").toLowerCase()));
    const hasSourceFile = attrs.some((attr) => attr.source_file) || Boolean(node.properties && node.properties.source_file);
    const degree = Number(node.degree || 0);
    const onlyLowSignal = predicates.length > 0 && predicates.every((predicate) => LOW_SIGNAL_PREDICATES.has(predicate));
    const literalStatus = attrs.some((attr) => {
      const value = String(attr.value || "").toLowerCase();
      return LOW_SIGNAL_PREDICATES.has(String(attr.predicate || "").toLowerCase()) && /готов|ready|sonnet|true|false|ok|done/.test(value);
    });
    if (degree <= 2 && onlyLowSignal && !hasStrongRelation && !hasSourceFile) {
      return { noise: true, reason: "only transient status facts" };
    }
    if (degree <= 1 && literalStatus && !hasSourceFile) {
      return { noise: true, reason: "single transient status" };
    }
    return { noise: false, reason: "" };
  }

  function withQuality(nodes, edges) {
    return (nodes || []).map((node) => {
      const quality = qualityForNode(node, edges);
      return quality.noise ? { ...node, noise: true, quality_reason: quality.reason } : node;
    });
  }

  function clusterForNode(node, edges) {
    const edgeText = (edges || [])
      .filter((edge) => edge.source === node.id || edge.target === node.id)
      .slice(0, 12)
      .map((edge) => `${edge.label || ""} ${edge.palace || ""}`)
      .join(" ");
    const attrText = (node.attributes || []).map((attr) => `${attr.predicate || ""} ${attr.value || ""}`).join(" ");
    const text = `${node.label || ""} ${node.id || ""} ${node.type || ""} ${node.description || ""} ${attrText} ${edgeText}`.toLowerCase();
    for (const cluster of CLUSTERS) {
      if (cluster.key === "other") continue;
      if (cluster.words.some((word) => text.includes(word))) return cluster;
    }
    return CLUSTERS[CLUSTERS.length - 1];
  }

  function buildAreaGraph(rawGraph, palace) {
    const sourceNodes = withQuality((rawGraph && rawGraph.nodes) || [], (rawGraph && rawGraph.edges) || []);
    const sourceEdges = (rawGraph && rawGraph.edges) || [];
    const cleanNodes = sourceNodes.filter((node) => !node.noise);
    const grouped = new Map();
    cleanNodes.forEach((node) => {
      const cluster = clusterForNode(node, sourceEdges);
      const row = grouped.get(cluster.key) || { cluster, nodes: [], degree: 0 };
      row.nodes.push(node);
      row.degree += Number(node.degree || 0) + 1;
      grouped.set(cluster.key, row);
    });
    const rows = [...grouped.values()]
      .filter((row) => row.nodes.length)
      .sort((a, b) => b.degree - a.degree || a.cluster.label.localeCompare(b.cluster.label));
    const rootId = `topic-root:${palace}`;
    const nodes = [
      {
        id: rootId,
        label: topicLabel(palace),
        type: "topic",
        kind: "topic-root",
        palace,
        degree: rows.length,
        description: `${fmt(cleanNodes.length)} clean entities · ${fmt(sourceNodes.length - cleanNodes.length)} hidden low-signal · ${fmt(sourceEdges.length)} visible relations`,
        attributes: [],
      },
      ...rows.map((row) => ({
        id: `cluster:${palace}:${row.cluster.key}`,
        label: row.cluster.label,
        type: "cluster",
        kind: "cluster",
        palace,
        cluster: row.cluster.key,
        degree: row.degree,
        entity_count: row.nodes.length,
        description: `${fmt(row.nodes.length)} entities in ${topicLabel(palace)}`,
        attributes: row.nodes.slice(0, 8).map((node) => ({
          predicate: "contains",
          value: node.label,
        })),
      })),
    ];
    const edges = rows.map((row) => ({
      id: `cluster-edge:${palace}:${row.cluster.key}`,
      source: rootId,
      target: `cluster:${palace}:${row.cluster.key}`,
      label: "contains",
      confidence: 1,
      palace,
    }));
    return {
      ...(rawGraph || {}),
      view_mode: "clusters",
      nodes,
      edges,
      node_count: nodes.length,
      edge_count: edges.length,
    };
  }

  function buildClusterEntityGraph(rawGraph, clusterKey) {
    const sourceEdges = (rawGraph && rawGraph.edges) || [];
    const sourceNodes = withQuality((rawGraph && rawGraph.nodes) || [], sourceEdges);
    const rows = sourceNodes
      .filter((node) => clusterForNode(node, sourceEdges).key === clusterKey)
      .sort((a, b) => Number(b.degree || 0) - Number(a.degree || 0));
    const selected = new Set(rows.map((node) => node.id));
    const realEdges = sourceEdges.filter((edge) => selected.has(edge.source) && selected.has(edge.target));
    const cluster = CLUSTERS.find((item) => item.key === clusterKey) || CLUSTERS[CLUSTERS.length - 1];
    const rootId = `cluster-root:${rawGraph && rawGraph.palace || "topic"}:${clusterKey}`;
    const root = {
      id: rootId,
      label: cluster.label,
      type: "cluster",
      kind: "cluster-root",
      palace: rawGraph && rawGraph.palace,
      cluster: clusterKey,
      degree: rows.length,
      description: `${fmt(rows.length)} entities in ${cluster.label}`,
      attributes: rows.slice(0, 10).map((node) => ({ predicate: "contains", value: node.label })),
    };
    const containsEdges = rows.map((node) => ({
      id: `cluster-contains:${clusterKey}:${node.id}`,
      source: rootId,
      target: node.id,
      label: "contains",
      confidence: 1,
      palace: rawGraph && rawGraph.palace,
      synthetic: true,
    }));
    return {
      ...(rawGraph || {}),
      view_mode: "cluster_entities",
      cluster: clusterKey,
      cluster_label: cluster.label,
      nodes: [root, ...rows],
      edges: [...containsEdges, ...realEdges],
      node_count: rows.length + 1,
      edge_count: containsEdges.length + realEdges.length,
    };
  }

  function layoutGraph(graph) {
    const nodes = (graph && graph.nodes) || [];
    const edges = (graph && graph.edges) || [];
    const width = 1500;
    const height = 920;
    const cx = width / 2;
    const cy = height / 2;
    if (graph && (graph.view_mode === "topics" || graph.view_mode === "clusters")) {
      const positions = new Map();
      const radialNodes = nodes.filter((node) => graph.view_mode === "clusters" ? node.kind === "cluster" : node.kind === "topic");
      const maxWeight = radialNodes.reduce((max, node) => Math.max(max, Number(node.degree || 0)), 1);
      nodes.forEach((node) => {
        if (node.kind === "profile" || node.kind === "topic-root") {
          positions.set(node.id, { x: cx, y: cy, r: 48, rank: 0, isCore: true });
        }
      });
      radialNodes.forEach((node, index) => {
        const total = Math.max(radialNodes.length, 1);
        const ring = total <= 8 ? 0 : Math.floor(index / 8);
        const perRing = total <= 8 ? total : ring === 0 ? 8 : Math.max(total - 8, 1);
        const ringIndex = total <= 8 ? index : ring === 0 ? index : index - 8;
        const angle = -Math.PI / 2 + (ringIndex / perRing) * Math.PI * 2 + (ring * 0.23);
        const radius = total <= 8 ? 280 : ring === 0 ? 255 : 405;
        const weight = Math.min(Number(node.degree || 0) / maxWeight, 1);
        positions.set(node.id, {
          x: cx + Math.cos(angle) * radius,
          y: cy + Math.sin(angle) * radius,
          r: 28 + Math.sqrt(weight) * 34,
          rank: index + 1,
          isCore: true,
        });
      });
      return {
        width,
        height,
        nodes,
        edges,
        positions,
        coreLimit: nodes.length,
        signature: `topics:${nodes.map((node) => `${node.id}:${node.degree || 0}`).join("|")}`,
      };
    }
    const ranked = [...nodes].sort((a, b) => {
      const degreeDiff = Number(b.degree || 0) - Number(a.degree || 0);
      if (degreeDiff) return degreeDiff;
      return String(a.label || a.id).localeCompare(String(b.label || b.id));
    });
    const rankById = new Map(ranked.map((node, index) => [node.id, index]));
    const maxDegree = ranked.reduce((m, n) => Math.max(m, Number(n.degree || 0)), 1);
    const coreLimit = Math.min(Math.max(34, Math.round(Math.sqrt(Math.max(nodes.length, 1)) * 5.2)), 86);
    const positions = new Map();
    const neighborMap = new Map();
    edges.forEach((edge) => {
      if (!neighborMap.has(edge.source)) neighborMap.set(edge.source, []);
      if (!neighborMap.has(edge.target)) neighborMap.set(edge.target, []);
      neighborMap.get(edge.source).push(edge.target);
      neighborMap.get(edge.target).push(edge.source);
    });

    ranked.forEach((node, index) => {
      const angle = index * 2.399963229728653 + (hashText(node.id) % 360) * 0.002;
      const degreeRatio = Math.min(Number(node.degree || 0) / maxDegree, 1);
      const size = 9 + Math.log2(Number(node.degree || 0) + 2) * 8;
      if (index < coreLimit) {
        const ring = Math.sqrt(index / Math.max(coreLimit - 1, 1));
        const degreePull = 1 - degreeRatio * 0.38;
        const radius = 36 + ring * 330 * degreePull;
        positions.set(node.id, {
          x: cx + Math.cos(angle) * radius,
          y: cy + Math.sin(angle) * radius,
          r: Math.min(Math.max(size, 12), 50),
          rank: index,
          isCore: true,
        });
        return;
      }

      const strongerNeighbor = (neighborMap.get(node.id) || [])
        .map((id) => ({ id, rank: rankById.get(id) ?? Number.MAX_SAFE_INTEGER }))
        .filter((item) => positions.has(item.id))
        .sort((a, b) => a.rank - b.rank)[0];
      const parent = strongerNeighbor ? positions.get(strongerNeighbor.id) : null;
      const local = hashText(`${node.id}:${index}`);
      const satelliteAngle = (local % 6283) / 1000;
      const satelliteRadius = 42 + (local % 80);
      const fallbackRadius = 420 + ((index - coreLimit) % 5) * 42;
      positions.set(node.id, {
        x: parent ? parent.x + Math.cos(satelliteAngle) * satelliteRadius : cx + Math.cos(angle) * fallbackRadius,
        y: parent ? parent.y + Math.sin(satelliteAngle) * satelliteRadius : cy + Math.sin(angle) * fallbackRadius,
        r: Math.min(Math.max(size * 0.72, 7), 24),
        rank: index,
        isCore: false,
      });
    });
    return { width, height, nodes, edges, positions, coreLimit, signature: ranked.map((node) => `${node.id}:${node.degree || 0}`).join("|") };
  }

  function NativeSelect(props) {
    const { value, onChange, children, label } = props;
    return h(
      "label",
      { className: "mp-select-wrap" },
      h("span", null, label),
      h("select", { value, onChange: (e) => onChange(e.target.value) }, children),
    );
  }

  function StatCell({ label, value }) {
    return h(
      "div",
      { className: "mp-stat" },
      h("span", null, label),
      h("strong", null, fmt(value)),
    );
  }

  function clamp(value, min, max) {
    return Math.max(min, Math.min(max, value));
  }

  function GraphView({ graph, selectedId, onSelect, onOpenTopic, onOpenCluster, onFocusNode }) {
    const svgRef = useRef(null);
    const dragRef = useRef(null);
    const clickRef = useRef({ id: "", at: 0 });
    const laidOut = useMemo(() => layoutGraph(graph), [graph]);
    const initialPositions = useMemo(() => {
      const next = {};
      laidOut.nodes.forEach((node) => {
        const pos = laidOut.positions.get(node.id);
        if (pos) next[node.id] = pos;
      });
      return next;
    }, [laidOut.signature]);
    const [positions, setPositions] = useState(initialPositions);
    const [viewport, setViewport] = useState({ x: 0, y: 0, zoom: 1 });
    const [showSmall, setShowSmall] = useState(false);
    const [hoveredId, setHoveredId] = useState("");

    useEffect(() => {
      setPositions(initialPositions);
      setViewport({ x: 0, y: 0, zoom: 1 });
      setHoveredId("");
      setShowSmall(false);
    }, [initialPositions]);

    const positionMap = useMemo(() => {
      const map = new Map();
      laidOut.nodes.forEach((node) => {
        map.set(node.id, positions[node.id] || laidOut.positions.get(node.id));
      });
      return map;
    }, [laidOut, positions]);

    const nodeById = useMemo(() => {
      const map = new Map();
      laidOut.nodes.forEach((node) => map.set(node.id, node));
      return map;
    }, [laidOut.nodes]);

    const visibleInfo = useMemo(() => {
      const focusId = hoveredId || selectedId || "";
      const related = new Set(focusId ? [focusId] : []);
      laidOut.edges.forEach((edge) => {
        if (edge.source === focusId) related.add(edge.target);
        if (edge.target === focusId) related.add(edge.source);
      });
      const revealSmall = graph && (graph.view_mode === "topics" || graph.view_mode === "clusters") ? true : showSmall || viewport.zoom >= 1.55;
      const nodeIds = new Set();
      laidOut.nodes.forEach((node) => {
        if (node.noise && !showSmall) return;
        const pos = positionMap.get(node.id) || {};
        if (revealSmall || pos.isCore || related.has(node.id)) nodeIds.add(node.id);
      });
      const edgeIds = new Set();
      laidOut.edges.forEach((edge) => {
        if (nodeIds.has(edge.source) && nodeIds.has(edge.target)) edgeIds.add(edge.id);
      });
      return {
        nodeIds,
        edgeIds,
        related,
        focusId,
        hiddenCount: Math.max(0, laidOut.nodes.length - nodeIds.size),
      };
    }, [graph, laidOut, positionMap, selectedId, hoveredId, showSmall, viewport.zoom]);

    const getSvgPoint = useCallback((event) => {
      const svg = svgRef.current;
      if (!svg) return { x: 0, y: 0 };
      const point = svg.createSVGPoint();
      point.x = event.clientX;
      point.y = event.clientY;
      const matrix = svg.getScreenCTM();
      if (!matrix) return { x: 0, y: 0 };
      const local = point.matrixTransform(matrix.inverse());
      return { x: local.x, y: local.y };
    }, []);

    const toGraphPoint = useCallback((event, currentViewport) => {
      const local = getSvgPoint(event);
      const view = currentViewport || viewport;
      return {
        x: (local.x - view.x) / view.zoom,
        y: (local.y - view.y) / view.zoom,
        local,
      };
    }, [getSvgPoint, viewport]);

    const fitToVisible = useCallback(() => {
      const points = laidOut.nodes
        .filter((node) => visibleInfo.nodeIds.has(node.id))
        .map((node) => positionMap.get(node.id))
        .filter(Boolean);
      if (!points.length) {
        setViewport({ x: 0, y: 0, zoom: 1 });
        return;
      }
      const minX = Math.min(...points.map((point) => point.x - point.r));
      const maxX = Math.max(...points.map((point) => point.x + point.r));
      const minY = Math.min(...points.map((point) => point.y - point.r));
      const maxY = Math.max(...points.map((point) => point.y + point.r));
      const pad = 90;
      const zoom = clamp(Math.min(laidOut.width / Math.max(maxX - minX + pad * 2, 1), laidOut.height / Math.max(maxY - minY + pad * 2, 1)), 0.35, 2.2);
      setViewport({
        zoom,
        x: laidOut.width / 2 - ((minX + maxX) / 2) * zoom,
        y: laidOut.height / 2 - ((minY + maxY) / 2) * zoom,
      });
    }, [laidOut, positionMap, visibleInfo.nodeIds]);

    const zoomAt = useCallback((event, factor) => {
      const local = getSvgPoint(event);
      setViewport((view) => {
        const zoom = clamp(view.zoom * factor, 0.28, 3.4);
        const graphX = (local.x - view.x) / view.zoom;
        const graphY = (local.y - view.y) / view.zoom;
        return {
          zoom,
          x: local.x - graphX * zoom,
          y: local.y - graphY * zoom,
        };
      });
    }, [getSvgPoint]);

    const activateNode = useCallback((nodeId) => {
      const node = nodeById.get(nodeId);
      if (!node) return;
      if (node.kind === "topic" && node.palace && onOpenTopic) {
        onOpenTopic(node.palace);
        return;
      }
      if (node.kind === "profile") {
        onSelect("");
        return;
      }
      if (node.kind === "cluster" && node.cluster && onOpenCluster) {
        onOpenCluster(node.cluster);
        return;
      }
      if (node.kind === "cluster-root") {
        onSelect(node.id);
        return;
      }
      if (graph && graph.palace && !graph.center_id && onFocusNode) {
        onSelect(node.id);
        onFocusNode(node);
        return;
      }
      onSelect(node.id);
    }, [graph, nodeById, onFocusNode, onOpenCluster, onOpenTopic, onSelect]);

    const openNode = useCallback((nodeId) => {
      const node = nodeById.get(nodeId);
      if (!node) return;
      if (node.kind === "topic" && node.palace && onOpenTopic) {
        onOpenTopic(node.palace);
        return;
      }
      if (node.kind === "cluster" && node.cluster && onOpenCluster) {
        onOpenCluster(node.cluster);
        return;
      }
      if (node.kind !== "profile" && onFocusNode) onFocusNode(node);
    }, [nodeById, onFocusNode, onOpenCluster, onOpenTopic]);

    useEffect(() => {
      const svg = svgRef.current;
      if (!svg) return undefined;
      const handleWheel = (event) => {
        event.preventDefault();
        zoomAt(event, event.deltaY > 0 ? 0.88 : 1.14);
      };
      svg.addEventListener("wheel", handleWheel, { passive: false });
      return () => svg.removeEventListener("wheel", handleWheel);
    }, [zoomAt, laidOut.signature]);

    if (!graph || !laidOut.nodes.length) {
      return h("div", { className: "mp-empty" }, "No graph data");
    }
    const setButtonZoom = (factor) => {
      setViewport((view) => {
        const zoom = clamp(view.zoom * factor, 0.28, 3.4);
        const centerX = laidOut.width / 2;
        const centerY = laidOut.height / 2;
        const graphX = (centerX - view.x) / view.zoom;
        const graphY = (centerY - view.y) / view.zoom;
        return {
          zoom,
          x: centerX - graphX * zoom,
          y: centerY - graphY * zoom,
        };
      });
    };

    const activeId = visibleInfo.focusId;
    const topicMode = graph && graph.view_mode === "topics";
    return h(
      "div",
      { className: "mp-graph-wrap" },
      h(
        "div",
        { className: "mp-graph-tools" },
        topicMode
          ? h("span", { className: "mp-mode-chip" }, "Palaces")
          : graph && graph.view_mode === "clusters"
            ? h("span", { className: "mp-mode-chip" }, "Areas")
          : h(
              "div",
              { className: "mp-segment", role: "group", "aria-label": "Node density" },
              h("button", { className: showSmall ? "" : "is-active", onClick: () => setShowSmall(false), title: "Show core nodes" }, "Core"),
              h("button", { className: showSmall ? "is-active" : "", onClick: () => setShowSmall(true), title: "Show every loaded node" }, "All"),
            ),
        h("button", { onClick: fitToVisible, title: "Fit graph" }, "Fit"),
        h("button", { onClick: () => setButtonZoom(0.82), title: "Zoom out" }, "−"),
        h("button", { onClick: () => setButtonZoom(1.22), title: "Zoom in" }, "+"),
        h("span", { className: "mp-hidden-count" }, visibleInfo.hiddenCount ? `${fmt(visibleInfo.hiddenCount)} hidden` : "all visible"),
      ),
      h(
        "svg",
        {
          ref: svgRef,
          className: `mp-graph ${dragRef.current ? "is-dragging" : ""}`,
          viewBox: `0 0 ${laidOut.width} ${laidOut.height}`,
          role: "img",
          "aria-label": "MemPalace graph",
          onPointerDown: (event) => {
            if (event.button !== 0) return;
            const point = getSvgPoint(event);
            dragRef.current = { kind: "pan", startX: point.x, startY: point.y, panX: viewport.x, panY: viewport.y, moved: false };
            event.currentTarget.setPointerCapture(event.pointerId);
          },
          onPointerMove: (event) => {
            const drag = dragRef.current;
            if (!drag) return;
            if (drag.kind === "pan") {
              const point = getSvgPoint(event);
              if (Math.hypot(point.x - drag.startX, point.y - drag.startY) > 3) drag.moved = true;
              setViewport((view) => ({ ...view, x: drag.panX + point.x - drag.startX, y: drag.panY + point.y - drag.startY }));
              return;
            }
            if (drag.kind === "node") {
              const point = toGraphPoint(event, drag.viewport);
              const distance = Math.hypot(point.local.x - drag.startLocalX, point.local.y - drag.startLocalY);
              if (distance > 3) drag.moved = true;
              setPositions((current) => ({
                ...current,
                [drag.id]: {
                  ...current[drag.id],
                  x: point.x - drag.offsetX,
                  y: point.y - drag.offsetY,
                },
              }));
            }
          },
          onPointerUp: (event) => {
            const drag = dragRef.current;
            if (drag && drag.kind === "node" && !drag.moved) {
              const now = Date.now();
              const previous = clickRef.current || {};
              if (previous.id === drag.id && now - Number(previous.at || 0) < 420) {
                clickRef.current = { id: "", at: 0 };
                openNode(drag.id);
              } else {
                clickRef.current = { id: drag.id, at: now };
                activateNode(drag.id);
              }
            }
            if (drag && drag.kind === "pan" && !drag.moved) onSelect("");
            dragRef.current = null;
            try {
              event.currentTarget.releasePointerCapture(event.pointerId);
            } catch (_e) {
              // Pointer capture may already be released by the browser.
            }
          },
          onPointerLeave: () => {
            if (dragRef.current && dragRef.current.kind === "pan") dragRef.current = null;
          },
        },
        h("rect", {
          className: "mp-pan-plane",
          x: 0,
          y: 0,
          width: laidOut.width,
          height: laidOut.height,
        }),
        h(
          "defs",
          null,
          h("marker", { id: "mp-arrow", viewBox: "0 0 10 10", refX: 8.5, refY: 5, markerWidth: 4.5, markerHeight: 4.5, orient: "auto-start-reverse" }, h("path", { d: "M 0 0 L 10 5 L 0 10 z" })),
        ),
        h(
          "g",
          { transform: `translate(${viewport.x} ${viewport.y}) scale(${viewport.zoom})` },
          h(
            "g",
            { className: "mp-edges" },
            laidOut.edges.map((edge) => {
              if (!visibleInfo.edgeIds.has(edge.id)) return null;
              const a = positionMap.get(edge.source);
              const b = positionMap.get(edge.target);
              if (!a || !b) return null;
              const active = activeId && (edge.source === activeId || edge.target === activeId);
              return h(
                "g",
                { key: edge.id, className: `mp-edge-row-svg ${edge.synthetic ? "is-synthetic" : ""} ${active ? "is-active" : activeId ? "is-muted" : ""}` },
                h("line", {
                  x1: a.x,
                  y1: a.y,
                  x2: b.x,
                  y2: b.y,
                  className: "mp-edge",
                  markerEnd: "url(#mp-arrow)",
                  "data-confidence": edge.confidence,
                }),
                active
                  ? h("text", { className: "mp-edge-label", x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 - 5 }, shorten(edge.label, 30))
                  : null,
              );
            }),
          ),
          h(
            "g",
            { className: "mp-nodes" },
            laidOut.nodes.map((node) => {
              if (!visibleInfo.nodeIds.has(node.id)) return null;
              const pos = positionMap.get(node.id);
              if (!pos) return null;
              const active = selectedId === node.id;
              const related = activeId && visibleInfo.related.has(node.id);
              const muted = activeId && !related;
              const showLabel = active || hoveredId === node.id || pos.isCore || viewport.zoom >= 1.35;
              const labelText = showLabel ? shorten(node.label, active || hoveredId === node.id ? 36 : 24) : "";
              const labelWidth = labelText ? Math.min(Math.max(labelText.length * 7.2, 54), 260) : 0;
              return h(
                "g",
                {
                  key: node.id,
                  className: `mp-node ${typeClass(node.type)} ${active ? "is-active" : ""} ${related ? "is-related" : ""} ${muted ? "is-muted" : ""} ${pos.isCore ? "is-core" : "is-small"}`,
                  transform: `translate(${pos.x} ${pos.y})`,
                  onPointerDown: (event) => {
                    if (event.button !== 0) return;
                    event.stopPropagation();
                    const point = toGraphPoint(event);
                    const local = point.local;
                    dragRef.current = {
                      kind: "node",
                      id: node.id,
                      viewport,
                      offsetX: point.x - pos.x,
                      offsetY: point.y - pos.y,
                      startLocalX: local.x,
                      startLocalY: local.y,
                      moved: false,
                    };
                    event.currentTarget.ownerSVGElement.setPointerCapture(event.pointerId);
                  },
                  onMouseEnter: () => setHoveredId(node.id),
                  onMouseLeave: () => setHoveredId((current) => (current === node.id ? "" : current)),
                  onDoubleClick: (event) => {
                    event.stopPropagation();
                    openNode(node.id);
                  },
                  tabIndex: 0,
                  role: "button",
                  onKeyDown: (e) => {
                    if (e.key === "Enter" || e.key === " ") activateNode(node.id);
                  },
                },
                h("rect", {
                  className: "mp-node-hit",
                  x: -pos.r - 6,
                  y: -18,
                  width: pos.r + 16 + labelWidth,
                  height: 36,
                }),
                h("circle", { r: pos.r }),
                labelText
                  ? h("text", { x: pos.r + 7, y: 4 }, labelText)
                  : null,
                h("title", null, `${node.label} (${node.type}) · ${fmt(node.degree)} links`),
              );
            }),
          ),
        ),
      ),
    );
  }

  function TreeBranch({ items, onSelectNode, level }) {
    if (!items || !items.length) {
      return h("div", { className: "mp-muted" }, "No linked facts");
    }
    return h(
      "div",
      { className: `mp-tree mp-tree-level-${level || 0}` },
      items.map((item, index) => {
        const entity = item.entity;
        return h(
          "details",
          {
            key: `${item.direction}-${item.predicate}-${item.value}-${index}`,
            className: "mp-tree-item",
            open: (level || 0) < 1 && item.children && item.children.length > 0,
          },
          h(
            "summary",
            null,
            h("span", { className: "mp-tree-predicate" }, item.direction === "in" ? `← ${item.predicate}` : `${item.predicate} →`),
            entity
              ? h(
                  "button",
                  {
                    className: "mp-tree-entity",
                    onClick: (event) => {
                      event.preventDefault();
                      event.stopPropagation();
                      onSelectNode(entity.id);
                    },
                    title: entity.palace || "",
                  },
                  shorten(entity.label, 120),
                )
              : h("strong", { className: "mp-tree-value" }, shorten(item.value, 180)),
          ),
          item.children && item.children.length
            ? h(TreeBranch, { items: item.children, onSelectNode, level: (level || 0) + 1 })
            : null,
        );
      }),
    );
  }

  function EntityPanel({ node, tree, onSelectNode, onFocusNode, onGoDeeper, canGoDeeper }) {
    const root = tree && tree.root ? { ...tree.root, noise: node && node.noise, quality_reason: node && node.quality_reason } : node;
    if (!root) {
      return h("aside", { className: "mp-detail" }, h("div", { className: "mp-empty" }, "Select a node"));
    }
    return h(
      "aside",
      { className: "mp-detail" },
      h(
        "div",
        { className: "mp-detail-head" },
        h("div", null, h("h3", null, root.label), root.palace ? h("span", { className: "mp-topic-tag" }, root.palace) : null),
        h(Badge, null, root.type),
      ),
      root.noise
        ? h("p", { className: "mp-quality-warning" }, `Low-signal memory: ${root.quality_reason || "weak provenance"}`)
        : null,
      h(
        "div",
        { className: "mp-detail-actions" },
        h(Button, { onClick: () => onFocusNode(root), title: "Focus graph around this node" }, "Focus"),
        h(Button, { onClick: () => onGoDeeper(root), disabled: !canGoDeeper, title: "Increase focus depth" }, "Go deeper"),
      ),
      root.description ? h("p", { className: "mp-description" }, root.description) : null,
      h(
        "div",
        { className: "mp-detail-section" },
        h("h4", null, "Direct facts"),
        (node && node.attributes || []).length
          ? node.attributes.slice(0, 8).map((attr, index) =>
              h(
                "div",
                { className: "mp-attr", key: `${attr.predicate}-${index}` },
                h("span", null, attr.predicate),
                h(
                  "strong",
                  null,
                  shorten(attr.value, 180),
                  attrSource(attr) ? h("em", { className: "mp-attr-source" }, attrSource(attr)) : null,
                ),
              ),
            )
          : h("div", { className: "mp-muted" }, "No attributes"),
      ),
      h(
        "div",
        { className: "mp-detail-section" },
        h("h4", null, "Tree"),
        tree ? h(TreeBranch, { items: tree.tree || [], onSelectNode, level: 0 }) : h("div", { className: "mp-muted" }, "Loading tree"),
      ),
    );
  }

  function SearchResults({ results, onPickTopic }) {
    return h(
      "div",
      { className: "mp-results" },
      h("h3", null, "Search"),
      results.length
        ? results.map((row) =>
            h(
              "button",
              {
                key: `${row.palace}-${row.kind}-${row.id}`,
                className: "mp-result",
                onClick: () => onPickTopic(row.palace),
                title: row.source_file || row.source_closet || "",
              },
              h("span", null, row.palace),
              h("strong", null, row.title),
              h("em", null, row.subtitle),
              row.snippet ? h("p", null, shorten(row.snippet, 240)) : null,
            ),
          )
        : h("div", { className: "mp-muted" }, "No results"),
    );
  }

  function ProfileMatrix({ matrix, activeProfile, onPickProfile, onPickTopic }) {
    if (!matrix || !Array.isArray(matrix.profiles)) {
      return h("section", { className: "mp-matrix" }, h("div", { className: "mp-muted" }, "Profile matrix unavailable"));
    }
    const profiles = matrix.profiles || [];
    const palaces = matrix.palaces || [];
    return h(
      "section",
      { className: "mp-matrix" },
      h(
        "div",
        { className: "mp-matrix-head" },
        h("h3", null, "Profile split"),
        h("p", null, "Empty cells mean this palace is not present in that profile."),
      ),
      h(
        "div",
        { className: "mp-profile-totals" },
        profiles.map((row) => {
          const totals = (matrix.totals && matrix.totals[row.name]) || {};
          return h(
            "button",
            {
              key: row.name,
              className: `mp-profile-total ${row.name === activeProfile ? "is-active" : ""}`,
              onClick: () => onPickProfile(row.name),
              title: row.path || "",
            },
            h("strong", null, row.name),
            h("span", null, `${fmt(totals.palaces)} palace(s)`),
            h("em", null, `${fmt(totals.entities)} entities · ${fmt(totals.triples)} triples`),
          );
        }),
      ),
      h(
        "div",
        { className: "mp-matrix-scroll" },
        h(
          "table",
          { className: "mp-matrix-table" },
          h(
            "thead",
            null,
            h(
              "tr",
              null,
              h("th", null, "Palace"),
              profiles.map((row) => h("th", { key: row.name }, row.name)),
            ),
          ),
          h(
            "tbody",
            null,
            palaces.map((row) =>
              h(
                "tr",
                { key: row.palace, className: row.shared ? "is-shared" : "is-profile-local" },
                h("td", null, row.palace),
                profiles.map((profileRow) => {
                  const cell = row.cells && row.cells[profileRow.name];
                  const exists = cell && cell.exists;
                  return h(
                    "td",
                    { key: profileRow.name },
                    exists
                      ? h(
                          "button",
                          {
                            className: "mp-matrix-cell",
                            onClick: () => {
                              onPickProfile(profileRow.name);
                              onPickTopic(row.palace);
                            },
                          },
                          h("strong", null, fmt(cell.entities)),
                          h("span", null, fmt(cell.triples)),
                        )
                      : h("span", { className: "mp-matrix-empty" }, "—"),
                  );
                }),
              ),
            ),
          ),
        ),
      ),
    );
  }

  function MempalacePage() {
    const [profiles, setProfiles] = useState([]);
    const [matrix, setMatrix] = useState(null);
    const [profile, setProfile] = useState("default");
    const [palaces, setPalaces] = useState([]);
    const [palace, setPalace] = useState("");
    const [graph, setGraph] = useState(null);
    const [nodeTree, setNodeTree] = useState(null);
    const [selectedId, setSelectedId] = useState("");
    const [query, setQuery] = useState("");
    const [results, setResults] = useState([]);
    const [showSplit, setShowSplit] = useState(false);
    const [focusStack, setFocusStack] = useState([]);
    const [activeCluster, setActiveCluster] = useState("");
    const [busy, setBusy] = useState(false);
    const [message, setMessage] = useState("");
    const [error, setError] = useState("");
    const [noisePreview, setNoisePreview] = useState(null);
    const [consolidator, setConsolidator] = useState(null);
    const [profileStatuses, setProfileStatuses] = useState([]);
    const [consolidatorPreview, setConsolidatorPreview] = useState(null);
    const [jobs, setJobs] = useState([]);
    const [route, setRoute] = useState(() => (window.location.hash === "#process" ? "process" : "graph"));
    const [extractorConfig, setExtractorConfig] = useState(null);

    const loadProfiles = useCallback(async () => {
      const [profileData, matrixData] = await Promise.all([
        fetchJSON(`${API}/profiles`),
        fetchJSON(`${API}/matrix`),
      ]);
      setProfiles(profileData.profiles || []);
      setMatrix(matrixData || null);
    }, []);

    const loadPalaces = useCallback(async () => {
      const data = await fetchJSON(`${API}/palaces?profile=${encodeURIComponent(profile)}`);
      const rows = data.palaces || [];
      setPalaces(rows);
      setPalace((current) => {
        if (current && rows.some((row) => row.palace === current)) return current;
        return "";
      });
    }, [profile]);

    const loadConsolidator = useCallback(async () => {
      const data = await fetchJSON(`${API}/consolidator/status?profile=${encodeURIComponent(profile)}`);
      setConsolidator(data || null);
    }, [profile]);

    const loadProfileStatuses = useCallback(async () => {
      const data = await fetchJSON(`${API}/consolidator/statuses`);
      const rows = data.profiles || [];
      setProfileStatuses(rows);
      const selected = rows.find((row) => row.profile === profile);
      if (selected) setConsolidator(selected);
    }, [profile]);

    const applyProfileStatuses = useCallback((data) => {
      const rows = data && data.profiles ? data.profiles : data && data.profile ? [data] : [];
      if (!rows.length) return;
      setProfileStatuses((current) => {
        const byProfile = new Map(current.map((row) => [row.profile, row]));
        rows.forEach((row) => byProfile.set(row.profile, row));
        return Array.from(byProfile.values());
      });
      const selected = rows.find((row) => row.profile === profile);
      if (selected) {
        setConsolidator(selected);
      } else if (data && data.profile === profile) {
        setConsolidator(data);
      }
    }, [profile]);

    const loadJobs = useCallback(async () => {
      const data = await fetchJSON(`${API}/consolidator/jobs`);
      setJobs(data.jobs || []);
    }, []);

    const loadExtractorConfig = useCallback(async () => {
      const data = await fetchJSON(`${API}/consolidator/config`);
      setExtractorConfig(data || null);
    }, []);

    const loadGraph = useCallback(async () => {
      const qs = new URLSearchParams({
        profile,
        query,
        node_limit: "220",
        edge_limit: "520",
      });
      if (palace) qs.set("palace", palace);
      const focus = focusStack[focusStack.length - 1];
      if (focus) {
        qs.set("center", focus.id);
        qs.set("depth", String(focus.depth || 1));
      }
      const data = await fetchJSON(`${API}/graph?${qs.toString()}`);
      setGraph(data);
      setNodeTree(null);
      setSelectedId((current) => {
        if (current && data.nodes.some((node) => node.id === current)) return current;
        return "";
      });
    }, [profile, palace, query, focusStack]);

    const loadNodeTree = useCallback(async () => {
      if (!selectedId) {
        setNodeTree(null);
        return;
      }
      const qs = new URLSearchParams({
        profile,
        node_id: selectedId,
        depth: "2",
        limit: "120",
      });
      if (palace) qs.set("palace", palace);
      const data = await fetchJSON(`${API}/node?${qs.toString()}`);
      setNodeTree(data);
    }, [profile, palace, selectedId]);

    useEffect(() => {
      loadProfiles().catch((err) => setError(parseError(err)));
    }, [loadProfiles]);

    useEffect(() => {
      setBusy(true);
      setError("");
      loadPalaces()
        .catch((err) => setError(parseError(err)))
        .finally(() => setBusy(false));
    }, [loadPalaces]);

    useEffect(() => {
      loadProfileStatuses().catch((err) => {
        setError(parseError(err));
        loadConsolidator().catch(() => {});
      });
    }, [loadProfileStatuses, loadConsolidator]);

    useEffect(() => {
      loadJobs().catch(() => {});
    }, [loadJobs]);

    useEffect(() => {
      loadExtractorConfig().catch(() => {});
    }, [loadExtractorConfig]);

    useEffect(() => {
      const syncRoute = () => setRoute(window.location.hash === "#process" ? "process" : "graph");
      window.addEventListener("hashchange", syncRoute);
      return () => window.removeEventListener("hashchange", syncRoute);
    }, []);

    useEffect(() => {
      const timer = window.setInterval(() => {
        loadProfileStatuses().catch(() => {
          loadConsolidator().catch(() => {});
        });
        loadJobs().catch(() => {});
      }, 5000);
      return () => window.clearInterval(timer);
    }, [loadProfileStatuses, loadConsolidator, loadJobs]);

    useEffect(() => {
      setFocusStack([]);
      setActiveCluster("");
    }, [profile, palace, query]);

    useEffect(() => {
      setBusy(true);
      setError("");
      loadGraph()
        .catch((err) => setError(parseError(err)))
        .finally(() => setBusy(false));
    }, [loadGraph]);

    useEffect(() => {
      setError("");
      loadNodeTree().catch((err) => setError(parseError(err)));
    }, [loadNodeTree]);

    const runSearch = useCallback(async () => {
      if (!query.trim()) {
        setResults([]);
        return;
      }
      setBusy(true);
      setError("");
      try {
        const qs = new URLSearchParams({ profile, q: query, limit: "40" });
        if (palace) qs.set("palace", palace);
        const data = await fetchJSON(`${API}/search?${qs.toString()}`);
        setResults(data.results || []);
      } catch (err) {
        setError(parseError(err));
      } finally {
        setBusy(false);
      }
    }, [profile, palace, query]);

    const generate = useCallback(async () => {
      setBusy(true);
      setError("");
      setMessage("");
      try {
        const data = await fetchJSON(`${API}/generate`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ profile, dry_run: false, auto_clean: true, clean_max_delete: 250 }),
        });
        const entries = (data.palaces || []).reduce((sum, item) => sum + Number(item.entries || 0), 0);
        setMessage(`Synced ${fmt(entries)} curated entry(s) from ${fmt(data.files)} memory file(s).${autoCleanText(data)}`);
        await loadProfiles();
        await loadPalaces();
        await loadConsolidator();
        await loadGraph();
      } catch (err) {
        setError(parseError(err));
      } finally {
        setBusy(false);
      }
    }, [profile, loadProfiles, loadPalaces, loadConsolidator, loadGraph]);

    const rebuildProfile = useCallback(async () => {
      setBusy(true);
      setError("");
      setMessage("");
      try {
        const data = await fetchJSON(`${API}/rebuild`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            profile,
            all_profiles: false,
            backup: true,
            auto_clean: true,
            clean_max_delete: 250,
            full: true,
            workers: 5,
            profile_workers: 2,
          }),
        });
        if (data && data.id && data.status) {
          setMessage(`Full rebuild for ${profile} started as process ${data.id}.`);
          await loadJobs();
          await loadConsolidator();
          return;
        }
        const generated = data.generated || {};
        const entries = (generated.palaces || []).reduce((sum, item) => sum + Number(item.entries || 0), 0);
        setMessage(
          `LLM rebuilt ${profile}: ${fmt(data.processed_messages || 0)} message(s), ${fmt(data.triples || 0)} graph triple(s), plus ${fmt(entries)} curated entry(s) from ${fmt(generated.files || 0)} memory file(s).${autoCleanText(data)}`,
        );
        await loadProfiles();
        await loadPalaces();
        await loadConsolidator();
        await loadGraph();
      } catch (err) {
        setError(parseError(err));
      } finally {
        setBusy(false);
      }
    }, [profile, loadProfiles, loadPalaces, loadConsolidator, loadJobs, loadGraph]);

    const rebuildAll = useCallback(async () => {
      setBusy(true);
      setError("");
      setMessage("");
      try {
        const data = await fetchJSON(`${API}/rebuild`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            all_profiles: true,
            backup: true,
            auto_clean: true,
            clean_max_delete: 250,
            full: true,
            workers: 5,
            profile_workers: 2,
          }),
        });
        if (data && data.id && data.status) {
          setMessage(`Full rebuild for all profiles started as process ${data.id}.`);
          await loadJobs();
          await loadConsolidator();
          return;
        }
        const summary = (data.profiles || [])
          .map((row) => `${row.profile}: ${fmt(row.processed_messages || 0)} msg / ${fmt(row.triples || 0)} triples`)
          .join(", ");
        setMatrix(data.matrix || null);
        setMessage(`Rebuilt all profiles: ${summary}.${autoCleanText(data)}`);
        await loadProfiles();
        await loadPalaces();
        await loadConsolidator();
        await loadGraph();
      } catch (err) {
        setError(parseError(err));
      } finally {
        setBusy(false);
      }
    }, [loadProfiles, loadPalaces, loadConsolidator, loadJobs, loadGraph]);

    const previewNoise = useCallback(async () => {
      setBusy(true);
      setError("");
      setMessage("");
      try {
        const data = await fetchJSON(`${API}/clean-noise`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ profile, palace, dry_run: true, backup: true, max_delete: 250 }),
        });
        setNoisePreview(data);
        const suffix = data.limited ? `; showing ${fmt(data.returned)}` : "";
        setMessage(`Noise preview: ${fmt(data.total_candidates)} candidate(s)${suffix}.`);
      } catch (err) {
        setError(parseError(err));
      } finally {
        setBusy(false);
      }
    }, [profile, palace]);

    const cleanNoise = useCallback(async () => {
      const count = noisePreview && Number(noisePreview.total_candidates || 0);
      const ok = window.confirm(`Delete ${fmt(count)} low-signal MemPalace node(s) from ${palace || "all palaces"}? A SQLite backup will be created first.`);
      if (!ok) return;
      setBusy(true);
      setError("");
      setMessage("");
      try {
        const data = await fetchJSON(`${API}/clean-noise`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ profile, palace, dry_run: false, backup: true, max_delete: Math.max(count, 250) }),
        });
        setNoisePreview(null);
        setMessage(`Cleaned ${fmt(data.deleted_entities)} node(s), ${fmt(data.deleted_triples)} triple(s), ${fmt(data.deleted_orphans)} orphan literal(s). Backup: ${data.backup_root || "none"}`);
        await loadProfiles();
        await loadPalaces();
        await loadConsolidator();
        await loadGraph();
      } catch (err) {
        setError(parseError(err));
      } finally {
        setBusy(false);
      }
    }, [profile, palace, noisePreview, loadProfiles, loadPalaces, loadConsolidator, loadGraph]);

    const runValidator = useCallback(async (options = {}) => {
      const allProfiles = Boolean(options.allProfiles);
      setBusy(true);
      setError("");
      setMessage("");
      try {
        const data = await fetchJSON(`${API}/validate-clean`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ profile, all_profiles: allProfiles, palace: allProfiles ? "" : palace, dry_run: false, backup: true, max_candidates: 80 }),
        });
        setMessage(`${allProfiles ? "Validator for all profiles" : `Validator for ${profile}`} started as process ${data.id || "unknown"}.`);
        await loadJobs();
        await loadProfiles();
        await loadPalaces();
        await loadConsolidator();
        await loadGraph();
      } catch (err) {
        setError(parseError(err));
      } finally {
        setBusy(false);
      }
    }, [profile, palace, loadJobs, loadProfiles, loadPalaces, loadConsolidator, loadGraph]);

    const runConsolidator = useCallback(async (options = {}) => {
      const dryRun = Boolean(options.dryRun);
      const backfill = Boolean(options.backfill);
      const allProfiles = Boolean(options.allProfiles);
      if (backfill) {
        const target = allProfiles ? "all profiles" : profile;
        const ok = window.confirm(`Rebuild LLM history memory for ${target}? Existing history_* palaces will be backed up and replaced.`);
        if (!ok) return;
      }
      setBusy(true);
      setError("");
      setMessage("");
      setConsolidatorPreview(null);
      try {
        const data = await fetchJSON(`${API}/consolidator/run`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            profile,
            all_profiles: allProfiles,
            dry_run: dryRun,
            backfill,
            reset_cursor: backfill,
            clear_history: backfill,
            backup: true,
            limit: 25,
            max_batches: backfill ? 10 : 1,
            full: backfill,
            workers: 5,
            profile_workers: 2,
            auto_clean: true,
            clean_max_delete: 250,
          }),
        });
        if (data && data.id && data.status) {
          setMessage(`${allProfiles ? "Full rebuild for all profiles" : `Full rebuild for ${profile}`} started as process ${data.id}.`);
          await loadJobs();
          await loadConsolidator();
          return;
        }
        if (dryRun) setConsolidatorPreview(data);
        const processed = data.profiles
          ? data.profiles.reduce((sum, row) => sum + Number(row.processed_messages || 0), 0)
          : Number(data.processed_messages || 0);
        const triples = data.profiles
          ? data.profiles.reduce((sum, row) => sum + Number(row.triples || 0), 0)
          : Number(data.triples || 0);
        setMessage(`${dryRun ? "Previewed" : backfill ? "Backfilled" : "Consolidated"} ${fmt(processed)} message(s), ${fmt(triples)} graph triple(s).${autoCleanText(data)}`);
        await loadProfiles();
        await loadPalaces();
        await loadConsolidator();
        await loadGraph();
      } catch (err) {
        setError(parseError(err));
      } finally {
        setBusy(false);
      }
    }, [profile, loadProfiles, loadPalaces, loadConsolidator, loadJobs, loadGraph]);

    const startExtractor = useCallback(async (options = {}) => {
      const targetProfile = options.profile || profile;
      const allProfiles = Boolean(options.allProfiles);
      setBusy(true);
      setError("");
      setMessage("");
      try {
        const unpauseData = await fetchJSON(`${API}/consolidator/pause`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ profile: targetProfile, all_profiles: allProfiles, paused: false }),
        });
        applyProfileStatuses(unpauseData);
        const data = await fetchJSON(`${API}/consolidator/run`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            profile: targetProfile,
            all_profiles: allProfiles,
            resume: true,
            dry_run: false,
            backup: true,
            limit: 25,
            max_batches: 10,
            workers: 5,
            profile_workers: 2,
            auto_clean: true,
            clean_max_delete: 250,
          }),
        });
        setMessage(`${allProfiles ? "Extractor for all profiles" : `Extractor for ${targetProfile}`} started as process ${data.id || "unknown"}.`);
        await loadJobs();
        await loadProfileStatuses();
        await loadConsolidator();
      } catch (err) {
        setError(parseError(err));
      } finally {
        setBusy(false);
      }
    }, [profile, applyProfileStatuses, loadJobs, loadProfileStatuses, loadConsolidator]);

    const setProfilePaused = useCallback(async (targetProfile, paused) => {
      setBusy(true);
      setError("");
      setMessage("");
      try {
        const data = await fetchJSON(`${API}/consolidator/pause`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ profile: targetProfile, paused }),
        });
        applyProfileStatuses(data);
        setMessage(paused ? `${targetProfile} paused after the current batch.` : `${targetProfile} resumed.`);
        await loadProfileStatuses();
        await loadConsolidator();
      } catch (err) {
        setError(parseError(err));
      } finally {
        setBusy(false);
      }
    }, [applyProfileStatuses, loadProfileStatuses, loadConsolidator]);

    const setGlobalAuto = useCallback(async (enabled) => {
      setBusy(true);
      setError("");
      setMessage("");
      try {
        const data = await fetchJSON(`${API}/consolidator/auto`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ all_profiles: true, enabled, unpause: enabled }),
        });
        applyProfileStatuses(data);
        setMessage(enabled ? "Automatic extraction enabled for all profiles." : "Automatic extraction disabled for all profiles.");
        await loadProfileStatuses();
        await loadConsolidator();
      } catch (err) {
        setError(parseError(err));
      } finally {
        setBusy(false);
      }
    }, [applyProfileStatuses, loadProfileStatuses, loadConsolidator]);

    const activeFocus = focusStack[focusStack.length - 1] || null;
    const selectedStats = (graph && graph.stats) || (palace ? palaces.find((row) => row.palace === palace) : {}) || {};
    const activeProfileTotal = matrix && matrix.totals ? matrix.totals[profile] : null;
    const consolidatorTotal = Number((consolidator && consolidator.total_messages) || 0);
    const consolidatorPending = Number((consolidator && consolidator.pending_messages) || 0);
    const consolidatorProcessed = Math.max(0, consolidatorTotal - consolidatorPending);
    const consolidatorProgress = consolidatorTotal ? Math.round((consolidatorProcessed / consolidatorTotal) * 100) : 0;
    const lastBatch = (consolidator && consolidator.last_batch) || {};
    const currentRun = (consolidator && consolidator.current) || {};
    const recentEvents = (consolidator && consolidator.events) || [];
    const scheduler = (consolidator && consolidator.scheduler) || {};
    const activeJob =
      (jobs || []).find((job) => job.status === "running") ||
      (jobs || []).find((job) => job.status === "queued") ||
      null;
    const activeProgress = (activeJob && activeJob.progress) || {};
    const activeStatus =
      activeProgress.status ||
      (activeProgress.profile_progress && activeProgress.profile_progress.status) ||
      (activeProgress.profile_result && activeProgress.profile_result.status) ||
      consolidator ||
      {};
    const activeCurrent =
      activeProgress.current ||
      (activeProgress.profile_progress && activeProgress.profile_progress.current) ||
      (activeProgress.profile_result && activeProgress.profile_result.current) ||
      activeStatus.current ||
      currentRun ||
      {};
    const activeTotal = Number(activeStatus.total_messages || consolidatorTotal || 0);
    const activePending = Number(activeStatus.pending_messages || consolidatorPending || 0);
    const hasActiveRun = Boolean(activeJob || (consolidator && consolidator.running));
    const autoGateText = !(consolidator && consolidator.auto_enabled)
      ? "auto off"
      : consolidator && consolidator.paused
        ? "paused"
        : hasActiveRun
          ? "job running"
          : activePending > 0
            ? "waiting for tick"
            : "no pending";
    const autoSummaryText = `${consolidator && consolidator.auto_enabled ? "on" : "off"} / ${autoGateText}`;
    const schedulerLastTick = scheduler.last_tick_at ? relativeTime(scheduler.last_tick_at) : "—";
    const schedulerNextTick = scheduler.next_tick_at ? relativeTime(scheduler.next_tick_at) : "—";
    const schedulerAction = scheduler.last_action || "—";
    const activeProcessed = activeTotal
      ? Math.max(0, activeTotal - activePending)
      : Number(
          (activeProgress.profile_progress && activeProgress.profile_progress.processed_messages) ||
            activeProgress.processed_messages ||
            consolidatorProcessed ||
            0,
        );
    const activePercent = activeTotal ? Math.round((activeProcessed / activeTotal) * 100) : consolidatorProgress;
    const heroMetricText = `${fmt(activePercent)}%`;
    const heroStateText = hasActiveRun ? (activeJob ? activeJob.status : "running") : activePending > 0 ? "queue" : "done";
    const heroBarWidth = activePercent;
    const lastActivityAt =
      activeCurrent.updated_at ||
      (recentEvents && recentEvents.length ? recentEvents[recentEvents.length - 1].at : "") ||
      (activeJob && activeJob.updated_at) ||
      (consolidator && consolidator.last_finished_at) ||
      "";
    const lastActivityMs = lastActivityAt ? Date.parse(lastActivityAt) : 0;
    const activityAgeSec = lastActivityMs ? Math.max(0, Math.round((Date.now() - lastActivityMs) / 1000)) : 0;
    const isExtractorStalled = Boolean(
      (activeJob && activeJob.status === "running") ||
        (consolidator && consolidator.running)
    ) && activityAgeSec > 360;
    const activePhaseRaw = !hasActiveRun && activePending > 0
      ? "waiting"
      : activeCurrent.phase || activeStatus.phase || (activeJob && activeJob.status) || (consolidator && consolidator.phase) || "idle";
    const activePhase = isExtractorStalled ? "stalled" : activePhaseRaw;
    const activeProfileName = activeCurrent.profile || activeProgress.profile || activeJob && activeJob.profile || profile;
    const activePalaceName =
      activeCurrent.palace ||
      (Array.isArray(activeCurrent.palaces) && activeCurrent.palaces.length ? activeCurrent.palaces.join(", ") : "—");
    const activeBatch = activeCurrent.batch_index ? fmt(activeCurrent.batch_index) : "—";
    const activeCursor = activeStatus.cursor_message_id || activeCurrent.cursor_message_id || consolidator && consolidator.cursor_message_id || 0;
    const activeBatchMessages = hasActiveRun ? Number(activeCurrent.message_count || activeCurrent.messages || 0) : 0;
    const activeBatchEntities = hasActiveRun ? Number(activeCurrent.batch_entities || activeCurrent.entities || 0) : 0;
    const activeBatchTriples = hasActiveRun ? Number(activeCurrent.batch_triples || activeCurrent.triples || 0) : 0;
    const activeBatchSkipped = hasActiveRun ? Number(activeCurrent.skipped_items || 0) : 0;
    const activeLabel = activeJob
      ? String(activeJob.kind || "").replace(/_/g, " ")
      : hasActiveRun
        ? "selected profile extractor"
        : activePending > 0
          ? "messages waiting for extractor"
          : "profile memory is up to date";
    const fallbackExtractorModels = [
      { role: "Primary", provider: "openai-codex", model: "gpt-5.4-mini" },
      { role: "Fallback", provider: "groq", model: "llama-3.3-70b-versatile" },
      { role: "Fallback", provider: "nvidia", model: "nvidia/nemotron-3-super-120b-a12b" },
      { role: "Fallback", provider: "auto", model: "main model fallback" },
    ];
    const extractorModels =
      extractorConfig && Array.isArray(extractorConfig.models) && extractorConfig.models.length
        ? extractorConfig.models
        : fallbackExtractorModels;
    const validatorModels =
      extractorConfig && extractorConfig.validator && Array.isArray(extractorConfig.validator.models) && extractorConfig.validator.models.length
        ? extractorConfig.validator.models
        : [{ role: "Primary", provider: "auto", model: "main model" }];
    const primaryModel = extractorModels.find((row) => String(row.role || "").toLowerCase() === "primary") || extractorModels[0] || {};
    const validatorPrimaryModel = validatorModels.find((row) => String(row.role || "").toLowerCase() === "primary") || validatorModels[0] || {};
    const fallbackModels = extractorModels.filter((row) => row !== primaryModel);
    const configuredModelLabel = `${primaryModel.provider || "auto"} / ${primaryModel.model || "main model"}`;
    const validatorModelLabel = `${validatorPrimaryModel.provider || "auto"} / ${validatorPrimaryModel.model || "main model"}`;
    const activeModelLabel =
      activeCurrent.model ||
      activeStatus.model ||
      lastBatch.model ||
      configuredModelLabel;
    const fallbackModelLabel = fallbackModels.length
      ? fallbackModels.map((row) => `${row.provider || "auto"} / ${row.model || "main model"}`).join(" -> ")
      : "none";
    const extractorTask = (extractorConfig && extractorConfig.task) || (consolidator && consolidator.task) || "mempalace_extractor";
    const extractorAdapter = (extractorConfig && extractorConfig.adapter) || (consolidator && consolidator.adapter) || "hermes_history_llm";
    const allProfileStatuses = profileStatuses && profileStatuses.length ? profileStatuses : (consolidator ? [consolidator] : []);
    const profileStatusCount = allProfileStatuses.length;
    const autoEnabledCount = allProfileStatuses.filter((status) => status && status.auto_enabled).length;
    const allAutoEnabled = profileStatusCount > 0 && autoEnabledCount === profileStatusCount;
    const anyExtractorRunning = Boolean(activeJob || allProfileStatuses.some((status) => status && status.running));
    const allRecentEvents = allProfileStatuses.flatMap((status) =>
      ((status && status.events) || []).map((event) => ({ ...event, profile: event.profile || status.profile || profile })),
    );
    const jobRunId = (job) => {
      const progress = (job && job.progress) || {};
      const status =
        progress.status ||
        (progress.profile_progress && progress.profile_progress.status) ||
        (progress.profile_result && progress.profile_result.status) ||
        {};
      const current =
        progress.current ||
        (progress.profile_progress && progress.profile_progress.current) ||
        (progress.profile_result && progress.profile_result.current) ||
        status.current ||
        {};
      const result = (job && job.result) || {};
      const resultCurrent = result.current || (result.status && result.status.current) || {};
      return current.run_id || resultCurrent.run_id || result.run_id || "";
    };
    const statusProcessRows = allProfileStatuses.flatMap((status) => {
      if (!status) return [];
      const profileName = status.profile || profile;
      const events = status.events || [];
      const startEvent = events.slice().reverse().find((event) => event && event.message === "Started MemPalace extraction") || {};
      const finishEvent = events.slice().reverse().find((event) => event && /finished/i.test(String(event.message || ""))) || {};
      const current = status.current || {};
      const pending = Number(status.pending_messages || 0);
      const running = Boolean(status.running);
      const runId = current.run_id || status.current_run_id || startEvent.run_id || "";
      const rows = [];
      if (runId || startEvent.at || status.last_started_at || finishEvent.at || status.last_finished_at) {
        rows.push({
          id: runId || `profile:${profileName}:last`,
          run_id: runId,
          kind: running ? "extraction" : "last_extraction",
          status: running ? "running" : status.stale ? "stale" : status.phase === "error" ? "error" : finishEvent.status || status.phase || "done",
          profile: profileName,
          all_profiles: false,
          model: current.model || status.model || startEvent.model || activeModelLabel,
          started_at: startEvent.at || status.last_started_at || "",
          finished_at: running ? "" : finishEvent.at || status.last_finished_at || "",
        });
      }
      return rows;
    });
    const rawProcessRows = [
      ...(jobs || []).map((job) => ({ ...job, run_id: jobRunId(job), model: job.model || activeModelLabel })),
      ...statusProcessRows,
    ].filter((job) => job && job.id);
    const processRows = rawProcessRows.filter((job, idx, rows) => {
      const keys = [job.id, job.run_id].filter(Boolean);
      return rows.findIndex((item) => {
        const itemKeys = [item.id, item.run_id].filter(Boolean);
        return itemKeys.some((key) => keys.includes(key));
      }) === idx;
    });
    const profileStatusByName = new Map(allProfileStatuses.map((status) => [status.profile || profile, status]));
    const eventMetaText = (event) => {
      const eventPalaces = Array.isArray(event.palaces) ? event.palaces.join(", ") : event.palace || "";
      return [
        event.model ? `model ${event.model}` : `model ${activeModelLabel}`,
        event.batch_index ? `batch ${fmt(event.batch_index)}` : "",
        event.messages ? `${fmt(event.messages)} msg` : "",
        event.triples ? `${fmt(event.triples)} triples` : "",
        event.selected !== undefined ? `${fmt(event.selected)} selected` : "",
        event.deleted_entities !== undefined ? `${fmt(event.deleted_entities)} deleted` : "",
        event.cursor ? `cursor ${fmt(event.cursor)}` : "",
        eventPalaces,
      ].filter(Boolean).join(" | ");
    };
    const eventsByProcess = {};
    allRecentEvents.forEach((event, idx) => {
      const key =
        event.run_id ||
        (event.model_task === "mempalace_validator" ? `validator:${event.at || idx}` : "") ||
        `event:${event.at || idx}`;
      if (!eventsByProcess[key]) eventsByProcess[key] = [];
      eventsByProcess[key].push(event);
    });
    const eventProcessRows = Object.keys(eventsByProcess).map((key) => {
      const evs = eventsByProcess[key] || [];
      const first = evs[0] || {};
      const last = evs[evs.length - 1] || {};
      const profileName = first.profile || last.profile || profile;
      const profileStatus = profileStatusByName.get(profileName) || {};
      const currentRunId =
        profileStatus.current_run_id ||
        (profileStatus.current && profileStatus.current.run_id) ||
        "";
      const isLiveRun = Boolean(profileStatus.running && currentRunId && currentRunId === key);
      const hasError = evs.some((event) => event.level === "error" || event.status === "error");
      const hasRunning = evs.some((event) => event.status === "running");
      const hasSkipped = evs.some((event) => /skipped/i.test(String(event.message || "")) && !isNoIssueValidationEvent(event));
      const isValidator = evs.some((event) => event.model_task === "mempalace_validator");
      const finished = evs.slice().reverse().find((event) => /finished|skipped/i.test(String(event.message || "")) || event.status === "success" || event.status === "error") || last;
      const unfinishedLiveRun = hasRunning && isLiveRun && !/finished|skipped/i.test(String(last.message || ""));
      return {
        id: key,
        kind: isValidator ? "validator" : "extraction",
        status: hasError ? "error" : unfinishedLiveRun ? "running" : hasSkipped ? "skipped" : finished.status || "done",
        profile: profileName,
        all_profiles: false,
        model: first.model || last.model || activeModelLabel,
        started_at: first.at || "",
        finished_at: unfinishedLiveRun ? "" : finished.at || last.at || "",
        events: evs,
      };
    });
    const processRowKeys = new Set(processRows.flatMap((row) => [row.id, row.run_id].filter(Boolean)));
    const processBlocks = [
      ...processRows.map((row) => {
        const rowEvents = eventsByProcess[row.run_id] || eventsByProcess[row.id] || [];
        return { ...row, events: rowEvents };
      }),
      ...eventProcessRows.filter((row) => !processRowKeys.has(row.id)),
    ].sort((a, b) => String(b.started_at || b.created_at || "").localeCompare(String(a.started_at || a.created_at || "")));
    const processKindLabel = (job) => {
      const value = String((job && job.kind) || "").toLowerCase();
      if (value.includes("last_extraction")) return "Last extraction";
      if (value.includes("validate")) return "Validation";
      if (value.includes("auto")) return "Auto extraction";
      if (value.includes("resume")) return "Resume extraction";
      if (value.includes("full") || value.includes("rebuild") || value.includes("backfill")) return job && job.all_profiles ? "Full rebuild all profiles" : "Full rebuild profile";
      if (value.includes("extract")) return "Extraction";
      return titleCaseWords(value || "process");
    };
    const processTitle = (job) => {
      const parts = [processKindLabel(job)];
      if (job && job.all_profiles) {
        parts.push("all profiles");
      } else if (job && job.profile) {
        parts.push(job.profile);
      }
      const when = relativeTime((job && (job.finished_at || job.started_at || job.created_at)) || "");
      if (when !== "—") parts.push(when);
      return parts.join(" · ");
    };
    const processTechnicalLabel = (job) => {
      const id = String((job && job.id) || "");
      if (!id || id === "persisted") return "persisted profile status";
      if (id.startsWith("validator:") || id.startsWith("event:")) return "from persisted event log";
      return `id ${shorten(id, 12)}`;
    };
    const isCurrentProcess = (job) => {
      if (!job) return false;
      if (statusTone(job.status) === "running") return true;
      if (activeJob && job.id === activeJob.id) return true;
      const activeRunId = activeCurrent.run_id || activeStatus.current_run_id || "";
      return Boolean(activeRunId && (job.id === activeRunId || job.run_id === activeRunId));
    };
    const activeRunStateText = heroStateText;
    const profileGroupNames = Array.from(new Set([
      ...allProfileStatuses.map((status) => status.profile || profile),
      ...processBlocks.map((row) => row.profile || profile),
    ]));
    const processGroups = profileGroupNames.map((profileName) => {
      const status = profileStatusByName.get(profileName) || {};
      const rows = processBlocks
        .filter((row) => (row.profile || profile) === profileName)
        .sort((a, b) => String(b.finished_at || b.started_at || b.created_at || "").localeCompare(String(a.finished_at || a.started_at || a.created_at || "")));
      const pending = Number(status.pending_messages || 0);
      const running = Boolean(status.running) || rows.some((row) => statusTone(row.status) === "running");
      const paused = Boolean(status.paused);
      const autoEnabled = Boolean(status.auto_enabled);
      const errored = Boolean(status.stale || status.phase === "error") || rows.some((row) => statusTone(row.status) === "error");
      const tone = errored ? "error" : running ? "running" : pending > 0 || paused ? "pending" : "done";
      const autoText = paused ? "paused" : autoEnabled ? "auto on" : "auto off";
      const queueText = pending > 0 ? `${fmt(pending)} messages waiting` : "up to date";
      const lastAt =
        (rows[0] && (rows[0].finished_at || rows[0].started_at || rows[0].created_at)) ||
        status.last_finished_at ||
        status.last_started_at ||
        "";
      return {
        profile: profileName,
        status,
        rows,
        pending,
        running,
        paused,
        autoEnabled,
        tone,
        autoText,
        queueText,
        lastAt,
      };
    }).sort((a, b) => {
      const toneRank = { running: 0, error: 1, pending: 2, done: 3 };
      const rankDiff = (toneRank[a.tone] ?? 9) - (toneRank[b.tone] ?? 9);
      if (rankDiff) return rankDiff;
      if (a.profile === profile) return -1;
      if (b.profile === profile) return 1;
      return a.profile.localeCompare(b.profile);
    });
    const renderProcessBlock = (job, idx) => {
      const tone = statusTone(job.status);
      const current = isCurrentProcess(job);
      const finishText = job.finished_at
        ? `finished ${relativeTime(job.finished_at)}`
        : tone === "running"
          ? "running now"
          : "finish not recorded";
      return h(
        "details",
        {
          className: `mp-process-block is-${tone} ${current ? "is-current" : ""}`,
          key: job.id,
          open: current || idx < 2 || tone === "error",
        },
        h(
          "summary",
          null,
          h(
            "div",
            { className: "mp-process-row-main" },
            h("span", { className: `mp-status-pill is-${tone}` }, statusLabel(job.status)),
            h(
              "div",
              { className: "mp-process-title-stack" },
              h("strong", null, processTitle(job)),
              h("small", { title: job.id || "" }, processTechnicalLabel(job)),
            ),
          ),
          h(
            "div",
            { className: "mp-process-row-meta" },
            h("span", null, job.all_profiles ? "all profiles" : job.profile || "—"),
            h("span", null, `model ${shorten(job.model || activeModelLabel, 64)}`),
            h("span", { title: job.started_at || job.created_at ? shortDateTime(job.started_at || job.created_at) : "" }, `started ${relativeTime(job.started_at || job.created_at)}`),
            h("span", { title: job.finished_at ? shortDateTime(job.finished_at) : "" }, finishText),
            h("span", null, `${fmt((job.events || []).length)} events`),
          ),
        ),
        job.events && job.events.length
          ? h(
              "div",
              { className: "mp-process-block-events" },
              job.events.slice().reverse().map((event, eventIdx) => {
                const eventToneValue = eventTone(event);
                return h(
                  "div",
                  { className: `mp-event-row is-${eventToneValue}`, key: `${event.at || eventIdx}:${eventIdx}` },
                  h("div", { className: "mp-event-time", title: event.at ? shortDateTime(event.at) : "" }, relativeTime(event.at)),
                  h("span", { className: `mp-status-pill is-${eventToneValue} is-small` }, eventLabel(event)),
                  h(
                    "div",
                    { className: "mp-event-body" },
                    h("strong", null, eventMessage(event)),
                    h("span", null, eventMetaText(event) || "—"),
                  ),
                );
              }),
            )
          : h("p", { className: "mp-muted" }, "No persisted events for this process."),
      );
    };
    const processErrors = [
      activeJob && activeJob.error ? { source: activeJob.id, message: activeJob.error } : null,
      consolidator && consolidator.last_error ? { source: "selected profile", message: consolidator.last_error } : null,
      scheduler && scheduler.last_error ? { source: "auto scheduler", message: scheduler.last_error } : null,
      ...allRecentEvents
        .filter((event) => event && event.level === "error")
        .map((event) => ({ source: event.at || "event", message: event.message || "" })),
    ].filter(Boolean);
    const displayGraph = useMemo(() => {
      if (!palace && !activeFocus && !query.trim()) {
        return buildTopicGraph(profile, palaces, selectedStats);
      }
      if (palace && !activeFocus && !query.trim() && graph) {
        return activeCluster ? buildClusterEntityGraph(graph, activeCluster) : buildAreaGraph(graph, palace);
      }
      return graph;
    }, [profile, palaces, selectedStats, palace, activeFocus, query, graph, activeCluster]);
    const selectedNode = displayGraph && displayGraph.nodes ? displayGraph.nodes.find((node) => node.id === selectedId) : null;
    const changeProfile = (next) => {
      setFocusStack([]);
      setActiveCluster("");
      setSelectedId("");
      setProfile(next);
    };
    const changeTopic = (next) => {
      setFocusStack([]);
      setActiveCluster("");
      setSelectedId("");
      setPalace(next);
    };
    const openCluster = (next) => {
      setFocusStack([]);
      setSelectedId("");
      setActiveCluster(next);
    };
    const focusNode = (node) => {
      if (!node || !node.id) return;
      setFocusStack((stack) => [...stack, { id: node.id, label: node.label || node.id, depth: 1 }]);
    };
    const goDeeper = (node) => {
      if (!node || !node.id) return;
      setFocusStack((stack) => {
        const current = stack[stack.length - 1];
        if (current && current.id === node.id) {
          return [...stack.slice(0, -1), { ...current, depth: Math.min(Number(current.depth || 1) + 1, 3) }];
        }
        return [...stack, { id: node.id, label: node.label || node.id, depth: 2 }];
      });
    };
    const openProcessPage = () => {
      window.location.hash = "process";
      setRoute("process");
    };
    const closeProcessPage = () => {
      if (window.location.hash === "#process") {
        history.pushState("", document.title, window.location.pathname + window.location.search);
      }
      setRoute("graph");
    };

    if (route === "process") {
      return h(
        "div",
        { className: "mp-root mp-process-page" },
        h(
          "section",
          { className: "mp-process-page-head" },
          h(
            "div",
            null,
            h("h2", null, "MemPalace Process"),
            h("p", null, `${activeJob ? `Process ${activeJob.id}` : `Profile ${profile}`} | model ${activeModelLabel}`),
          ),
          h("button", { className: "mp-process-back-button", onClick: closeProcessPage, type: "button" }, "Back to graph"),
        ),
        h(
          "section",
          { className: "mp-consolidator" },
          h(
            "div",
            { className: "mp-process-hero" },
            h(
              "div",
              { className: "mp-process-pct" },
              h("strong", null, heroMetricText),
              h("span", { className: `mp-status-pill is-${statusTone(activeRunStateText)}` }, statusLabel(activeRunStateText)),
            ),
            h(
              "div",
              { className: "mp-process-main" },
              h(
                "div",
                { className: "mp-process-title" },
                h("strong", null, activeLabel),
                activeJob ? h("code", null, activeJob.id) : null,
                h("span", { className: "mp-model-live" }, "Model ", h("b", null, activeModelLabel)),
              ),
              h(
                "div",
                { className: "mp-process-bar", title: hasActiveRun ? `${fmt(activeProcessed)} of ${fmt(activeTotal)} messages processed` : "No extractor job is running" },
                h("span", { style: { width: `${Math.max(0, Math.min(100, heroBarWidth))}%` } }),
              ),
              h(
                "div",
                { className: "mp-process-grid" },
                h("div", null, h("span", null, "Profile"), h("b", null, activeProfileName || "—")),
                h("div", null, h("span", null, "Palace"), h("b", null, activePalaceName)),
                h("div", null, h("span", null, "Phase"), h("b", null, activePhase)),
                h("div", null, h("span", null, "Batch"), h("b", null, activeBatch)),
                h("div", null, h("span", null, "Cursor"), h("b", null, fmt(activeCursor))),
                h("div", null, h("span", null, "Messages"), h("b", null, `${fmt(activeProcessed)} / ${fmt(activeTotal)}`)),
                h("div", null, h("span", null, "Pending"), h("b", null, fmt(activePending))),
                h("div", null, h("span", null, "Current messages"), h("b", null, hasActiveRun && activeBatchMessages ? fmt(activeBatchMessages) : "—")),
                h("div", null, h("span", null, "Entities"), h("b", null, hasActiveRun ? fmt(activeBatchEntities) : "—")),
                h("div", null, h("span", null, "Triples"), h("b", null, hasActiveRun ? fmt(activeBatchTriples) : "—")),
                h("div", null, h("span", null, "Skipped"), h("b", null, hasActiveRun ? fmt(activeBatchSkipped) : "—")),
                h("div", null, h("span", null, "Last update"), h("b", { title: lastActivityAt ? shortDateTime(lastActivityAt) : "" }, lastActivityAt ? relativeTime(lastActivityAt) : "—")),
                h("div", null, h("span", null, "Age"), h("b", null, activityAgeSec ? `${fmt(Math.floor(activityAgeSec / 60))} min ${fmt(activityAgeSec % 60)} sec` : "—")),
                activeProgress.profile_index !== undefined
                  ? h("div", null, h("span", null, "Profiles"), h("b", null, `${fmt(Number(activeProgress.profile_index) + 1)} / ${fmt(activeProgress.profiles_total || 0)}`))
                  : null,
              ),
            ),
          ),
          h(
            "div",
            { className: "mp-model-strip" },
            h(
              "div",
              { className: "mp-model-primary" },
              h("span", null, hasActiveRun ? "Running model" : "Configured extractor model"),
              h("strong", null, activeModelLabel),
              h("em", null, `${extractorTask} / ${extractorAdapter}`),
            ),
            h(
              "div",
              { className: "mp-model-fallbacks" },
              h("span", null, "Fallback chain"),
              h("strong", null, fallbackModelLabel),
            ),
            h(
              "div",
              { className: "mp-model-validator" },
              h("span", null, "Validation model"),
              h("strong", null, validatorModelLabel),
            ),
          ),
          h(
            "div",
            { className: "mp-process-page-actions" },
            h("button", { className: "mp-action-primary", onClick: () => startExtractor({ allProfiles: true }), disabled: busy || anyExtractorRunning, type: "button" }, anyExtractorRunning ? "Running" : "Start"),
            h("button", { onClick: () => runConsolidator({ dryRun: true, allProfiles: true }), disabled: busy, type: "button" }, "Preview all"),
            h("button", { onClick: () => runConsolidator({ backfill: true, allProfiles: true }), disabled: busy, type: "button" }, "Full rebuild all"),
            h("button", { onClick: () => runValidator({ allProfiles: true }), disabled: busy, type: "button" }, "Run validator"),
            h(
              "button",
              {
                className: `mp-auto-switch ${allAutoEnabled ? "is-on" : "is-off"}`,
                role: "switch",
                "aria-checked": allAutoEnabled ? "true" : "false",
                onClick: () => setGlobalAuto(!allAutoEnabled),
                disabled: busy,
                type: "button",
              },
              h("span", { "aria-hidden": "true" }),
              h("strong", null, allAutoEnabled ? "Auto on" : "Auto off"),
              h("small", null, `${fmt(autoEnabledCount)} / ${fmt(profileStatusCount)} profiles`),
            ),
          ),
          h(
            "div",
            { className: "mp-process-details is-page" },
            h(
              "div",
              { className: "mp-process-details-head" },
              h("h3", null, "Extractor details"),
              h("span", null, activeJob ? `process ${activeJob.id}` : `profile ${profile}`),
            ),
            h(
              "div",
              { className: "mp-detail-grid" },
              h("div", null, h("span", null, "Task"), h("b", null, extractorTask)),
              h("div", null, h("span", null, "Adapter"), h("b", null, extractorAdapter)),
              h("div", null, h("span", null, "Started"), h("b", { title: shortDateTime((activeJob && activeJob.started_at) || (consolidator && consolidator.last_started_at)) }, relativeTime((activeJob && activeJob.started_at) || (consolidator && consolidator.last_started_at)))),
              h("div", null, h("span", null, "Updated"), h("b", { title: shortDateTime((activeJob && activeJob.updated_at) || (consolidator && consolidator.last_finished_at)) }, relativeTime((activeJob && activeJob.updated_at) || (consolidator && consolidator.last_finished_at)))),
              h("div", null, h("span", null, "Stalled"), h("b", null, isExtractorStalled ? "yes" : "no")),
              h("div", null, h("span", null, "Workers"), h("b", null, `${fmt((extractorConfig && extractorConfig.workers) || 5)} palace / 2 profile`)),
              h("div", null, h("span", null, "Mode"), h("b", null, activeJob && activeJob.all_profiles ? "all profiles" : "selected profile")),
              h("div", null, h("span", null, "Auto"), h("b", null, autoSummaryText)),
              h("div", null, h("span", null, "Scheduler tick"), h("b", { title: scheduler.last_tick_at ? shortDateTime(scheduler.last_tick_at) : "" }, schedulerLastTick)),
              h("div", null, h("span", null, "Next tick"), h("b", { title: scheduler.next_tick_at ? shortDateTime(scheduler.next_tick_at) : "" }, schedulerNextTick)),
              h("div", null, h("span", null, "Scheduler action"), h("b", null, schedulerAction)),
            ),
            h(
              "div",
              { className: "mp-detail-section" },
              h("strong", null, "Models"),
              h(
                "div",
                { className: "mp-model-stack" },
                extractorModels.map((row, idx) =>
                  h(
                    "div",
                    { className: `mp-model-card ${idx === 0 ? "is-primary" : ""}`, key: `${row.role}:${row.provider}:${row.model}` },
                    h("span", null, idx === 0 ? (hasActiveRun ? "Running now" : "Primary extractor") : row.role || "Fallback"),
                    h("strong", null, `${row.provider || "auto"} / ${row.model || "main model"}`),
                  ),
                ),
              ),
            ),
            h(
              "div",
              { className: "mp-detail-section" },
              h("strong", null, "Process history"),
              processGroups && processGroups.length
                ? h(
                    "div",
                    { className: "mp-profile-groups" },
                    processGroups.map((group) => {
                      const status = group.status || {};
                      const open = group.profile === profile || group.tone !== "done";
                      return h(
                        "details",
                        {
                          className: `mp-profile-group is-${group.tone}`,
                          key: group.profile,
                          open,
                        },
                        h(
                          "summary",
                          null,
                          h(
                            "div",
                            { className: "mp-profile-group-main" },
                            h("span", { className: `mp-status-pill is-${group.tone}` }, group.tone === "pending" ? "Queue" : statusLabel(group.tone)),
                            h(
                              "div",
                              null,
                              h("strong", null, group.profile),
                              h("small", null, `${group.queueText} · ${group.autoText}`),
                            ),
                          ),
                          h(
                            "div",
                            { className: "mp-profile-group-meta" },
                            h("span", null, `${fmt(status.total_messages || 0)} total`),
                            h("span", null, `${fmt(status.cursor_message_id || 0)} cursor`),
                            h("span", null, `${fmt(status.max_message_id || 0)} latest`),
                            h("span", { title: group.lastAt ? shortDateTime(group.lastAt) : "" }, group.lastAt ? `last ${relativeTime(group.lastAt)}` : "no runs"),
                            h("span", null, `${fmt(group.rows.length)} runs`),
                          ),
                          h(
                            "div",
                            {
                              className: "mp-profile-group-actions",
                              onClick: (event) => event.stopPropagation(),
                            },
                            h(
                              "button",
                              {
                                className: group.running ? "is-pause" : "is-start",
                                disabled: busy || (!group.running && !group.paused && group.pending <= 0),
                                onClick: (event) => {
                                  event.preventDefault();
                                  event.stopPropagation();
                                  if (group.running) {
                                    setProfilePaused(group.profile, true);
                                  } else {
                                    startExtractor({ profile: group.profile });
                                  }
                                },
                                type: "button",
                              },
                              group.running ? "Pause" : "Start",
                            ),
                          ),
                        ),
                        group.rows.length
                          ? h("div", { className: "mp-process-blocks" }, group.rows.slice(0, 8).map((job, idx) => renderProcessBlock(job, idx)))
                          : h("p", { className: "mp-muted" }, "No process history recorded for this profile."),
                      );
                    }),
                  )
                : h("p", null, "No process history recorded yet."),
            ),
            h(
              "div",
              { className: "mp-detail-section" },
              h("strong", null, "Errors"),
              processErrors.length
                ? h(
                    "div",
                    { className: "mp-error-list" },
                    processErrors.map((row, idx) =>
                      h(
                        "div",
                        { className: "mp-error-row", key: `${row.source}:${idx}` },
                        h("span", null, row.source),
                        h("strong", null, row.message),
                      ),
                    ),
                  )
                : h("p", null, "No errors recorded."),
            ),
          ),
          palaces && palaces.length
            ? h(
                "div",
                { className: "mp-process-palaces" },
                h(
                  "div",
                  { className: "mp-process-palaces-head" },
                  h("strong", null, `Profile palaces: ${profile}`),
                  h("span", null, `${fmt(palaces.length)} palace(s)`),
                ),
                h(
                  "table",
                 null,
                  h("thead", null, h("tr", null, h("th", null, "Palace"), h("th", null, "Entities"), h("th", null, "Triples"), h("th", null, "Last update"))),
                  h(
                    "tbody",
                    null,
                    palaces.slice(0, 20).map((row) =>
                      h(
                        "tr",
                        { key: row.palace },
                        h("td", null, row.palace),
                        h("td", null, fmt(row.entity_count || 0)),
                        h("td", null, fmt(row.triple_count || 0)),
                        h("td", null, row.modified_at ? String(row.modified_at).replace("T", " ").slice(0, 16) : "—"),
                      ),
                    ),
                  ),
                ),
              )
            : null,
        ),
      );
    }

    return h(
      "div",
      { className: "mp-root" },
      h(
        "section",
        { className: "mp-toolbar" },
        h(
          "div",
          { className: "mp-title" },
          h("h2", null, "MemPalace"),
          h("p", null, palace ? `Palace filter: ${palace}` : "All profile memory from chat history + curated notes"),
        ),
        h(
          "div",
          { className: "mp-controls" },
          h(
            NativeSelect,
            { label: "Profile", value: profile, onChange: changeProfile },
            profiles.map((row) => h("option", { key: row.name, value: row.name }, row.name)),
          ),
          h(
            NativeSelect,
            { label: "Palace", value: palace, onChange: changeTopic },
            [
              h("option", { key: "__all", value: "" }, "All profile memory"),
              ...palaces.map((row) => h("option", { key: row.palace, value: row.palace }, row.palace)),
            ],
          ),
          h(Input, {
            className: "mp-query",
            value: query,
            onChange: (e) => setQuery(e.target.value),
            onKeyDown: (e) => {
              if (e.key === "Enter") runSearch();
            },
            placeholder: "Search graph",
          }),
          h(Button, { onClick: runSearch, disabled: busy, title: "Search MemPalace" }, "Search"),
          h(Button, { onClick: loadGraph, disabled: busy, title: "Reload graph" }, "Refresh"),
          h(
            "details",
            { className: "mp-admin" },
            h("summary", null, "Admin"),
            h(
              "div",
              { className: "mp-admin-actions" },
              h(Button, { onClick: generate, disabled: busy, title: "Sync curated Hermes memory markdown into the graph" }, "Sync"),
              h(Button, { onClick: rebuildProfile, disabled: busy, title: "Fully rebuild this profile from chat history, then add curated memory overlay" }, "Rebuild profile"),
              h(Button, { onClick: rebuildAll, disabled: busy, title: "Fully rebuild every profile from chat history, then add curated memory overlays" }, "Rebuild all"),
              h(Button, { onClick: previewNoise, disabled: busy, title: "Preview low-signal memory candidates" }, "Preview noise"),
              h(Button, { onClick: cleanNoise, disabled: busy || !noisePreview || !Number(noisePreview.total_candidates || 0), title: "Delete previewed low-signal memory with backup" }, "Clean noise"),
            ),
          ),
        ),
      ),
      error ? h("div", { className: "mp-banner mp-error" }, error) : null,
      message ? h("div", { className: "mp-banner mp-ok" }, message) : null,
      noisePreview && noisePreview.candidates && noisePreview.candidates.length
        ? h(
            "section",
            { className: "mp-noise-preview" },
            h("strong", null, `Noise candidates: ${fmt(noisePreview.total_candidates)}`),
            noisePreview.candidates.slice(0, 8).map((item) =>
              h(
                "span",
                { key: `${item.palace}:${item.id}` },
                `${item.palace} / ${item.label} — ${item.reason}`,
              ),
            ),
          )
        : null,
      h(
        "section",
        { className: "mp-consolidator" },
        h(
          "div",
          { className: "mp-process-hero" },
          h(
            "div",
            { className: "mp-process-pct" },
            h("strong", null, heroMetricText),
            h("span", { className: `mp-status-pill is-${statusTone(activeRunStateText)}` }, statusLabel(activeRunStateText)),
          ),
          h(
            "div",
            { className: "mp-process-main" },
            h(
              "div",
              { className: "mp-process-title" },
              h("strong", null, activeLabel),
              activeJob ? h("code", null, activeJob.id) : null,
              h("span", { className: "mp-model-live" }, "Model ", h("b", null, activeModelLabel)),
              h(
                "button",
                {
                  className: "mp-process-open-button",
                  onClick: openProcessPage,
                  type: "button",
                },
                "Open process page",
              ),
            ),
            h(
              "div",
              { className: "mp-process-bar", title: hasActiveRun ? `${fmt(activeProcessed)} of ${fmt(activeTotal)} messages processed` : "No extractor job is running" },
              h("span", { style: { width: `${Math.max(0, Math.min(100, heroBarWidth))}%` } }),
            ),
            h(
              "div",
              { className: "mp-process-grid is-summary" },
              h("div", null, h("span", null, "Profile"), h("b", null, activeProfileName || "—")),
              h("div", null, h("span", null, "Model"), h("b", null, activeModelLabel)),
              h("div", null, h("span", null, "Auto"), h("b", null, autoSummaryText)),
              h("div", null, h("span", null, "Phase"), h("b", null, activePhase)),
              h("div", null, h("span", null, "Messages"), h("b", null, `${fmt(activeProcessed)} / ${fmt(activeTotal)}`)),
              h("div", null, h("span", null, "Pending"), h("b", null, fmt(activePending))),
              h("div", null, h("span", null, "Last update"), h("b", { title: lastActivityAt ? shortDateTime(lastActivityAt) : "" }, lastActivityAt ? relativeTime(lastActivityAt) : "—")),
              h("div", null, h("span", null, "Next auto tick"), h("b", { title: scheduler.next_tick_at ? shortDateTime(scheduler.next_tick_at) : "" }, schedulerNextTick)),
            ),
          ),
        ),
      ),
      h(
        "section",
        { className: "mp-stats" },
        h(StatCell, { label: "Palaces", value: palace ? 1 : (selectedStats.topic_count || palaces.length) }),
        h(StatCell, { label: "Entities", value: selectedStats.entity_count }),
        h(StatCell, { label: "Triples", value: selectedStats.triple_count }),
        h(StatCell, { label: "Nodes", value: selectedStats.node_count }),
        h(StatCell, { label: "Edges", value: selectedStats.edge_count }),
      ),
      h(
        "section",
        { className: "mp-breadcrumb" },
        h(
          "button",
          {
            className: !palace && !activeFocus ? "is-active" : "",
            onClick: () => changeTopic(""),
          },
          "Palaces",
        ),
        palace
          ? h(
              "button",
              {
                className: palace && !activeCluster && !activeFocus ? "is-active" : "",
                onClick: () => {
                  setFocusStack([]);
                  setActiveCluster("");
                  setSelectedId("");
                },
              },
              topicLabel(palace),
            )
          : null,
        activeCluster
          ? h(
              "button",
              {
                className: activeCluster && !activeFocus ? "is-active" : "",
                onClick: () => {
                  setFocusStack([]);
                  setSelectedId("");
                },
              },
              (CLUSTERS.find((item) => item.key === activeCluster) || {}).label || activeCluster,
            )
          : null,
        focusStack.map((item, index) =>
          h(
            "button",
            {
              key: `${item.id}-${index}`,
              className: index === focusStack.length - 1 ? "is-active" : "",
              onClick: () => setFocusStack((stack) => stack.slice(0, index + 1)),
            },
            `${shorten(item.label, 36)} d${item.depth || 1}`,
          ),
        ),
      ),
      h(
        "section",
        { className: "mp-profile-strip" },
        h("span", null, profile),
        activeProfileTotal
          ? h("strong", null, `${fmt(activeProfileTotal.palaces)} palaces · ${fmt(activeProfileTotal.entities)} entities · ${fmt(activeProfileTotal.triples)} triples`)
          : null,
        h(
          "button",
          { className: "mp-link-button", onClick: () => setShowSplit((value) => !value) },
          showSplit ? "Hide profile split" : "Show profile split",
        ),
      ),
      showSplit
        ? h(ProfileMatrix, {
            matrix,
            activeProfile: profile,
            onPickProfile: changeProfile,
            onPickTopic: changeTopic,
          })
        : null,
      h(
        "section",
        { className: "mp-layout" },
        h(
          "main",
          { className: "mp-canvas-panel" },
          busy ? h("div", { className: "mp-loading" }, "Loading") : null,
          h(GraphView, {
            graph: displayGraph,
            selectedId,
            onSelect: setSelectedId,
            onOpenTopic: changeTopic,
            onOpenCluster: openCluster,
            onFocusNode: focusNode,
          }),
        ),
        h(
          "div",
          { className: "mp-side" },
          h(EntityPanel, {
            node: selectedNode,
            tree: nodeTree,
            onSelectNode: (nextId) => setSelectedId(nextId),
            onFocusNode: focusNode,
            onGoDeeper: goDeeper,
            canGoDeeper: !activeFocus || Number(activeFocus.depth || 1) < 3,
          }),
          h(SearchResults, {
            results,
            onPickTopic: (next) => {
              changeTopic(next);
            },
          }),
        ),
      ),
    );
  }

  window.__HERMES_PLUGINS__.register("mempalace", MempalacePage);
})();
