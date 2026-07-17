# Deploying p4-mcp-server on GCP

## Infrastructure

| Resource | Value |
|---|---|
| VM | `p4-mcp-server` |
| Project | `scio-salessavvy-host` |
| Zone | `us-central1-a` |
| Machine | `e2-small` (2 shared vCPU, 2GB RAM) |
| OS | Debian 12, 10GB disk |
| Subnet | `shared-vpc-subnet9` (10.9.0.0/24) |
| Internal IP | `10.9.0.35` |
| External IP | `136.114.228.90` |
| Firewall | `allow-p4-mcp-server` — TCP 8000 from 0.0.0.0/0 |

## MCP Endpoint

```
http://136.114.228.90:8000/mcp/
```

## Container Configuration

The server runs as a Docker container with these settings:

- **Source repo:** `github.com/askscio/p4mcp-server` branch `king-11/search-tools`
- **P4PORT:** `ssl:34.31.24.93:1666` (perforce-helix-core VM public IP)
- **P4USER:** `steve.smith`
- **P4PASSWD:** a pre-created admin ticket for `steve.smith` (never a plaintext password)
- **MCP_IMPERSONATION_ENABLED:** `true`
- **Flags:** `--readonly --transport http --port 8000`
- **Restart policy:** `unless-stopped`
- **Volume:** `/opt/p4data:/root` — persists `.p4trust` and `.p4tickets` across container rebuilds

## SSH into the VM

```bash
gcloud compute ssh p4-mcp-server \
  --project=scio-salessavvy-host \
  --zone=us-central1-a \
  --tunnel-through-iap
```

## Rebuild and Redeploy

After SSH-ing into the VM, run the following to pull latest changes and rebuild:

```bash
# Pull latest changes
cd /opt/p4mcp-server
sudo git pull origin king-11/search-tools

# Rebuild the Docker image
sudo docker build -t p4-mcp-server:latest .

# Recreate the container
sudo docker stop p4-mcp-server
sudo docker rm p4-mcp-server
sudo docker run -d \
  --name p4-mcp-server \
  --restart=unless-stopped \
  -p 8000:8000 \
  -v /opt/p4data:/root \
  -e P4PORT=ssl:34.31.24.93:1666 \
  -e P4USER=$P4USER \
  -e P4PASSWD="$P4_ADMIN_TICKET" \
  -e P4TICKETS=/root/.p4tickets \
  -e MCP_IMPERSONATION_ENABLED=true \
  -e LOG_LEVEL=INFO \
  p4-mcp-server:latest \
  python3 -m src.main --readonly --transport http --port 8000
```

## P4 Trust and Login

Trust and ticket files are persisted at `/opt/p4data/` on the host via the volume mount.
They survive container rebuilds. Before the MCP server handles requests, obtain
an admin ticket for `P4USER` through the approved Perforce administration flow
and provide it as `P4_ADMIN_TICKET` to the container. `P4PASSWD` is treated as
that opaque ticket, not as a password. The shared ticket file must be writable
because the server stores target-user tickets there.

If you need to re-establish trust (e.g. after a P4 server cert change):

```bash
sudo docker exec p4-mcp-server p4 -p ssl:34.31.24.93:1666 trust -y
```

The MCP server does not perform an admin `p4 login`; an expired or missing
admin ticket fails the first impersonated request with the Perforce error.

## Common Operations

```bash
# View logs
sudo docker logs -f p4-mcp-server

# Restart without rebuilding
sudo docker restart p4-mcp-server

# Stop
sudo docker stop p4-mcp-server

# Check status
sudo docker ps -a --filter name=p4-mcp-server
```

## Cleanup

To remove old Docker images after a rebuild:

```bash
sudo docker image prune -f
```
