// Microphone permission helper. Runs as a normal extension tab (not the side
// panel) so Chrome actually renders the getUserMedia prompt. The grant is keyed
// to the extension origin, so the side panel inherits it afterwards.

const msg = document.getElementById("msg");
const btn = document.getElementById("grant");

async function request() {
  msg.style.color = "var(--muted)";
  msg.textContent = "Requesting... (click Allow on Chrome's prompt)";
  try {
    const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    stream.getTracks().forEach((t) => t.stop());   // we only needed the grant
    msg.style.color = "var(--teal)";
    msg.textContent = "Granted. Close this tab and press Fix microphone access again in the side panel.";
    btn.textContent = "Granted";
    btn.disabled = true;
  } catch (e) {
    msg.style.color = "var(--red)";
    msg.textContent = e.name === "NotAllowedError"
      ? "Blocked. Click the camera/mic icon in the address bar, choose Allow, then click the button again.\n" +
        "If there is no icon, open chrome://settings/content/microphone and remove any block for this extension."
      : "Failed: " + (e.message || e);
  }
}

btn.addEventListener("click", request);
