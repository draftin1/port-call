/**
 * CHAT — post and read Google Meet chat messages.
 *
 * `chat_send` / `chat_read` have been declared in acts.v1 all along with no implementation; this
 * supplies both. Meet does not put chat messages in the DOM until the panel has been opened, so
 * every operation opens it first (idempotent — reopening a panel that is already open is a no-op
 * because we look for the message input before clicking).
 *
 * Read is a poll of the panel rather than the bundle's MutationObserver reader
 * (`createGmeetChat`): an act is request/response, and polling needs no page↔node bridge. The
 * watcher (createChatWatcher) is the same poll on a timer, for the one thing the room can say TO
 * the bot: an opt-out (optout.ts). It keeps the panel open, since Meet puts messages in the DOM
 * only while it is.
 *
 * Results go to stdout as one `[chat] {...}` line, landing in /tmp/vexa-workloads/mtg-<id>-*.log,
 * so the e2e harness reads them with grep — same convention as `[selfcheck]`.
 *
 * L+: Anti-repetition guard wired into send() — duplicates within window are suppressed loudly.
 */
import type { Page } from '@vexa/remote-browser';
import { createRepetitionGuard } from './repetition-guard.js';
import { dismissModals, sweep } from './modals.js';
import { optOutLevel } from './optout.js';

// MODULE scope, not per-controller: index.ts builds a NEW ChatController for EVERY chat_send act
// (surface() re-imports this module, cache-busted only by mtime). A guard owned by the controller
// therefore starts empty on every send and suppresses nothing — it was a no-op until this moved
// out here. ESM caches by URL, so an unedited file keeps this history across acts, which is
// exactly the lifetime the guard needs; a hotswap deliberately resets it.
const guard = createRepetitionGuard('chat');

const OPEN = 'button[aria-label*="Chat with everyone" i], button[aria-label*="chat" i]';
const INPUT = 'textarea[aria-label*="Send a message" i], textarea[placeholder*="Send a message" i], ' +
              'div[contenteditable="true"][aria-label*="message" i]';
const SEND = 'button[aria-label*="Send message" i], button[aria-label*="Send a message" i]';

export interface ChatController {
  send(text: string): Promise<void>;
  /** The bot's own replies (an opt-out confirmation): past the repetition guard, which exists to
   *  stop a brain repeating itself, not to make the second person's confirmation vanish. */
  post(text: string): Promise<void>;
  read(): Promise<void>;
}

export interface ChatMessage { sender: string; text: string }

export interface ChatWatcher { stop(): void }

// Meet AUTO-HIDES the in-call toolbar after a few seconds with no pointer activity, and the bot
// never moves a real mouse — so every toolbar control reads isVisible:false and any click times
// out. The join layer already fights this (googlemeet/admission.ts moves the pointer before
// probing); do the same before touching the toolbar. This is why an act works right after
// another act and fails from idle.
const wakeToolbar = async (page: Page): Promise<void> => {
  await page.mouse.move(640, 360).catch(() => {});
  await page.mouse.move(900, 700).catch(() => {});
  await page.waitForTimeout(400);
};

const panelOpen = (page: Page): Promise<boolean> =>
  page.locator(INPUT).first().isVisible().catch(() => false);

const openPanel = async (page: Page): Promise<void> => {
  if (await panelOpen(page)) return;
  await wakeToolbar(page);
  await page.locator(OPEN).first().waitFor({ state: 'visible', timeout: 20_000 });
  await page.locator(OPEN).first().click({ timeout: 10_000 });
  await page.locator(INPUT).first().waitFor({ state: 'visible', timeout: 10_000 });
};

// REAL Meet (dumped 2026-08-11) exposes only `data-message-id` on the message container — there
// is NO `data-message-text` attribute, and no `data-sender-name` alongside it. An earlier
// version read those attributes and returned NOTHING in production while passing a mock that
// had invented them. So: take the container's text, minus the nested controls, because raw
// textContent is "dom dump probeHover over a message to pin itkeepPin message".
const messages = (page: Page): Promise<ChatMessage[]> => page.evaluate(() => {
  const doc = (globalThis as any).document;
  const log = doc.querySelector('[role="log"], [aria-live="polite"]') || doc;
  const out: Array<{ sender: string; text: string }> = [];
  for (const node of Array.from(log.querySelectorAll('div[data-message-id]')) as any[]) {
    if (node.closest('button')) continue;              // the "keep" button carries the id too
    const clone = node.cloneNode(true) as any;
    clone.querySelectorAll('button,[role="button"],[aria-label]').forEach((b: any) => b.remove());
    const text = (clone.textContent || '').replace(/\s+/g, ' ').trim();
    if (!text) continue;
    const holder = node.closest('[data-sender-name]');
    out.push({ sender: (holder?.getAttribute('data-sender-name') || '').trim(), text });
  }
  return out;
});

