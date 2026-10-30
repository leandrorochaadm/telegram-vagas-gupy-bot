/// <reference path="../pb_data/types.d.ts" />

// Full text of each job sent to Telegram. Bot-only data with no per-user owner (same as the other
// bot collections). `title` and `description` use the `editor` type for long text (no explicit `max`).
const BOT_ONLY = '@request.auth.id != "" && @request.auth.collectionName = "bot_accounts"'

const botRules = () => ({
  listRule: BOT_ONLY,
  viewRule: BOT_ONLY,
  createRule: BOT_ONLY,
  updateRule: BOT_ONLY,
  deleteRule: BOT_ONLY,
})

const uniqueIndex = (name, table, columns) =>
  "CREATE UNIQUE INDEX `" + name + "` ON `" + table + "` (" + columns.map((c) => "`" + c + "`").join(", ") + ")"

const createdField = { type: "autodate", name: "created", onCreate: true, onUpdate: false }

migrate(
  (app) => {
    app.save(
      new Collection({
        type: "base",
        ...botRules(),
        name: "job_descriptions",
        fields: [
          { type: "text", name: "link", required: true },
          { type: "text", name: "source", required: true },
          { type: "editor", name: "title" },
          { type: "text", name: "company" },
          { type: "text", name: "published" },
          { type: "editor", name: "description" },
          { type: "text", name: "seniority" },
          { type: "text", name: "employment_type" },
          { type: "text", name: "status", required: true },
          // Not `required`: PocketBase rejects 0 in a required number field.
          { type: "number", name: "attempts", onlyInt: true },
          { type: "date", name: "first_try_at" },
          { type: "date", name: "last_try_at" },
          { type: "date", name: "finished_at" },
          createdField,
        ],
        indexes: [uniqueIndex("uq_job_descriptions_link_upsert", "job_descriptions", ["link"])],
      }),
    )
  },
  (app) => {
    app.delete(app.findCollectionByNameOrId("job_descriptions"))
  },
)
