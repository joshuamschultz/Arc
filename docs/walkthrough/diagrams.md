# Mermaid Diagram Index

> **Walkthrough**  ·  Understand  ·  page 14 of 14  
> **For** Anyone who needs to understand how Arc works  
> [← Data flows](data-flows.md)  ·  [Docs home](../README.md)

---

## Architecture Diagrams

### Package Architecture

```mermaid
flowchart TB
    classDef surface fill:#5A9CFF,stroke:#003B82,color:#002550
    classDef agent fill:#0073FE,stroke:#0055BC,color:#FFFFFF
    classDef runtime fill:#0055BC,stroke:#003B82,color:#FFFFFF
    classDef llm fill:#003B82,stroke:#002550,color:#FFFFFF
    classDef found fill:#002550,stroke:#001A38,color:#FFFFFF

    subgraph Surface Layer
        arcgateway[arcgateway]:::surface
        arcui[arcui]:::surface
    end

    subgraph Entry Layer
        arccli[arccli]:::surface
    end

    subgraph Agent Layer
        arcagent[arcagent]:::agent
        arcteam[arcteam]:::agent
        arcmemory[arcmemory]:::agent
        arcskill[arcskill]:::agent
        arcskill-improver[arcskill.improver]:::agent
    end

    subgraph Runtime Layer
        arcrun[arcrun]:::runtime
        arcprompt[arcprompt]:::runtime
    end

    subgraph LLM Layer
        arcllm[arcllm]:::llm
        arcmodel[arcmodel]:::llm
    end

    subgraph Foundation Layer
        arctrust[arctrust]:::found
        arcstore[arcstore]:::found
        arcmas[arcmas]:::found
    end
```

**Found in:** [PACKAGE_INDEX.md](../building/package-index.md)

---

### Layering Law

```mermaid
flowchart TB
    classDef entry   fill:#D6E6FF,stroke:#0073FE,color:#002550
    classDef surface fill:#5A9CFF,stroke:#003B82,color:#002550
    classDef agent   fill:#0073FE,stroke:#0055BC,color:#FFFFFF
    classDef runtime fill:#0055BC,stroke:#003B82,color:#FFFFFF
    classDef llm     fill:#003B82,stroke:#002550,color:#FFFFFF
    classDef found   fill:#002550,stroke:#001A38,color:#FFFFFF

    CLI["arccli / arctui — entry points"]
    GW["arcgateway — channel sessions"]
    UI["arcui — observe plus chat"]
    AGENT["arcagent — tools, skills, memory-as-tools"]
    RUN["arcrun — the execution loop"]
    LLM["arcllm — provider calls"]
    PROMPT["arcprompt — signed prompts"]
    SKILL["arcskill — signed skill hub"]
    MEMORY["arcmemory — analogical memory"]
    TEAM["arcteam — multi-agent bus"]
    STORE["arcstore — spool plus PostgreSQL operational store"]
    TRUST["arctrust — identity, sign, policy, WORM"]
```

**Found in:** [PACKAGE_INDEX.md](../building/package-index.md)

---

### Two Planes Architecture

```mermaid
flowchart LR
    classDef entry   fill:#D6E6FF,stroke:#0073FE,color:#002550
    classDef surface fill:#5A9CFF,stroke:#003B82,color:#002550
    classDef agent   fill:#0073FE,stroke:#0055BC,color:#FFFFFF
    classDef runtime fill:#0055BC,stroke:#003B82,color:#FFFFFF
    classDef found   fill:#002550,stroke:#001A38,color:#FFFFFF

    subgraph OBSERVE["Observe — read path, no push"]
        direction TB
        W["arcrun / arctrust write durable files"] --> ING["arcstore StoreIngest — backfill plus tail"]
    ING --> SQL["PostgresBackend operational store"]
        SQL --> REST["arcui REST — reads on demand"]
    end

    subgraph INTERACT["Interact — the live sockets"]
        direction TB
        CHAT["ws chat agent_id — bidirectional turn stream"]
        TEAMWS["ws team — read-only bus stream plus human post forward"]
    end
```