export function createChatController(page: Page): ChatController {
  const deliver = async (text: string): Promise<void> => {
    await page.bringToFront();
    await sweep(page, 'chat', (m) => console.log(m));
    await openPanel(page);
    const box = page.locator(INPUT).first();
    // A throw here used to vanish — the act handler swallowed it, so a blocked composer looked
    // exactly like a delivered message. Name it, then rethrow: never a silent failure.
    await box.click({ timeout: 5_000 }).catch((e: any) => {
      console.log('[chat] BLOCKED — composer not clickable: ' + String(e).slice(0, 120));
      throw e;
    });
    await box.fill(text);
    // Enter first (Meet's normal path); fall back to the explicit send button if the box did
    // not clear, which is the observable signal that the message was actually submitted.
    await box.press('Enter');
    if ((await box.inputValue().catch(() => '')) === text) {
      await page.locator(SEND).first().click({ timeout: 5_000 });
    }
    console.log('[chat] ' + JSON.stringify({ sent: text }));
  };

  return {
    async send(text: string): Promise<void> {
      // L+: anti-repetition guard — suppress duplicates within window
      const verdict = guard.check(text);
      if (!verdict.allowed) {
        const ageMs = Date.now() - verdict.seenAt;
        console.log(`[guard] suppressed repeat chat: "${text.slice(0, 60)}" (seen ${Math.round(ageMs / 1000)}s ago as "${verdict.normalized}")`);
        return; // LOUD suppression — no silent fallbacks
      }
      await deliver(text);
    },

    post: deliver,

    async read(): Promise<void> {
      await page.bringToFront();
      await dismissModals(page);
      await openPanel(page);
      // A level-2 opt-out keeps the name out of everything written down, this line included.
      const seen = (await messages(page)).filter((m) => optOutLevel(m.sender) < 2);
      console.log('[chat] ' + JSON.stringify({ messages: seen }));
    },
  };
}

/**
 * Hand every NEW chat message to `onMessage`, from the moment this starts. Meet delivers nothing
 * sent before the bot joined, so the first scan is all new. A message is counted, not just seen:
 * the same person typing the same thing twice is two messages, and nothing is replayed if the
 * panel closes and reopens (counts never go down). The panel is reopened when something else
 * (a modal sweep's Escape) has closed it, at most every 15 s, because opening it moves the mouse
 * and clicks the toolbar, which an act in flight would not thank it for.
 */
export function createChatWatcher(page: Page, onMessage: (m: ChatMessage) => void,
                                  log: (m: string) => void = (m) => console.log(m),
                                  intervalMs = 2000): ChatWatcher {
  const counted = new Map<string, number>();
  let lastOpen = 0, busy = false, stopped = false;

  const tick = async (): Promise<void> => {
    if (busy || stopped) return;
    busy = true;
    try {
      if (!(await panelOpen(page))) {
        if (Date.now() - lastOpen < 15_000) return;
        lastOpen = Date.now();
        await sweep(page, 'chat', log);
        await openPanel(page);
        log('[chat] watcher opened the panel');
      }
      const now = new Map<string, { n: number; m: ChatMessage }>();
      for (const m of await messages(page)) {
        const k = `${m.sender}\u0000${m.text}`;
        now.set(k, { n: (now.get(k)?.n ?? 0) + 1, m });
      }
      for (const [k, { n, m }] of now) {
        const had = counted.get(k) ?? 0;
        if (n <= had) continue;
        counted.set(k, n);
        for (let i = had; i < n; i++) onMessage(m);
      }
    } catch (e) {
      log(`[chat] watcher: ${String(e).slice(0, 160)}`);
    } finally {
      busy = false;
    }
  };

  const timer = setInterval(() => { void tick(); }, intervalMs);
  void tick();
  return { stop() { stopped = true; clearInterval(timer); } };
}
