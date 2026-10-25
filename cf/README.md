# PYNE Pro API on Cloudflare Containers

Production Flask/gunicorn API (`Dockerfile` target `api`) behind a Worker.

```bash
cd cf
npm install
npm run deploy
# optional:
# npx wrangler secret put ADMIN_TOKEN
```

Worker: `https://pyne-api-container.<account>.workers.dev`  
Health: `/` and `/health` (proxied into the container). The Container class
probes `http://localhost/health` on port 8080.  
Worker-only ready check: `/__cf/ready`.

`standard-2` (1 vCPU / 6 GiB). Named instance `api` keeps the IR cache warm.
Set `?instance=` to address another instance (max 2).

`ADMIN_TOKEN` is a Worker secret. The Durable Object copies it into the
container environment when the instance starts. An empty or missing token
leaves Flask admin routes closed. Gunicorn matches the production entrypoint:
`gthread`, 1 worker, 100 threads, 120s timeout.

Optional **hosted runner** (script registry + bar-close, same as Flask
`PYNE_RUNNER`): keep `PYNE_RUNNER=0` in `src/index.ts` unless you want
`POST /scripts` + `POST /cron/run` inside the container. Enable with
`PYNE_RUNNER=1` and `PYNE_RUNNER_SCHEDULER=1` (one gunicorn worker is
already the container default).