**Found in:** [PACKAGE_INDEX.md](../building/package-index.md)

---

## Security Diagrams

### Four Pillars

```mermaid
flowchart LR
    classDef pillar fill:#002550,stroke:#001A38,color:#FFFFFF
    classDef pkg fill:#0055BC,stroke:#003B82,color:#FFFFFF

    subgraph "Arc Security Model"
        ID[Identity<br/>Ed25519 DID]:::pillar
        SIG[Sign<br/>Sigstore]:::pillar
        AUTH[Authorize<br/>Policy Pipeline]:::pillar
        AUDIT[Audit<br/>Dual WORM]:::pillar
    end

    ID -.->|DID| TRUST[arctrust]:::pkg
    SIG -.->|Sign| TRUST
    AUTH -.->|Policy| TRUST
    AUDIT -.->|Audit| TRUST
```

**Found in:** [SECURITY.md](../reference/security.md)

---

### Tier Comparison

```mermaid
flowchart LR
    classDef personal fill:#D6E6FF,stroke:#0073FE,color:#002550
    classDef enterprise fill:#5A9CFF,stroke:#003B82,color:#002550
    classDef federal fill:#002550,stroke:#001A38,color:#FFFFFF

    subgraph "Security Tiers"
        P[Personal<br/>Relaxed]:::personal
        E[Enterprise<br/>Standard]:::enterprise
        F[Federal<br/>Strict]:::federal
    end
```

**Found in:** [SECURITY.md](../reference/security.md), [TIERS_AND_PRESETS.md](../reference/tiers-and-presets.md)

---

### Lethal Trifecta

```mermaid
flowchart LR
    classDef danger fill:#F68D2E,stroke:#C06000,color:#FFFFFF
    classDef safe fill:#0073FE,stroke:#0055BC,color:#FFFFFF

    subgraph "Lethal Trifecta Check"
        PRIV[Private Data]:::danger
        COMMS[External Comms]:::danger
        INPUT[Untrusted Input]:::danger
    end

    PRIV & COMMS & INPUT -->|All three = BLOCKED| BLOCK[❌ Requires Approval]:::danger
    PRIV & COMMS -->|Two = Allowed| OK1[✓ Allowed]:::safe
    PRIV & INPUT -->|Two = Allowed| OK2[✓ Allowed]:::safe
```

**Found in:** [SECURITY.md](../reference/security.md)

---

## Data Flow Diagrams

### Turn Flow

```mermaid
sequenceDiagram
    participant User
    participant Agent
    participant LLM
    participant Tool

    User->>Agent: task
    Agent->>LLM: chat request
    LLM->>Agent: response + tool_calls
    Agent->>Tool: execute
    Tool->>Agent: result
    Agent->>LLM: follow-up
    LLM->>Agent: final response
    Agent->>User: result
```

**Found in:** [DATA_FLOW.md](data-flows.md)

---

### Memory Flow

```mermaid
flowchart TB
    classDef input fill:#D6E6FF,stroke:#0073FE,color:#002550
    classDef process fill:#0073FE,stroke:#0055BC,color:#FFFFFF
    classDef output fill:#002550,stroke:#001A38,color:#FFFFFF

    subgraph "Memory Pipeline"
        INPUT[Input]:::input
        PARSE[Parse]:::process
        ENTITY[Entity Store]:::output
        EPISODE[Episode Store]:::output
        DAILY[Daily Log]:::output
        INDEX[SQLite Index]:::process
        QUERY[Search]:::input
    end

    INPUT --> PARSE
    PARSE --> ENTITY
    PARSE --> EPISODE
    PARSE --> DAILY
    ENTITY & EPISODE & DAILY --> INDEX
    QUERY --> INDEX
```

