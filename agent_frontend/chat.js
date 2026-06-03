const API_BASE = (window.SUPERVISOR_API_URL || "http://127.0.0.1:8000").replace(/\/$/, "");

const chatMessages = document.getElementById("chat-messages");
const chatForm = document.getElementById("chat-form");
const chatInput = document.getElementById("chat-input");
const sendBtn = document.getElementById("send-btn");
const chatStatus = document.getElementById("chat-status");
const agentWallet = document.getElementById("agent-wallet");

let history = [];
let isSending = false;

function appendMessage(role, content, extraClass = "") {
  const wrapper = document.createElement("div");
  wrapper.className = `message ${role} ${extraClass}`.trim();
  const bubble = document.createElement("div");
  bubble.className = "bubble";
  bubble.textContent = content;
  wrapper.appendChild(bubble);
  chatMessages.appendChild(wrapper);
  chatMessages.scrollTop = chatMessages.scrollHeight;
  return wrapper;
}

function setStatus(text, isError = false) {
  chatStatus.textContent = text;
  chatStatus.classList.toggle("error", isError);
}

function setComposerBusy(busy) {
  isSending = busy;
  sendBtn.disabled = busy;
  chatInput.disabled = busy;
}

async function loadChatHealth() {
  const supervisorLink = document.getElementById("supervisor-link");
  if (supervisorLink) supervisorLink.href = `${API_BASE}/`;

  try {
    const response = await fetch(`${API_BASE}/agent/chat/health`);
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || "Health check failed");
    agentWallet.textContent = `Agent: ${shortenAddress(data.agentAddress)}`;
    if (!data.openaiConfigured) {
      setStatus("OPENAI_API_KEY is missing in .env — chat will not work.", true);
      return;
    }
    setStatus(`Model: ${data.model}. Provider backends must run on ports 8001 and 8002.`);
  } catch (error) {
    setStatus(`Connection error: ${error.message}`, true);
  }
}

function shortenAddress(address) {
  if (!address || address.length < 12) return address || "--";
  return `${address.slice(0, 8)}...${address.slice(-6)}`;
}

chatForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const message = chatInput.value.trim();
  if (!message || isSending) return;

  appendMessage("user", message);
  chatInput.value = "";
  history.push({ role: "user", content: message });

  const typingEl = appendMessage(
    "assistant",
    "Thinking and checking whether data should be purchased...",
    "typing"
  );
  setComposerBusy(true);
  setStatus("Agent is processing (on-chain purchase can take 1–3 minutes on Sepolia)...");

  const controller = new AbortController();
  const chatTimeoutMs = 300000;
  const timeoutId = window.setTimeout(() => controller.abort(), chatTimeoutMs);

  try {
    const response = await fetch(`${API_BASE}/agent/chat`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message, history: history.slice(0, -1) }),
      signal: controller.signal,
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) {
      throw new Error(
        typeof payload.detail === "string" ? payload.detail : JSON.stringify(payload.detail || payload)
      );
    }
    typingEl.remove();
    appendMessage("assistant", payload.reply || "(no reply)");
    history = payload.history || history;
    setStatus("Done. Send another message.");
  } catch (error) {
    history.pop();
    typingEl.remove();
    const message =
      error.name === "AbortError"
        ? "Request timed out. Sepolia transactions can be slow — try again in a minute."
        : error.message;
    appendMessage("assistant", `Error: ${message}`);
    setStatus(message, true);
  } finally {
    window.clearTimeout(timeoutId);
    setComposerBusy(false);
    chatInput.focus();
  }
});

loadChatHealth();
