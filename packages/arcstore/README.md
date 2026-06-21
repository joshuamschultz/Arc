<div align="center">

# 🗄️ arcstore

### **The Durable Storage Foundation for Arc**
*Operational, always-on persistence other layers read and write through.*

[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-002550.svg)](https://opensource.org/licenses/Apache-2.0)
[![Tests](https://img.shields.io/badge/tests-passing-0055BC.svg)](#)
[![Coverage](https://img.shields.io/badge/coverage-high-003B82.svg)](#)
[![Strict mypy](https://img.shields.io/badge/mypy-strict-0073FE.svg)](#)

</div>

---

## ✨ What is arcstore?

`arcstore` is the operational, durable storage foundation of the Arc stack. It is the
always-on data plane the rest of Arc reads and writes through — `arcllm`, `arcrun`,
`arcagent`, and the UIs all persist and query their operational records here.

`arcstore` depends only on `arctrust`. It imports nothing else from Arc, sitting just
above the cryptographic floor so that every layer above it has a single, consistent
place to durably record and retrieve what happened.

---

## 🏗️ Where It Fits

```mermaid
flowchart TB
    classDef found fill:#002550,stroke:#001A38,color:#FFFFFF
    classDef llm fill:#003B82,stroke:#002550,color:#FFFFFF
    classDef runtime fill:#0055BC,stroke:#003B82,color:#FFFFFF
    classDef agent fill:#0073FE,stroke:#0055BC,color:#FFFFFF
    classDef surface fill:#5A9CFF,stroke:#003B82,color:#002550
    classDef entry fill:#D6E6FF,stroke:#0073FE,color:#002550

    arcstore[arcstore<br/>durable operational storage]:::found
    arctrust[arctrust]:::found
    arcllm[arcllm]:::llm
    arcrun[arcrun]:::runtime
    arcagent[arcagent]:::agent
    arcui[arcui]:::surface
    arccli[arccli]:::entry

    arcstore --> arctrust
    arcllm --> arcstore
    arcrun --> arcstore
    arcagent --> arcstore
    arcui --> arcstore
    arccli --> arcstore
```

`arcstore` depends only on `arctrust`; every operational layer above it reads and
writes through `arcstore`.

---

## 📄 License

Apache 2.0 · Copyright © 2025-2026 BlackArc Systems.
