# Sila Lite — backend

Sila is a sovereign AI-agent marketplace: teams discover, build, vet and hire AI
agents, and everything runs on your own infrastructure. **Sila Lite** runs
**before the GPU servers arrive**. The whole platform works except the AI:

- **Chat** answers every message with: *"Sila's AI is waiting for its GPU
  servers. Once they are installed, chat, agent building and security reviews
  will switch on here automatically."*
- **Security reviews, skill imports and knowledge uploads** answer with the same
  message instead of starting.

No cloud AI provider is used, so no data leaves your infrastructure.

This repository is the **backend** (Python). The website is in
**[sila-lite-frontend](https://github.com/Mojahid-Souti/sila-lite-frontend)**.
Both deploy on one hostname: `/` goes to the website and `/v1` to this API.

| Part | What it is |
|---|---|
| `apps/api` | The API: FastAPI on Python 3.13, with its tests |
| `infra/opa` | Permission rules (Open Policy Agent) |
| `infra/policies` | Classification and data-protection policies the API reads |
| `infra/supabase` | Database start-up scripts and the internal gateway (Kong) config |
| `infra/openshift` + `kustomization.yaml` | The OpenShift deployment |
| `docker-compose.yml` | The same services on one machine |
| `.github/workflows/ci.yml` | Tests, then builds and publishes the images |

Five services run: **sila-api, opa, Postgres (Supabase), GoTrue (sign-in) and
Kong** (the internal gateway between the API and GoTrue). Only the API has a
Route.

## Images

GitHub Actions builds the images on every push and publishes them to GitHub's
container registry. OpenShift pulls them; nothing is compiled on the cluster.

| Image | Built from |
|---|---|
| `ghcr.io/mojahid-souti/sila-lite-api` | `apps/api/Dockerfile` |
| `ghcr.io/mojahid-souti/sila-lite-opa` | `infra/opa/Dockerfile` |

Tags: `latest` and `sha-<commit>` on every push to `main`; `X.Y.Z` when a
`vX.Y.Z` git tag is pushed. The OpenShift deployment pins its version in
`kustomization.yaml` (`images:`).

## Run it locally

Needs Docker with Compose, and Python 3 for the secret generator.

```bash
python scripts/gen_secrets.py > .env
docker compose pull && docker compose up -d     # published images
# or: docker compose up -d --build               # build from this repository
curl http://localhost:8001/healthz
```

Then run the website from sila-lite-frontend against `http://localhost:8001`.
Our images run under a random non-root user ID, the way OpenShift runs them.

---

## Deploy to OpenShift

### 1. What the cluster administrator provides

- A project (namespace), for example `sila-dev`, with the **admin** role for you.
- Quota of about **3 vCPU, 6 GiB memory and 20 GiB storage**. Add roughly
  1 vCPU and 1 GiB for the frontend.
- The `anyuid` SCC for the database's service account. The Supabase Postgres
  image starts as root and drops to the `postgres` user itself:

  ```bash
  oc adm policy add-scc-to-user anyuid -z sila-db -n sila-dev
  ```

- DNS for the public hostname pointing at the cluster's router, and a TLS
  certificate for it. Add the certificate to the Routes, or use the router's
  default if it already covers the hostname.
- Pulls from `ghcr.io` (the images are public), plus `docker.io` for the
  Postgres, GoTrue and Kong images.

### 2. Log in, choose the project, create the secrets

```bash
oc login --token=<token> --server=https://api.<cluster>:6443
oc project sila-dev

python scripts/gen_secrets.py --openshift > sila-secrets.env
oc create secret generic sila-secrets --from-env-file=sila-secrets.env
rm sila-secrets.env          # it is now in the cluster; don't keep a copy around
```

Every installation gets fresh keys. Never reuse another installation's.

### 3. Set the hostname, then apply

Edit `infra/openshift/config.yaml`: `PUBLIC_HOST`, `PUBLIC_URL` and the sign-up
domains. It is the only file to edit, and the hostname must match the
frontend's. Then:

```bash
oc apply -k .            # needs oc 4.10 or newer
oc logs -f job/sila-migrate          # creates the database tables, then ends
```

After an upgrade (a new image version in `kustomization.yaml`), run the
migration again:

```bash
oc delete job sila-migrate && oc apply -k .
```

### 4. Check

```bash
oc get pods              # all Running (ready = health checks pass); sila-migrate Completed
curl -s -o /dev/null -w "%{http_code}\n" https://<PUBLIC_HOST>/v1/workspaces   # 401 = API reached
```

Then deploy the frontend and sign up with an address in the allowed domain. A
chat message should get the "waiting for GPU servers" reply.

---

## When the GPUs arrive

1. Deploy the model server (Ollama) with GPU access and pull the models.
2. Set `SILA_AI_ENABLED: "true"` in `infra/openshift/config.yaml`.
3. Run `oc apply -k .` and `oc rollout restart deployment/sila-api`.

## Security notes

- **Secrets** live only in the `sila-secrets` Secret. `.env` and `*.env` are
  git-ignored.
- **Exposure:** only `/v1` (the API) is routed. Postgres, GoTrue, Kong and OPA
  have no Route.
- **Accounts:** accounts are active on creation, because Lite sends no
  verification email. Sign-up is limited to the domains in
  `SILA_SIGNUP_ALLOWED_DOMAINS` and checked by the API before any account is
  created. GoTrue cannot be reached directly.
- **Passwords:** "Forgot password" needs email, so in Lite an administrator
  resets passwords.
- **Containers** run as non-root with a random UID; only the database needs
  `anyuid`.

## Licence

MIT. See `LICENSE`.
