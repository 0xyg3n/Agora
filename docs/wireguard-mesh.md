# Multi-Machine ACP Mesh via WireGuard

> Status: **Architecture Ready — Implementation Straightforward**
> The WireGuard mesh and iptables routing already work between seaverse and Giannis PC.

## Concept

The ACP Event Bus currently runs on a single host (`ws://0.0.0.0:9090`). With WireGuard, multiple machines join a private mesh network and any machine can connect agents to the bus. This turns a single-host voice room into a distributed multi-machine agent network.

## Topology

```
Machine A (Seaverse VPS, 10.0.0.1)
├── ACP Event Bus (ws://10.0.0.1:9090)
├── LiveKit Server
├── Agora Frontend (:3210)
├── skynet-laira (Hermes gateway)
└── skynet-loki (OpenClaw gateway)

Machine B (Giannis PC, 10.0.0.3)
├── GPU workloads (VibeVoice TTS, local STT)
├── Additional agents
└── Connects to bus at ws://10.0.0.1:9090

Machine C (Cloud GPU, 10.0.0.4)
├── Heavy inference models (local LLMs)
└── Connects to bus at ws://10.0.0.1:9090
```

## What This Enables

- **GPU offload**: TTS, STT, and local LLM inference on machines with GPUs while seaverse runs the bus and room
- **Distributed agents**: Agent containers on different machines sharing the same ACP bus for cross-session awareness
- **Scale horizontally**: Add machines to the WireGuard mesh, not by upgrading one server
- **All traffic encrypted**: WireGuard encrypts everything between machines

## Implementation

### Already Working
- WireGuard mesh between seaverse (10.0.0.1) and Giannis PC (10.0.0.3)
- iptables FORWARD rules for Docker containers to access WireGuard network
- ACP bus binds to `0.0.0.0` (reachable on all interfaces including WireGuard)

### To Connect a New Machine

1. Add WireGuard peer on seaverse and the new machine
2. Set `ACP_BUS_URL=ws://10.0.0.1:9090` in the agent's environment
3. For Docker containers: ensure the host routes WireGuard traffic (`iptables -A FORWARD -i wg0 -j ACCEPT`)
4. Agents on the new machine connect to the bus and subscribe/publish like local ones

### Container Access

Docker containers access the WireGuard network via host routing:
```bash
# On the host running containers:
iptables -A FORWARD -i wg0 -o br-<bridge_id> -j ACCEPT
iptables -A FORWARD -i br-<bridge_id> -o wg0 -j ACCEPT
```

Containers use the Docker gateway IP (172.20.0.1) to reach the host, which routes to 10.0.0.1 via WireGuard.

## Security

- WireGuard encrypts all inter-machine traffic (Noise protocol, Curve25519)
- Only WireGuard peers can reach the ACP bus on the WireGuard interface
- Bus auth token required for all connections (`ACP_BUS_SECRET` env var)
- No public internet exposure — the bus listens on private interfaces only
- Each machine's firewall blocks port 9090 on public interfaces