**Found in:** [DATA_FLOW.md](data-flows.md)

---

### Skill Install Pipeline

```mermaid
flowchart LR
    classDef gate fill:#002550,stroke:#001A38,color:#FFFFFF
    classDef term fill:#D6E6FF,stroke:#0073FE,color:#002550

    A[Fetch]:::gate --> B[Sigstore]:::gate
    B --> C[Rekor]:::gate
    C --> D[CRL]:::gate
    D --> E[Static Scan]:::gate
    E --> F[Sandbox]:::gate
    F --> G[Atomic Activate]:::gate
    G --> H[Lock File]:::term
```

**Found in:** [PACKAGE_INDEX.md](../building/package-index.md), [BLUEPRINTS.md](../blueprints/blueprints.md)

---

## Scaling Diagrams

### Horizontal Scaling

```mermaid
flowchart LR
    classDef agent fill:#0073FE,stroke:#0055BC,color:#FFFFFF
    classDef natas fill:#002550,stroke:#001A38,color:#FFFFFF

    LoadBalancer[Load Balancer] --> Agent1[Agent 1]:::agent
    LoadBalancer --> Agent2[Agent 2]:::agent
    LoadBalancer --> Agent3[Agent 3]:::agent
    
    Agent1 <--> NATS[NATS]:::natas
    Agent2 <--> NATS
    Agent3 <--> NATS
```

**Found in:** [PERFORMANCE.md](../building/performance.md)

---

## Deployment Diagrams

### Docker Deployment

```mermaid
flowchart TB
    classDef svc fill:#0073FE,stroke:#0055BC,color:#FFFFFF
    classDef storage fill:#002550,stroke:#001A38,color:#FFFFFF

    subgraph "Docker Compose"
        ARC[arc-agent]:::svc
        NATS[NATS]:::svc
        REDIS[Redis]:::storage
    end

    ARC --> NATS
    ARC --> REDIS
```

**Found in:** [DEPLOYMENT.md](../runbooks/deploy/overview.md)

---

## Reading Paths

### Documentation Paths

```mermaid
flowchart TB
    classDef entry   fill:#D6E6FF,stroke:#0073FE,color:#002550
    classDef surface fill:#5A9CFF,stroke:#003B82,color:#002550
    classDef agent   fill:#0073FE,stroke:#0055BC,color:#FFFFFF
    classDef runtime fill:#0055BC,stroke:#003B82,color:#FFFFFF
    classDef llm     fill:#003B82,stroke:#002550,color:#FFFFFF
    classDef found   fill:#002550,stroke:#001A38,color:#FFFFFF

    START["Why are you here?"]

    START --> A["I want to understand<br/>what Arc is"]
    START --> B["I'm writing code<br/>this week"]
    START --> C["I'm evaluating Arc<br/>for security"]
    START --> D["I'm adding a<br/>capability"]

    A --> A1["QUICKSTART.md"] --> A2["PACKAGE_INDEX.md"] --> A3["GLOSSARY.md"]
    B --> B1["DATA_FLOW.md"] --> B2["PACKAGE_INDEX.md"] --> B3["CONTRIBUTING.md"]
    C --> C1["SECURITY.md"] --> C2["DATA_FLOW.md"] --> C3["TIERS_AND_PRESETS.md"]
    D --> D1["IMPLEMENTATION_GUIDES.md"] --> D2["BLUEPRINTS.md"] --> D3["TIERS_AND_PRESETS.md"]

    class START entry
    class A,B,C,D surface
    class A1,A2,A3 agent
    class B1,B2,B3 runtime
    class C1,C2,C3 found
    class D1,D2,D3 llm
```

**Found in:** [README.md](../README.md)

---

## Next Steps

- [README.md](../README.md) - Main documentation index
- [PACKAGE_INDEX.md](../building/package-index.md) - Detailed architecture
