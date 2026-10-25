/// <reference path="../pb_data/types.d.ts" />

// Replaces the old SQLite tables. The data is the bot's single shared state (no per-user owner),
// so every data collection is reachable only by the `bot_accounts` login. Nothing is open to
// anonymous users, and `users` is locked.
const BOT_ONLY = '@request.auth.id != "" && @request.auth.collectionName = "bot_accounts"'
const SELF_ONLY = "id = @request.auth.id"
const DATA_COLLECTIONS = [
  "sent_jobs",
  "queued_jobs",
  "alerts",
  "alert_sends",
  "brave_searches",
  "linkedin_sessions",
  "inhire_tenants",
  "inhire_discoveries",
]

const botRules = () => ({
  listRule: BOT_ONLY,
  viewRule: BOT_ONLY,
  createRule: BOT_ONLY,
  updateRule: BOT_ONLY,
  deleteRule: BOT_ONLY,
})

const uniqueIndex = (name, table, columns) =>
  "CREATE UNIQUE INDEX `" + name + "` ON `" + table + "` (" + columns.map((c) => "`" + c + "`").join(", ") + ")"

// `title`, `message` and `text` (long) use the `editor` type: a `text` field caps at 5000 characters
// and the audit rejects an explicit `max`. The bot stores plain text; PocketBase does not sanitize it.
const createdField = { type: "autodate", name: "created", onCreate: true, onUpdate: false }

migrate(
  (app) => {
    app.save(
      new Collection({
        type: "auth",
        name: "bot_accounts",
        listRule: SELF_ONLY,
        viewRule: SELF_ONLY,
        createRule: null,
        updateRule: SELF_ONLY,
        deleteRule: SELF_ONLY,
        authRule: "",
        manageRule: null,
        passwordAuth: { enabled: true, identityFields: ["email"] },
        oauth2: { enabled: false },
        otp: { enabled: false },
        mfa: { enabled: false },
      }),
    )

    const users = app.findCollectionByNameOrId("users")
    users.listRule = null
    users.viewRule = null
    users.createRule = null
    users.updateRule = null
    users.deleteRule = null
    app.save(users)

    const definitions = [
      {
        name: "sent_jobs",
        fields: [
          { type: "text", name: "link", required: true },
          { type: "text", name: "published" },
          { type: "editor", name: "title" },
        ],
        indexes: [uniqueIndex("uq_sent_jobs_link_upsert", "sent_jobs", ["link"])],
      },
      {
        name: "queued_jobs",
        fields: [
          { type: "text", name: "link", required: true },
          { type: "text", name: "title" },
          { type: "text", name: "company" },
          { type: "text", name: "published" },
          { type: "editor", name: "message" },
          { type: "text", name: "source", required: true },
          createdField,
        ],
        indexes: [uniqueIndex("uq_queued_jobs_link_source_upsert", "queued_jobs", ["link", "source"])],
      },
      {
        name: "alerts",
        fields: [
          { type: "text", name: "label", required: true },
          { type: "editor", name: "text", required: true },
          createdField,
        ],
        indexes: [],
      },
      {
        name: "alert_sends",
        fields: [{ type: "date", name: "sent_at", required: true }],
        indexes: [],
      },
      {
        name: "brave_searches",
        fields: [
          { type: "text", name: "label", required: true },
          { type: "date", name: "searched_at", required: true },
        ],
        indexes: [uniqueIndex("uq_brave_searches_label_upsert", "brave_searches", ["label"])],
      },
      {
        name: "linkedin_sessions",
        fields: [
          { type: "text", name: "cookie_hash", required: true },
          { type: "date", name: "searched_at" },
          { type: "date", name: "expired_at" },
          { type: "date", name: "reminded_at" },
        ],
        indexes: [uniqueIndex("uq_linkedin_sessions_cookie_hash_upsert", "linkedin_sessions", ["cookie_hash"])],
      },
      {
        name: "inhire_tenants",
        fields: [
          { type: "text", name: "slug", required: true },
          { type: "date", name: "found_at" },
        ],
        indexes: [uniqueIndex("uq_inhire_tenants_slug_upsert", "inhire_tenants", ["slug"])],
      },
      {
        name: "inhire_discoveries",
        fields: [
          { type: "date", name: "last_run", required: true },
          { type: "number", name: "found", onlyInt: true },
        ],
        indexes: [],
      },
    ]

    for (const definition of definitions) {
      app.save(new Collection({ type: "base", ...botRules(), ...definition }))
    }
  },
  (app) => {
    for (const name of DATA_COLLECTIONS) {
      app.delete(app.findCollectionByNameOrId(name))
    }
    app.delete(app.findCollectionByNameOrId("bot_accounts"))

    const users = app.findCollectionByNameOrId("users")
    users.listRule = "id = @request.auth.id"
    users.viewRule = "id = @request.auth.id"
    users.updateRule = "id = @request.auth.id"
    users.deleteRule = "id = @request.auth.id"
    users.createRule = ""
    app.save(users)
  },
)
