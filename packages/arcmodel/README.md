<div align="center">

# 🧠 arcmodel

### **Model Management and Routing for Arc**
*Future home of multi-tenant model selection, capability discovery, and tiered routing.*

[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-002550.svg)](https://opensource.org/licenses/Apache-2.0)
[![Status: Scaffolding](https://img.shields.io/badge/status-early_scaffolding-F68D2E.svg)](#status)

</div>

---

## ✨ What is arcmodel?

`arcmodel` is reserved as the home of cross-tenant model selection, capability discovery, and tiered routing logic — concerns that are currently embedded in `arcllm` configs but will eventually have their own surface.

> ⚠️ **Status: early scaffolding.** The package installs and exports `__version__` only. No public API yet.

---

## ⭐ Planned Top Features

What `arcmodel` will bring to multi-tenant model management:

### **Intelligent Routing**
- **Capability-aware model selection** — Automatically picks the right model for each call (tools, vision, long context, JSON mode)
- **Per-call eligibility rules** — "this call requires SOC2-certified hosting" → only matching models considered
- **Cost-bounded selection** — Automatically downgrades to cheaper models when budget thresholds approached

### **Multi-Tenant Management**
- **Per-organization model catalogs** — Separate model registries with ACLs; orgs see only their approved models
- **Tier-aware fallback** — Federal-only models for sensitive calls, open models for general use
- **Capability discovery** — Query providers for current model lineup, prices, and context windows

### **Advanced Features**
- **Budget tracking** — Running budget counters with automatic downgrades; prevents cost overruns
- **Model health monitoring** — Track provider availability and latency; route around degraded models
- **Custom routing rules** — Per-organization routing policies; compliance requirements enforced

---

## 🏗️ Where It Fits

Reserved to sit beside `arcllm`, lifting routing and model-selection concerns out of provider configs. The dotted edge is planned, not yet wired.

```mermaid
flowchart TB
    classDef llm fill:#003B82,stroke:#002550,color:#FFFFFF
    classDef other fill:#E9EAEB,stroke:#7F7F7F,color:#0B1220

    arcllm[arcllm<br/>16 providers]:::llm -. "planned: routing" .-> arcmodel[arcmodel<br/>model selection · routing]:::other
```

---

## 🔭 Future Scope

- **Capability-aware routing** — pick the right model for each call (tools / vision / long context)
- **Tier-aware fallback** — federal-only models for sensitive calls, open models for the rest
- **Cost-bounded selection** — automatically downgrade to a cheaper model when the running budget approaches a threshold
- **Multi-tenant model registries** — per-org model catalogs with ACLs
- **Capability discovery** — query providers for current model lineup, prices, context windows
- **Per-call eligibility** — "this call requires SOC2-certified hosting" → only matching models considered

---

## 🧪 Status

- **Status:** scaffolding only — no public API yet
- **License:** Apache 2.0 · Copyright © 2025-2026 BlackArc Systems
