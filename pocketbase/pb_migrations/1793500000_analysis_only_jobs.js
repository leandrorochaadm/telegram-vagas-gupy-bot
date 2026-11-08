/// <reference path="../pb_data/types.d.ts" />

// Jobs the bot found but did not send to Telegram, kept only for the weekly analysis (back-end jobs).
// Like `sent_jobs`, it tells the bot the job was already handled, so it is not processed again.
// Bot-only data with no per-user owner (same as the other bot collections).
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
        name: "analysis_only_jobs",
        fields: [
          { type: "text", name: "link", required: true },
          { type: "text", name: "published" },
          { type: "editor", name: "title" },
          createdField,
        ],
        indexes: [uniqueIndex("uq_analysis_only_jobs_link", "analysis_only_jobs", ["link"])],
      }),
    )
  },
  (app) => {
    app.delete(app.findCollectionByNameOrId("analysis_only_jobs"))
  },
)
