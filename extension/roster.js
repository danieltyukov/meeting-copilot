// Who is in the call, read from the call page itself. The panel runs this one
// function in the call tab with chrome.scripting.executeScript, which Chrome
// allows once Sparky was invoked on that tab (the toolbar icon or Alt+Shift+S,
// the same invocation tabCapture needs). It is serialized and run there, so it
// must stay self-contained: no closures, no other functions from this file.
//
// It reads display names and nothing else: no messages, no chat, no captions,
// no page text beyond the name elements below. Every strategy is best effort,
// the hits are pooled, and the panel tidies them with cleanRosterName. Returns
// { names: [raw strings], self: "your own display name, or ''" }.

function readMeetingRoster() {
  const MAX = 50;
  const names = [];
  const seen = new Set();
  const lines = (el) => String((el && (el.innerText || el.textContent)) || "")
    .split("\n").map((l) => l.replace(/\s+/g, " ").trim()).filter((l) => /\p{L}/u.test(l));
  const add = (raw) => {
    const name = String(raw || "").split("\n")[0].replace(/\s+/g, " ").trim();
    // A name is short and has a letter; counts like "In this call (3)" are not names.
    if (!name || name.length > 80 || !/\p{L}/u.test(name) || /\(\d+\)/.test(name)) return;
    if (seen.has(name) || names.length >= MAX) return;
    seen.add(name);
    names.push(name);
  };
  const all = (sel, root) => {
    try { return Array.from((root || document).querySelectorAll(sel)); } catch { return []; }
  };
  // Not names: icon ligatures (Material icons render "mic_off", "more_vert",
  // "videocam" as text), the apps' section titles, and tile status words.
  const ICON = /^[a-z0-9_]+$/;
  const SECTION = /^(?:add people|participants|people|attendees|contributors|in this (?:call|meeting)|in the meeting|invited|others invited|suggestions|presenters|organizers?)\b/i;
  const STATUS = /^(?:muted|unmuted|pinned|presenting|speaking|you|host|co-host)$/i;
  // An element that holds a name (Meet's label, Zoom's name, a title) only needs
  // the first checks; free text from a tile or list item could be an icon too.
  const plain = (t) => Boolean(t) && !SECTION.test(t) && !STATUS.test(t) && !/^\d+$/.test(t);
  const textual = (t) => plain(t) && !ICON.test(t);
  // A heading, a button, or an element holding one: structure, not a person.
  const HEADING = 'h1, h2, h3, h4, h5, h6, [role="heading"]';
  const ACTION = 'button, [role="button"]';

  // (a) Google Meet: one tile per participant. Your own tile carries
  // data-self-name, which is also how Meet labels you.
  let self = "";
  const mine = all("[data-self-name]")[0];
  if (mine) self = mine.getAttribute("data-self-name") || "";
  for (const tile of all("[data-participant-id]")) {
    if (tile.hasAttribute("data-self-name")) continue;
    const label = all(".notranslate", tile).map((el) => lines(el)[0]).find(plain);
    const shortest = lines(tile).filter(textual).sort((x, y) => x.length - y.length)[0];
    add(label || shortest);
  }

  // (b) Any participant list exposed to assistive tech (Meet's and Teams'
  // people panels, among others): its items, by visible name or label.
  const LIST = /participant|people|attendee|roster|in (this )?(call|meeting)/i;
  for (const list of all("[role=list]")) {
    if (!LIST.test(list.getAttribute("aria-label") || "")) continue;
    for (const item of all("[role=listitem]", list)) {
      if (item.querySelector(HEADING)) continue;
      const shown = lines(item).filter(textual);
      const label = item.getAttribute("aria-label");
      const name = shown[0] || (plain(label) ? label : "");
      // "Copy joining info" and its kind: an item that is one button and its
      // label is an action, not a person.
      const action = shown.length <= 1 && all(ACTION, item).some((b) => lines(b).filter(textual).join() === name);
      if (name && !action) add(name);
    }
  }

  // (c) Zoom on the web.
  for (const el of all(".participants-item__display-name, .video-avatar__avatar-name")) {
    if (plain(lines(el)[0])) add(lines(el)[0]);
  }

  // (d) Teams on the web: roster and participant elements, innermost only, so
  // a list or section is never mistaken for a name; headings, buttons, counts
  // and section titles are skipped.
  const TID = '[data-tid*="roster"], [data-tid*="participant"]';
  for (const el of all(TID)) {
    if (el.querySelector(TID) || el.matches(HEADING) || el.querySelector(HEADING) || el.matches(ACTION)) continue;
    const title = el.getAttribute("title");
    if (title ? plain(title) : textual(lines(el)[0])) add(title || lines(el)[0]);
  }

  return { names, self };
}
