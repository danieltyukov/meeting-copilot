// Service worker: opens the side panel and hands it a tab-capture stream id.
// All audio capture/processing happens in the side panel (which has a user
// gesture, so its AudioContext actually runs — an offscreen doc's does not).

// This click is also what lets Chrome capture the tab: tabCapture only works on a
// tab the extension was invoked on (toolbar icon, its shortcut or menu entry).
// No permission, <all_urls> included, stands in for it. So tell an open panel,
// and a recording that is still missing the call joins it now.
chrome.action.onClicked.addListener(async (tab) => {
  if (tab?.windowId != null) await chrome.sidePanel.open({ windowId: tab.windowId });
  if (tab?.id != null) {
    chrome.runtime.sendMessage({ target: "sidepanel", cmd: "tabInvoked", tabId: tab.id, meeting: isCallTab(tab) })
      .catch(() => {});   // panel not open yet: Start will find the grant in place
  }
});

// Call pages. Start looks for one of these before falling back to the tab in
// front: Start pressed while Spotify was in front used to transcribe Spotify.
const CALL_URL = new RegExp("^https://(" + [
  "meet\\.google\\.com/[a-z]{3}-[a-z]{4}-[a-z]{3}",
  "teams\\.microsoft\\.com/", "teams\\.live\\.com/", "teams\\.cloud\\.microsoft/",
  "([\\w-]+\\.)?zoom\\.us/wc/", "[\\w-]+\\.webex\\.com/", "meet\\.jit\\.si/", "whereby\\.com/",
].join("|") + ")", "i");
const isCallTab = (tab) => CALL_URL.test(tab?.url || "");

// The front tab if it is a call; else the call that is making sound, else the
// call you looked at last; else the front tab, whatever it is.
async function pickCallTab() {
  const [front] = await chrome.tabs.query({ active: true, lastFocusedWindow: true });
  if (isCallTab(front)) return front;
  const calls = (await chrome.tabs.query({})).filter(isCallTab);
  calls.sort((a, b) => (!!b.audible - !!a.audible) || ((b.lastAccessed || 0) - (a.lastAccessed || 0)));
  return calls[0] || front;
}

function getStreamId(tabId) {
  return new Promise((resolve, reject) => {
    chrome.tabCapture.getMediaStreamId({ targetTabId: tabId }, (id) => {
      const err = chrome.runtime.lastError;
      if (err) reject(err);
      else resolve(id);
    });
  });
}

chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  if (msg?.target !== "background") return;
  (async () => {
    try {
      if (msg.cmd === "getStreamId") {
        const tab = msg.tabId != null ? await chrome.tabs.get(msg.tabId) : await pickCallTab();
        if (!tab) throw new Error("No active meeting tab found — focus the Meet/Teams tab.");
        const streamId = await getStreamId(tab.id);
        // tabId: the panel reads who is in the call from this same tab.
        sendResponse({ ok: true, streamId, tabTitle: tab.title, tabId: tab.id, meeting: isCallTab(tab) });
      }
    } catch (e) {
      sendResponse({ ok: false, error: String(e?.message || e) });
    }
  })();
  return true; // async sendResponse
});
