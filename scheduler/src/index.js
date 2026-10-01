// Triggers the Vagas Bot GitHub Actions workflow on a precise schedule.
// GitHub's own `schedule` event is delayed or dropped under load, so a
// Cloudflare Cron Trigger (see wrangler.jsonc) calls the workflow_dispatch API instead.

const REPO = "leandrorochaadm/telegram-vagas-gupy-bot";
const WORKFLOW = "vagas.yml";
const MAX_ATTEMPTS = 3;

async function dispatchWorkflow(env) {
  const url = `https://api.github.com/repos/${REPO}/actions/workflows/${WORKFLOW}/dispatches`;

  const response = await fetch(url, {
    method: "POST",
    headers: {
      Accept: "application/vnd.github+json",
      Authorization: `Bearer ${env.GITHUB_TOKEN}`,
      "X-GitHub-Api-Version": "2022-11-28",
      "User-Agent": "vagas-bot-scheduler",
    },
    body: JSON.stringify({ ref: "main" }),
  });

  if (!response.ok) {
    const body = await response.text();
    const error = new Error(`GitHub API ${response.status}: ${body}`);
    // 4xx means bad token/config: retrying will not help
    error.retryable = response.status >= 500 || response.status === 429;
    throw error;
  }
}

export default {
  async scheduled(controller, env) {
    console.log(`Cron fired: ${controller.cron}`);
    for (let attempt = 1; ; attempt++) {
      try {
        await dispatchWorkflow(env);
        console.log(`Workflow dispatched (attempt ${attempt})`);
        return;
      } catch (error) {
        if (error.retryable === false || attempt >= MAX_ATTEMPTS) throw error;
        console.warn(`Attempt ${attempt} failed, retrying: ${error.message}`);
        await new Promise((resolve) => setTimeout(resolve, attempt * 5000));
      }
    }
  },
};
