from pydantic import AnyHttpUrl, PostgresDsn
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # App
    sila_env: str = "development"
    sila_secret_key: str
    sila_cors_origins: str = "http://localhost:3002"

    # AI availability. Off on a deployment without its model servers yet (the
    # "Lite" edition before the GPUs arrive): every chat turn gets this message
    # instead of a model call, and endpoints that need the model (security
    # scans, document embedding, skill review) refuse with it — a clear wait,
    # never a half-run that fails closed somewhere deep.
    sila_ai_enabled: bool = True
    sila_ai_unavailable_message: str = (
        "Sila's AI is waiting for its GPU servers. Once they are installed, chat, "
        "agent building and security reviews will switch on here automatically."
    )
    # Comma-separated email domains allowed to sign up ("" = any). Matters most
    # when accounts are active on creation (no verification email).
    sila_signup_allowed_domains: str = ""

    # Database — connects directly to Supabase Postgres
    database_url: PostgresDsn

    # Supabase / GoTrue
    sila_supabase_url: AnyHttpUrl
    sila_supabase_service_key: str
    supabase_jwt_secret: str

    # OPA
    opa_url: str = "http://opa:8181"

    # Dify agent runtime
    dify_base_url: str = "http://dify-api:5001"
    # Dify ADMIN_API_KEY — server-to-server console access (bypasses CSRF).
    # Empty disables Dify calls (fail-closed 503).
    dify_console_token: str = ""
    # Dify workspace (tenant) id — sent as X-WORKSPACE-ID so the admin key
    # authenticates as that workspace's owner.
    dify_workspace_id: str = ""

    # Agent sandbox quota (invocations per user per day)
    agent_sandbox_daily_quota: int = 100

    # Meilisearch
    meilisearch_url: str = "http://sila-meilisearch:7700"
    meilisearch_master_key: str = ""  # MEILI_MASTER_KEY from env
    # SearXNG — self-hosted meta-search for SILA's web_search tool (sovereign:
    # queries never leave local infra). Internal-only container.
    searxng_url: str = "http://sila-searxng:8080"
    # Sila's built-in Grafana (Stage B): the API talks to the internal address;
    # LINKS handed to users must use the browser-reachable public URL. The admin
    # password enables the one-click connect (backend mints the service-account
    # token itself — the user never touches Grafana).
    grafana_internal_url: str = "http://sila-grafana:3000"
    grafana_public_url: str = "http://localhost:3004"
    grafana_admin_password: str = ""
    # Kill-switch for the reverse-engineered Power BI report generation (the
    # thin-PBIX import). Flip to false if Microsoft changes the Layout format —
    # the tool then falls back to the dataset link + Auto-create instructions.
    powerbi_report_generation: bool = True
    # Gotenberg (headless LibreOffice) — renders slide-template previews.
    gotenberg_url: str = "http://sila-gotenberg:3000"
    # The BROWSER-reachable base of this API — used when a chat link must point
    # back at us (generated-file downloads). Tunnel domain later.
    sila_api_public_url: str = "http://localhost:8001"
    # The browser-reachable web app — used in notification emails (invite links).
    sila_web_public_url: str = "http://localhost:3002"

    # Knowledge Base & RAG (Phase 4)
    clamav_host: str = "sila-clamav"
    clamav_port: int = 3310
    # Scan timeout — right after a stack restart clamd reloads signatures and a
    # first scan can exceed 30s; steady-state a 6MB file scans in <1s.
    clamav_timeout_s: float = 60.0
    ollama_url: str = "http://ollama:11434"
    embedding_model: str = "bge-m3"
    # Phase 6 LLM-driven gatekeeper. Local-only inference — never an external API.
    # Standardised on qwen3:14b (2026-08-16) — one generation model for the whole
    # platform. Measured cost of the move for THIS caller: a scanner-shaped call
    # (≈550 in / ≈250 out, JSON mode) goes 2.55s → 5.74s of GPU time (2.25x). The
    # scanner fires once per agent submission (observed 21 runs in 14 days ≈
    # 1.5/day), so ≈ +4.8 GPU-seconds/day at current volume. It scales with
    # SUBMISSIONS, not invocations — revisit if submission rate approaches
    # invocation rate.
    llm_scanner_model: str = "qwen3:14b"
    # SILA concierge brain — tool-calling capable; upgradeable to 14b within VRAM.
    sila_llm_model: str = "qwen3:14b"
    # ---- Concierge model provider -------------------------------------------
    # "ollama" (default, local, sovereign) | "qwen-cloud" (HOSTED — prompts leave
    # the machine). The cloud option exists to BENCHMARK answer quality against
    # the local 14B; it contradicts the platform's own PDPL stance and must stay
    # a deliberate, temporary switch. Everything it implies is written down in
    # docs/ops/CLOUD_MODEL_EVALUATION.md — read that before enabling it.
    sila_llm_provider: str = "ollama"
    # The GRC OKR template. Omantel-confidential, so never shipped with the code:
    # locally a path under private/, on OpenShift an encrypted volume or internal
    # object storage (docs/ops/OKR_TEMPLATE_DEPLOYMENT.md). Empty = not configured.
    sila_okr_template_path: str = ""
    # Check each KR's written progress against the KR with the LOCAL model and
    # note likely mismatches on the slide. On because qwen3:14b passed the
    # labelled evaluation's gate (docs/okr/PROGRESS_VALIDATION.md); when off,
    # the deck and the job result say progress was not validated this month.
    sila_okr_validate_progress: bool = True
    # The LOCAL Ollama model that validates progress text (the chat's own
    # provider is never used for OKR data). Re-run the evaluation before changing.
    sila_okr_validation_model: str = "qwen3:14b"
    # DashScope's OpenAI-compatible endpoint. Singapore region by default: still
    # not Oman, but the nearer of the two and the one to argue for if this ever
    # goes past evaluation.
    qwen_base_url: str = "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"
    # qwen3.8-flash: function-calling capable, 1M context, and ~13x cheaper than
    # the max tier ($0.15/$0.47 per 1M vs $2/$6). It picked the right tool on the
    # routing case the local 14B failed, so the max tier's extra cost buys
    # nothing measurable here. See docs/ops/CLOUD_MODEL_EVALUATION.md.
    qwen_model: str = "qwen3.8-flash"
    # ---- Paid web-search fallback ------------------------------------------
    # SearXNG is free but depends on public engines that block server-side
    # callers from a datacenter IP; measured here, only two of eleven answer and
    # one of those rate-limits under load. DashScope's built-in web_search does
    # work — and costs $10 per 1,000 calls, which a model that re-searches can
    # burn through fast (one observed turn fired 14 searches).
    #
    # So: free first, paid only when free search comes back BLOCKED, and never
    # more than `web_search_paid_daily_cap` paid calls a day. Off unless the
    # hosted provider is already configured.
    web_search_paid_fallback: bool = True
    web_search_paid_daily_cap: int = 200  # ≈ $2/day ceiling at $10/1k
    # What the UI calls the hosted brain. Users see the product, not the vendor's
    # model id — and the id is meaningless to them anyway since the picker cannot
    # change it while the hosted provider is active.
    cloud_model_display_name: str = "Sila 1.0"
    # Read from the environment; never defaulted, never logged.
    qwen_api_key: str = ""
    # New SILA sessions allowed per user per day (denial-of-wallet guard). Env-
    # overridable; resuming an existing session never counts against it.
    sila_max_sessions_per_day: int = 100
    # Per-user memory cap — when reached the composer blocks sending until the
    # user deletes entries (Mojahid's design).
    sila_max_memories: int = 20
    # Phase 18 — open-source Agent Skills in the Bazaar. The catalog and each
    # SKILL.md are fetched only from these hosts (plus the SSRF IP check); no
    # user data is ever sent. The GitHub token is optional (60 -> 5,000 req/h).
    skills_catalog_url: str = "https://skills-library.com/api/skills.json"
    skills_github_api_url: str = "https://api.github.com"
    skills_raw_url: str = "https://raw.githubusercontent.com"
    skills_allowed_hosts: str = "skills-library.com,api.github.com,raw.githubusercontent.com"
    github_token: str = ""
    skills_catalog_max_bytes: int = 15_000_000
    skill_md_max_bytes: int = 64_000
    skills_fetch_timeout_s: float = 20.0
    skills_imports_per_hour: int = 10
    # One local-model review call per ~12k characters of a skill.
    skills_scan_timeout_s: float = 120.0
    # Skills a chat may carry, and how much of each one's instructions it sends
    # to the model (the local 14B's context is shared with the conversation).
    sila_max_skills_per_session: int = 3
    sila_skill_prompt_chars: int = 8_000
    # Analysis Engine P0 — uploaded dataset storage (full files, DuckDB-read).
    sila_datasets_dir: str = "/app/data/datasets"
    sila_dataset_max_mb: int = 200
    # Context window for the brain. Ollama's DEFAULT is 4096 — far too small for
    # SILA's system prompt + tool schemas + a fetched page; below this the model
    # silently truncates (loses the system prompt, forgets tool results).
    # 8192, with OLLAMA_KV_CACHE_TYPE=q8_0 + OLLAMA_FLASH_ATTENTION=1 (compose).
    #
    # [MEASURED 2026-08-16, container, qwen3:14b Q4_K_M, 12 GB card] The previous
    # 12288 was NOT a measured ceiling — it spilled ~11% to CPU and collapsed
    # decode to 5.4 tok/s, i.e. production ran ~8x slower than every benchmark
    # (all of which were taken at Ollama's 4096 default).
    #
    #   num_ctx  KV cache  R_d tok/s  placement      embedder coexists?
    #     4096   f16          40.4    100% GPU       yes   <- too small: a RAG
    #                                                        prompt is ~2,050 and
    #                                                        grows to ~4,100 after
    #                                                        one tool call
    #     8192   f16          43.1    100% GPU       NO (evicts)
    #     8192   q8_0         43.9    100% GPU       YES  <- chosen
    #    12288   f16           5.4    ~11% on CPU    no
    #    12288   q8_0         15.8    still spills   no
    #    16384   q8_0          5.1    spills         no
    #
    # 8192 + q8_0 is the only configuration that fits the tool-calling prompt at
    # full speed AND keeps bge-m3 resident, which also removes the ~21 s cold
    # load from every RAG embed->generate cycle.
    sila_llm_num_ctx: int = 8192
    # Concierge runtime: "graph" (single-supervisor LangGraph — vetting, credentials,
    # trace rehydrate, durable pause) is the default; "loop" is the legacy flat tool
    # loop; "fanout" adds the planner / parallel-worker Send fan-out + synthesizer
    # AROUND the supervisor (staged — simple requests still behave like "graph").
    sila_orchestrator: str = "graph"
    # Fan-out caps (denial-of-wallet controls; all fail-closed when hit). Only used
    # by the "fanout" runtime; harmless otherwise.
    sila_max_workers: int = 4  # max parallel subtasks the planner may fan out to
    sila_fanout_recursion_limit: int = 48  # backstop for the deepest fan-out branch
    sila_turn_token_budget: int = 60_000  # soft per-turn LLM token budget across workers
    sila_turn_deadline_s: float = 120.0  # soft per-turn wall-clock budget
    # Internal voice container (STT/TTS); empty string disables voice mode.
    voice_url: str = "http://sila-voice:7800"
    # Internal image-gen container (FLUX.1-schnell) for decorative slide art;
    # empty string disables image generation (decks render placeholders). It
    # shares the GPU with the brain, so the adapter unloads the brain first.
    image_gen_url: str = ""
    # Optional. Unlocks the Unsplash provider for deck imagery; without it only
    # Wikimedia Commons is available, whose abstract/technical hit rate is low.
    unsplash_access_key: str = ""
    image_gen_timeout_s: float = 120.0
    # Formal outbound-email signature (SILA send_email tool). All swappable via
    # env so ops can brand it without a code change (Rule 5). A hosted logo image
    # is the only kind email clients render (Gmail/Outlook strip data-URIs); when
    # SILA_EMAIL_LOGO_URL is empty we fall back to a CSS wordmark that renders
    # everywhere. Phone is a placeholder until the team gives the real number.
    sila_email_brand: str = "Sila"
    sila_email_tagline: str = "Sovereign AI Agent Platform"
    sila_email_team: str = "Sila Team"
    sila_email_phone: str = "+968 9418 8120"
    sila_email_contact: str = "agent@sila-ai.org"
    sila_email_logo_url: str = ""  # hosted https logo; empty → CSS wordmark
    llm_scanner_timeout_s: float = 30.0
    embedding_batch_size: int = 32
    document_max_mb: int = 50
    document_chunk_tokens: int = 1024
    document_chunk_overlap: int = 128
    supabase_storage_url: str = "http://supabase-storage:5000"

    # Tools / MCP (Phase 5)
    # Comma-separated hostnames that bypass the SSRF guard. Used for trusted
    # Docker-network sidecars (sila-mcp-*) registered as built-in tools.
    mcp_builtin_hostnames: str = ""

    # Sila API's own address on the Docker network — used to build URLs that
    # other containers (e.g. an n8n workflow node) call back into Sila.
    sila_api_internal_url: str = "http://sila-api:8001"

    # Phase B multi-tenancy — Sila→n8n auth bridge
    n8n_internal_url: str = "http://sila-n8n:5678"
    # Passthrough for direct JWT mint (unused — we use the login endpoint).
    n8n_user_management_jwt_secret: str = ""
    sila_n8n_owner_email: str = "n8n-owner@sila.local"
    sila_n8n_owner_password: str = ""  # required at runtime; bootstrap fails loudly if missing
    sila_n8n_owner_first_name: str = "Sila"
    sila_n8n_owner_last_name: str = "System"
    # Fernet key — required for storing per-user n8n passwords at rest.
    sila_field_encryption_key: str = ""
    # Phase 15 Surface 3 — workflow agents: max seconds a webhook-triggered
    # workflow may run before the invoke is cut off (workflows can be slow).
    workflow_invoke_timeout_s: float = 60.0

    # WS-9 — Connector authorization via self-hosted Nango (Auth + Proxy only).
    # The backend mints Connect sessions and proxies third-party calls; the Nango
    # secret key never leaves the server. Empty secret disables connector calls
    # (fail-closed). nango_server_url is the internal Docker address; the public
    # callback/Connect URL is a frontend concern (NEXT_PUBLIC_*).
    nango_server_url: str = "http://nango-server:3003"
    nango_secret_key: str = ""

    # SMTP — the PLATFORM sends "email me the summary" notifications from its own
    # server (MailHog in dev → self-hosted/Omantel in prod). Sovereign: no per-user
    # OAuth, no cloud. n8n never holds these creds; only the internal send-email
    # endpoint does. (Env: SMTP_HOST/PORT/USER/PASS/SMTP_SENDER.)
    smtp_host: str = "mailhog"
    smtp_port: int = 1025
    smtp_user: str = ""
    smtp_pass: str = ""
    smtp_sender: str = "sila@sila.local"
    smtp_use_tls: bool = False

    # Developer SDK & API (Phase 7)
    # Per-key sliding-window limits. Defaults match the CLAUDE.md plan.
    api_rate_limit_per_minute: int = 60
    api_rate_limit_per_day: int = 10_000
    # Webhook delivery: exponential backoff (1s, 5s, 25s, 2m, 10m), max 5 attempts.
    webhook_max_attempts: int = 5
    webhook_initial_backoff_s: int = 1
    webhook_delivery_timeout_s: float = 10.0
    # Worker tick interval — how often the delivery worker scans for due retries.
    webhook_worker_interval_s: int = 5

    @property
    def cors_origins(self) -> list[str]:
        return [o.strip() for o in self.sila_cors_origins.split(",")]

    @property
    def signup_allowed_domains(self) -> frozenset[str]:
        return frozenset(
            d.strip().lower().lstrip("@")
            for d in self.sila_signup_allowed_domains.split(",")
            if d.strip()
        )

    @property
    def mcp_builtin_hostname_set(self) -> frozenset[str]:
        return frozenset(h.strip() for h in self.mcp_builtin_hostnames.split(",") if h.strip())

    @property
    def database_url_psycopg(self) -> str:
        """psycopg v3 DSN for LangGraph's AsyncPostgresSaver.

        SQLAlchemy uses postgresql+asyncpg://; the checkpointer needs a bare
        postgresql:// DSN on the same database.
        """
        return str(self.database_url).replace("postgresql+asyncpg://", "postgresql://")


settings = Settings()  # type: ignore[call-arg]
