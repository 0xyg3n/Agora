# Multi-Machine ACP Mesh via WireGuard

> Status: **Architecture Ready — Implementation Straightforward**

## Concept

The ACP Event Bus currently runs on a single host (`ws://0.0.0.0:9090`). With WireGuard, multiple machines join a private mesh network and any machine can connect agents to the bus. This turns a single-host voice room into a distributed multi-machine agent network.

## Topology

```
Machine A (Primary VPS, 10.x.x.1)
├── ACP Event Bus (ws://10.x.x.1:9090)
├── LiveKit Server
├── Agora Frontend (:3210)
├── Agent container 1 (e.g. Hermes)
└── Agent container 2 (e.g. OpenClaw)

Machine B (GPU Workstation, 10.x.x.3)
├── GPU workloads (TTS, STT, local LLMs)
├── Additional agents
└── Connects to bus at ws://10.x.x.1:9090

Machine C (Cloud GPU, 10.x.x.4)
├── Heavy inference models
└── Connects to bus at ws://10.x.x.1:9090
```

## What This Enables

- **GPU offload**: TTS, STT, and local LLM inference on machines with GPUs while the VPS runs the bus and room
- **Distributed agents**: Agent containers on different machines sharing the same ACP bus for cross-session awareness
- **Scale horizontally**: Add machines to the WireGuard mesh, not by upgrading one server
- **All traffic encrypted**: WireGuard encrypts everything between machines

## Implementation

### To Connect a New Machine

1. Add WireGuard peer on the primary VPS and the new machine
2. Set `ACP_BUS_URL=ws://10.x.x.1:9090` in the agent's environment
3. For Docker containers: ensure the host routes WireGuard traffic (`iptables -A FORWARD -i wg0 -j ACCEPT`)
4. Agents on the new machine connect to the bus and subscribe/publish like local ones

### Container Access

Docker containers access the WireGuard network via host routing:
```bash
# On the host running containers:
iptables -A FORWARD -i wg0 -o br-<bridge_id> -j ACCEPT
iptables -A FORWARD -i br-<bridge_id> -o wg0 -j ACCEPT
```

Containers use the Docker gateway IP to reach the host, which routes to the WireGuard interface.

## Security

- WireGuard encrypts all inter-machine traffic (Noise protocol, Curve25519)
- Only WireGuard peers can reach the ACP bus on the WireGuard interface
- Bus auth token required for all connections (`ACP_BUS_SECRET` env var)
- No public internet exposure — the bus listens on private interfaces only
- Each machine's firewall blocks port 9090 on public interfaces
