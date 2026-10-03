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
    chrome.runtime.sendMessage({ target: "sidepanel", cmd: "tabInvoked", tabId: tab.id })
      .catch(() => {});   // panel not open yet: Start will find the grant in place
  }
});

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
        const [tab] = msg.tabId != null
          ? [await chrome.tabs.get(msg.tabId)]
          : await chrome.tabs.query({ active: true, lastFocusedWindow: true });
        if (!tab) throw new Error("No active meeting tab found — focus the Meet/Teams tab.");
        const streamId = await getStreamId(tab.id);
        sendResponse({ ok: true, streamId, tabTitle: tab.title });
      }
    } catch (e) {
      sendResponse({ ok: false, error: String(e?.message || e) });
    }
  })();
  return true; // async sendResponse
});
