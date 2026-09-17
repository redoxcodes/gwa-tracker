const ADMIN_CHAT_ID = 1465049104;
const ADMIN_HANDLE = "https://t.me/amredox";
const SUBSCRIPTION_DAYS = 31;
const SUBSCRIBE_MESSAGE =
  "Subscribe to gain access to all notifications from Metawin giveaway host. " +
  "DM me here to get a token: " + ADMIN_HANDLE;

function generateToken() {
  const chars = "abcdefghijklmnopqrstuvwxyz0123456789";
  let token = "mw-";
  for (let i = 0; i < 10; i++) {
    token += chars[Math.floor(Math.random() * chars.length)];
  }
  return token;
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);

    // Secrets Store bindings require .get() to retrieve the actual value
    const webhookSecret = await env.WEBHOOK_SECRET.get();
    const telegramToken = await env.TELEGRAM_BOT_TOKEN.get();

    // Internal endpoint for GitHub Actions to read/write subscribers,
    // protected by the same secret (checked via a header instead of
    // Telegram's secret_token header).
    if (url.pathname === "/subscribers") {
      const apiSecret = request.headers.get("X-Api-Secret");
      if (apiSecret !== webhookSecret) {
        return new Response("Unauthorized", { status: 401 });
      }
      if (request.method === "GET") {
        const subs = (await env.GWA_STATE.get("subscribers")) || "{}";
        return new Response(subs, { headers: { "Content-Type": "application/json" } });
      }
      if (request.method === "POST") {
        const body = await request.text();
        await env.GWA_STATE.put("subscribers", body);
        return new Response("OK");
      }
      return new Response("Method not allowed", { status: 405 });
    }

    if (request.method !== "POST") {
      return new Response("OK");
    }

    // Only accept requests carrying the secret Telegram sends us
    const secretHeader = request.headers.get("X-Telegram-Bot-Api-Secret-Token");
    if (secretHeader !== webhookSecret) {
      return new Response("Unauthorized", { status: 401 });
    }

    const update = await request.json();
    const message = update.message;
    if (!message || !message.text) {
      return new Response("OK");
    }

    const chatId = message.chat.id;
    const text = message.text.trim();

    async function send(toChatId, msg) {
      await fetch(`https://api.telegram.org/bot${telegramToken}/sendMessage`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ chat_id: toChatId, text: msg }),
      });
    }

    const subscribers = JSON.parse((await env.GWA_STATE.get("subscribers")) || "{}");
    const tokens = JSON.parse((await env.GWA_STATE.get("tokens")) || "{}");
    let changed = false;

    function grantSubscription(id) {
      const expiresAt = new Date(Date.now() + SUBSCRIPTION_DAYS * 86400000).toISOString();
      subscribers[String(id)] = { expires_at: expiresAt };
    }

    async function handleTokenOrFallback() {
      if (tokens[text] && !tokens[text].used) {
        tokens[text].used = true;
        tokens[text].used_by = chatId;
        grantSubscription(chatId);
        changed = true;
        await send(chatId, `You're subscribed! Access lasts ${SUBSCRIPTION_DAYS} days.`);
      } else {
        await send(chatId, SUBSCRIBE_MESSAGE);
      }
    }

    if (chatId === ADMIN_CHAT_ID) {
      if (text === "/admin") {
        await send(
          chatId,
          "Admin menu:\n\n" +
            "/generate — create a new one-time token\n" +
            "/addsub <chat_id> — grant access without a token\n" +
            "/removesub <chat_id> — remove a subscriber's access\n" +
            "/broadcast <message> — message every subscriber"
        );
      } else if (text === "/generate") {
        const token = generateToken();
        tokens[token] = { used: false, used_by: null };
        changed = true;
        await send(chatId, `New token: ${token}`);
      } else if (text.startsWith("/addsub")) {
        const parts = text.split(" ");
        if (parts.length === 2) {
          grantSubscription(parts[1]);
          changed = true;
          await send(chatId, `Added ${parts[1]} for ${SUBSCRIPTION_DAYS} days.`);
        } else {
          await send(chatId, "Usage: /addsub <chat_id>");
        }
      } else if (text.startsWith("/removesub")) {
        const parts = text.split(" ");
        if (parts.length === 2 && subscribers[parts[1]]) {
          delete subscribers[parts[1]];
          changed = true;
          await send(chatId, `Removed ${parts[1]}.`);
        } else {
          await send(chatId, "Usage: /removesub <chat_id> (must be an existing subscriber)");
        }
      } else if (text.startsWith("/broadcast")) {
        const announcement = text.slice("/broadcast".length).trim();
        if (announcement) {
          const ids = Object.keys(subscribers);
          for (const id of ids) {
            await send(id, announcement);
          }
          await send(chatId, `Broadcast sent to ${ids.length} subscribers.`);
        } else {
          await send(chatId, "Usage: /broadcast <your message>");
        }
      } else {
        await handleTokenOrFallback();
      }
    } else {
      await handleTokenOrFallback();
    }

    if (changed) {
      await env.GWA_STATE.put("subscribers", JSON.stringify(subscribers));
      await env.GWA_STATE.put("tokens", JSON.stringify(tokens));
    }

    return new Response("OK");
  },
};
