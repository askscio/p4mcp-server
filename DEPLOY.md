# Deploy P4 MCP Server with impersonation

These steps rebuild the Docker image from `king-11/search-tools` and run the server in read-only mode over Streamable HTTP on port 8000. On the Bungie AWS host, use the existing private Glean proxy and load the Perforce credentials from AWS Secrets Manager.

## Connect to the GCP test VM

```bash
gcloud compute ssh p4-mcp-server \
  --project=scio-salessavvy-host \
  --zone=us-central1-a \
  --tunnel-through-iap
```

## Configure Perforce access

Provision `/etc/p4mcp-server.env` on the Docker host as a root-owned file with mode `0600`. Set `P4PORT` to the deployment's Perforce endpoint and `P4USER` to its Perforce superuser account. Set `P4PASSWD` to a valid, pre-issued login ticket for that account. The `P4PORT` and `P4USER` below are the values from the GCP test setup.

```dotenv
P4PORT=ssl:34.31.24.93:1666
P4USER=steve.smith
P4PASSWD=<pre-issued superuser ticket>
```

The server uses the supplied ticket to run `p4 login <as_user>` and saves the resulting user ticket under `/opt/p4data` on the host. Prepare the directory and files for the container's `mcpuser` (UID and GID 1000). The following commands preserve existing ticket and trust entries.

```bash
sudo install -d -m 0700 -o 1000 -g 1000 /opt/p4data &&
  sudo touch /opt/p4data/.p4tickets /opt/p4data/.p4trust &&
  sudo chown 1000:1000 /opt/p4data/.p4tickets /opt/p4data/.p4trust &&
  sudo chmod 0600 /opt/p4data/.p4tickets /opt/p4data/.p4trust
```

For an `ssl:` endpoint, populate the trust file using the `p4` CLI on the host. Set `P4PORT` in your shell to the same endpoint as in the env file. Set `P4_FINGERPRINT` to the fingerprint verified with your Perforce administrator.

```bash
sudo -u '#1000' env P4TRUST=/opt/p4data/.p4trust \
  p4 -p "$P4PORT" trust -i "$P4_FINGERPRINT"
```

## Rebuild and redeploy

Run this from the existing source checkout on the Docker host.

```bash
cd /opt/p4mcp-server &&
  sudo git switch king-11/search-tools &&
  sudo git pull --ff-only origin king-11/search-tools &&
  sudo docker build -t p4-mcp-server:latest .
```

The host port defaults to loopback. If the trusted Glean proxy connects over the network, set `MCP_BIND_IP` to the Docker host's private IP and restrict access to the proxy. The MCP listener has no built-in HTTP authentication. The server binds to `0.0.0.0` inside the container so Docker can forward traffic to it.

```bash
(
  set -e
  if sudo docker container inspect p4-mcp-server >/dev/null 2>&1; then
    sudo docker stop p4-mcp-server
    sudo docker rm p4-mcp-server
  fi

  sudo docker run -d \
    --name p4-mcp-server \
    --restart=unless-stopped \
    --user 1000:1000 \
    --publish "${MCP_BIND_IP:-127.0.0.1}:8000:8000" \
    --volume /opt/p4data:/p4data \
    --env-file /etc/p4mcp-server.env \
    --env P4TICKETS=/p4data/.p4tickets \
    --env P4TRUST=/p4data/.p4trust \
    --env MCP_IMPERSONATION_ENABLED=true \
    --env LOG_LEVEL=INFO \
    p4-mcp-server:latest \
    python3 -m p4mcp.main --readonly --transport http --host 0.0.0.0 --port 8000
)
```

`--user 1000:1000` uses the writable ticket file directly, so issued tickets persist in `/opt/p4data` across container replacements.

## Verify and operate

Connect the MCP client to `/mcp` on port 8000 over Streamable HTTP. Test a read against a known depot file through Glean with `as_user` set to the intended caller. P4-backed tool calls require `as_user`. If Glean sends `Glean-User-Email`, the identity must match the header, as described in [Superuser impersonation](README.md#superuser-impersonation).

```bash
sudo docker ps -a --filter name=p4-mcp-server
sudo docker logs -f p4-mcp-server
```

Use `sudo docker restart p4-mcp-server` to restart the existing container. After updating the superuser ticket in the env file, rerun the container replacement block to load the new ticket.
