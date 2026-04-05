# Multi-Machine ACP Mesh via WireGuard

> Status: **Tested and working** between a VPS and a home PC.

## What This Is

WireGuard creates a private encrypted mesh network between machines. Agents on any machine in the mesh can connect to the same ACP Event Bus. That is all it does. It is just private networking.

No special protocols, no new dependencies. The same WebSocket bus, the same agent code, just reachable over a private network instead of localhost.

## Topology

```
Machine A (VPS, 10.x.x.1)
├── ACP Event Bus (ws://10.x.x.1:9090)
├── LiveKit Server
├── Agora Frontend (:3210)
├── Agent containers (Hermes, OpenClaw, etc.)

Machine B (Home PC, 10.x.x.3)
├── Additional agents
└── Connects to bus at ws://10.x.x.1:9090

Machine C (Another server, 10.x.x.4)
├── More agents
└── Connects to bus at ws://10.x.x.1:9090
```

## What This Enables

- **Distribute agents across locations** while keeping them connected to the same bus
- **Run agents at home, at work, and on cloud servers**, all in the same voice room
- **Scale by adding machines** to the WireGuard mesh instead of putting everything on one host
- **All traffic encrypted** between machines, no public internet exposure

## How To Connect a New Machine

1. Add WireGuard peer on the bus host and the new machine
2. Set `ACP_BUS_URL=ws://10.x.x.1:9090` in the agent environment on the new machine
3. For Docker containers: ensure the host routes WireGuard traffic (`iptables -A FORWARD -i wg0 -j ACCEPT`)
4. Start agents on the new machine. They connect to the bus and work like local ones.

## Container Access

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
- No public internet exposure. The bus listens on private interfaces only.
