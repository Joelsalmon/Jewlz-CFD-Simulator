# Jewlz CFD Backend

This backend lets your public Streamlit Cloud app submit OpenFOAM CFD cases to your PC through a secure API key and Cloudflare Tunnel.

## 1. Start your OpenFOAM Docker container

```powershell
docker start -ai jewlz-openfoam
```

Leave this terminal open.

## 2. Start the backend API

Open a second PowerShell in the backend folder:

```powershell
python -m pip install -r requirements_backend.txt
$env:JEWLZ_CFD_API_KEY="make-a-long-private-key-here"
$env:OPENFOAM_CONTAINER="jewlz-openfoam"
python -m uvicorn backend_api:app --host 0.0.0.0 --port 8000
```

## 3. Test locally

```powershell
curl -H "x-api-key: make-a-long-private-key-here" http://localhost:8000/health
```

## 4. Expose safely with Cloudflare Tunnel

```powershell
cloudflared tunnel --url http://localhost:8000
```

Copy the generated `https://...trycloudflare.com` URL.

## 5. Add Streamlit Cloud secrets

In Streamlit Cloud > Manage app > Settings > Secrets:

```toml
CFD_BACKEND_URL = "https://your-tunnel-url.trycloudflare.com"
CFD_BACKEND_API_KEY = "make-a-long-private-key-here"
```

## Security notes

- Do not use router port forwarding.
- Do not expose Docker socket.
- Expose only this FastAPI service through Cloudflare Tunnel.
- Keep the API key private.
- Shut down the tunnel when you do not want customers submitting CFD jobs.
