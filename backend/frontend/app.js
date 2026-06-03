const statusElement = document.getElementById("agent-status");
const oracleAddress = document.getElementById("oracle-address");
const oracleLast = document.getElementById("oracle-last");
const oracleHealth = document.getElementById("oracle-health");
const contractInfo = document.getElementById("contract-info");
const refreshButton = document.getElementById("refresh-status");
const pauseButton = document.getElementById("pause-btn");
const recentRequestsListEl = document.getElementById("recent-requests-list");
const editLimitsButton = document.getElementById("edit-limits-btn");
const addProviderButton = document.getElementById("add-provider-btn");
const removeProviderButton = document.getElementById("remove-provider-btn");

const maxAmountEl = document.getElementById("max-amount");
const contractBalanceEl = document.getElementById("contract-balance");
const contractStatusEl = document.getElementById("contract-status");
const requestCountEl = document.getElementById("request-count");

const alertListEl = document.querySelector(".alert-list");
const providerPaymentsListEl = document.getElementById("provider-payments-list");
const policyContractAddressEl = document.getElementById("policy-contract-address");
const policyApprovedCountEl = document.getElementById("policy-approved-count");

let contractAttachedStatus = null;
let currentPaused = false;
let currentMaxAmountWei = 0;
let providerRegistry = [];

const ETH_ADDRESS_RE = /^0x[a-fA-F0-9]{40}$/i;
const REQUEST_TIMEOUT_MS = 180000;
const BLOCKCHAIN_TIMEOUT_MS = 360000;

function formatWei(weiValue) {
  if (weiValue === undefined || weiValue === null) return "-- wei";
  const n = Number(weiValue);
  if (!Number.isFinite(n) || n < 0) return "-- wei";
  return `${n.toLocaleString("en-US")} wei`;
}

function formatPriceWei(priceWei) {
  if (!priceWei || priceWei <= 0) return "Not set on chain";
  return formatWei(priceWei);
}

function shortenAddress(address) {
  if (!address || address.length < 12) return address || "--";
  return `${address.substring(0, 8)}...${address.substring(address.length - 6)}`;
}

function setActionMessage(message, isError = false) {
  contractInfo.textContent = message;
  contractInfo.style.color = isError ? "#b42318" : "#4b4b4b";
}

function setButtonBusy(button, busy) {
  button.disabled = busy;
  button.setAttribute("aria-busy", busy ? "true" : "false");
}

async function postJson(url, body, timeoutMs = REQUEST_TIMEOUT_MS) {
  const controller = new AbortController();
  const timeoutId = window.setTimeout(() => controller.abort(), timeoutMs);
  let response;
  try {
    response = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
      signal: controller.signal,
    });
  } catch (error) {
    if (error.name === "AbortError") {
      throw new Error("Request timed out while waiting for blockchain confirmation.");
    }
    throw error;
  } finally {
    window.clearTimeout(timeoutId);
  }

  let payload = {};
  try {
    payload = await response.json();
  } catch {
    payload = {};
  }
  if (!response.ok) {
    const detail = payload.detail;
    const message = typeof detail === "string" ? detail : JSON.stringify(detail || payload);
    throw new Error(message || `Request failed (${response.status})`);
  }
  return payload;
}

function parseWeiInput(weiInput) {
  const normalized = weiInput.trim().replace(/_/g, "").replace(/,/g, "");
  if (!/^\d+$/.test(normalized)) {
    throw new Error("Enter a whole number of wei, e.g. 1000000000000000");
  }
  const wei = BigInt(normalized);
  if (wei <= 0n) {
    throw new Error("Amount must be greater than 0 wei");
  }
  if (wei > BigInt(Number.MAX_SAFE_INTEGER)) {
    throw new Error("Amount is too large for this UI");
  }
  return Number(wei);
}

function promptProviderAddress(actionLabel) {
  const value = window.prompt(`${actionLabel} provider address (0x...):`);
  if (value === null) return null;
  const trimmed = value.trim();
  if (!ETH_ADDRESS_RE.test(trimmed)) {
    throw new Error("Invalid Ethereum address");
  }
  return trimmed;
}

async function promptAndSetProviderPrice(providerAddress, defaultWei = 0) {
  const defaultValue =
    defaultWei > 0
      ? String(defaultWei)
      : currentMaxAmountWei > 0
        ? String(Math.max(1, Math.floor(currentMaxAmountWei / 100)))
        : "1";
  const maxWeiLabel =
    currentMaxAmountWei > 0 ? `${currentMaxAmountWei} wei` : "contract max (refresh first)";
  const input = window.prompt(
    `On-chain payment for ${shortenAddress(providerAddress)} (wei, must be ≤ ${maxWeiLabel}):`,
    defaultValue
  );
  if (input === null) return null;
  const priceWei = parseWeiInput(input);
  if (currentMaxAmountWei > 0 && priceWei > currentMaxAmountWei) {
    throw new Error(`Price exceeds contract max (${formatWei(currentMaxAmountWei)})`);
  }
  return postJson("/set-provider-price", { provider: providerAddress, priceWei });
}

function updatePauseButtonLabel() {
  if (!pauseButton) return;
  pauseButton.textContent = currentPaused ? "Resume Agent" : "Pause Agent";
}

function approvalPill(approved) {
  return approved
    ? '<span class="provider-status-pill approved">Approved</span>'
    : '<span class="provider-status-pill not-approved">Not approved</span>';
}

function pricePill(priceWei) {
  if (!priceWei || priceWei <= 0) {
    return '<span class="provider-status-pill no-price">Price not set</span>';
  }
  return `<span class="provider-status-pill approved">On-chain: ${formatWei(priceWei)}</span>`;
}

function renderProviderRegistry(providers) {
  providerRegistry = providers || [];
  if (!providerPaymentsListEl) return;
  if (!providerRegistry.length) {
    providerPaymentsListEl.innerHTML =
      "<li class='empty-state'>No providers on chain yet — use Approve provider, then Refresh</li>";
    return;
  }

  const paymentHtml = providerRegistry
    .map(
      (provider) => `
        <li class="provider-item">
          <div class="provider-item-header">
            <span class="mono-wrap provider-address">${provider.address}</span>
            ${approvalPill(provider.approved)}
          </div>
          <div class="provider-meta">${pricePill(provider.priceWei)}</div>
          <div class="provider-actions">
            <button type="button" class="set-price-btn" data-address="${provider.address}">Set on-chain price</button>
          </div>
        </li>`
    )
    .join("");
  providerPaymentsListEl.innerHTML = paymentHtml;

  document.querySelectorAll(".set-price-btn").forEach((button) => {
    button.addEventListener("click", () => handleSetProviderPrice(button.dataset.address, button));
  });
}

async function handleSetProviderPrice(address, button) {
  const entry = providerRegistry.find((p) => p.address.toLowerCase() === address.toLowerCase());
  setButtonBusy(button, true);
  try {
    setActionMessage(`Setting on-chain price for ${shortenAddress(address)}...`);
    const result = await promptAndSetProviderPrice(address, entry?.priceWei || 0);
    if (!result) {
      setActionMessage("Price update cancelled.");
      return;
    }
    setActionMessage(`${result.message} Tx: ${result.transactionHash}. Click Refresh.`);
  } catch (error) {
    setActionMessage(`Set price failed: ${error.message}`, true);
  } finally {
    setButtonBusy(button, false);
  }
}

function requestStatusLabel(request) {
  if (request.paid && request.fulfilled) {
    return { text: "Paid to provider", color: "#15803d" };
  }
  if (request.paid && !request.fulfilled) {
    return { text: "Returned to buyer", color: "#6d28d9" };
  }
  if (request.fulfilled) {
    return { text: "Awaiting release", color: "#c2410c" };
  }
  return { text: "Awaiting confirm", color: "#b42318" };
}

function formatOracleConfirmation(lastConfirmation) {
  if (!lastConfirmation?.confirmedAt) return "No DeliveryConfirmed on chain";
  const when = new Date(lastConfirmation.confirmedAt);
  const local = Number.isNaN(when.getTime()) ? lastConfirmation.confirmedAt : when.toLocaleString();
  return `Request #${lastConfirmation.requestId} · ${local}`;
}

function renderRecentRequests(data) {
  if (!recentRequestsListEl) return;

  const requests = data.recentRequests || [];
  if (requests.length === 0) {
    recentRequestsListEl.innerHTML =
      '<p class="empty-state">No requests on chain yet.</p>' +
      `<div class="request-total"><span>Total</span><strong id="request-count">${data.requestCount}</strong></div>`;
    return;
  }

  const requestsHTML = requests
    .map((r) => {
      const status = requestStatusLabel(r);
      const actions = r.paid
        ? ""
        : `<div class="request-actions">
            <button type="button" class="release-request-btn" data-request-id="${r.requestId}">Release to provider</button>
            <button type="button" class="return-request-btn" data-request-id="${r.requestId}">Return to buyer</button>
          </div>`;
      return `
        <article class="request-item">
          <div class="request-item-header">
            <span class="request-id">#${r.requestId}</span>
            <strong class="request-resource">${r.resourceId}</strong>
          </div>
          <div class="request-meta">
            <span>${formatWei(r.amountWei)}</span>
            <span class="request-status" style="color: ${status.color}">${status.text}</span>
          </div>
          ${actions}
        </article>`;
    })
    .join("");

  recentRequestsListEl.innerHTML =
    requestsHTML +
    `<div class="request-total"><span>Total</span><strong id="request-count">${data.requestCount}</strong></div>`;
}

async function handleRequestAction(requestId, action, button) {
  const endpoint = action === "release" ? "/release-payment" : "/refund-to-buyer";
  const label = action === "release" ? "Releasing to provider" : "Returning to buyer";
  setButtonBusy(button, true);
  try {
    setActionMessage(`Request #${requestId}: ${label} on Sepolia (may take 1–2 min)...`);
    const result = await postJson(endpoint, { requestId }, BLOCKCHAIN_TIMEOUT_MS);
    setActionMessage(result.message || `${label} completed. Click Refresh.`);
    await loadDashboardData();
  } catch (error) {
    const hint =
      action === "return" && /refundToRequester|function|revert/i.test(error.message)
        ? " Refund needs contract with refundToRequester — redeploy if you use an older deployment."
        : "";
    setActionMessage(`Request #${requestId}: ${error.message}${hint}`, true);
  } finally {
    setButtonBusy(button, false);
  }
}

function bindRecentRequestActions() {
  if (!recentRequestsListEl || recentRequestsListEl.dataset.bound === "1") return;
  recentRequestsListEl.dataset.bound = "1";
  recentRequestsListEl.addEventListener("click", (event) => {
    const releaseBtn = event.target.closest(".release-request-btn");
    if (releaseBtn) {
      handleRequestAction(Number(releaseBtn.dataset.requestId), "release", releaseBtn);
      return;
    }
    const returnBtn = event.target.closest(".return-request-btn");
    if (returnBtn) {
      handleRequestAction(Number(returnBtn.dataset.requestId), "return", returnBtn);
    }
  });
}

function updateConfiguredContractAddress(address) {
  if (!address || !policyContractAddressEl) return;
  policyContractAddressEl.textContent = address;
}

async function loadHealth() {
  const response = await fetch("/health");
  if (!response.ok) {
    throw new Error("Backend unavailable");
  }
  const result = await response.json();
  contractAttachedStatus = result.contractAttached
    ? `Contract: attached (${result.contractAddress})`
    : "Contract: not attached — set CONTRACT_ADDRESS in .env";
  if (result.contractAttached && result.contractAddress) {
    updateConfiguredContractAddress(result.contractAddress);
  }
  return result;
}

async function loadDashboardData() {
  const response = await fetch("/dashboard-summary");
  if (!response.ok) {
    const payload = await response.json().catch(() => ({}));
    throw new Error(payload.detail || "Cannot load dashboard data");
  }
  const data = await response.json();

  currentPaused = Boolean(data.paused);
  currentMaxAmountWei = data.maxAmountWei;
  updatePauseButtonLabel();

  oracleAddress.textContent = shortenAddress(data.oracle);
  oracleLast.textContent = formatOracleConfirmation(data.lastOracleConfirmation);
  oracleHealth.textContent = data.paused ? "Paused" : "OK";
  statusElement.textContent = data.paused ? "Paused" : "Active";

  if (maxAmountEl) maxAmountEl.textContent = formatWei(data.maxAmountWei);
  if (contractBalanceEl) contractBalanceEl.textContent = formatWei(data.balanceWei);
  if (contractStatusEl) {
    contractStatusEl.textContent = data.paused ? "PAUSED" : "ACTIVE";
    contractStatusEl.style.color = data.paused ? "red" : "green";
  }
  if (requestCountEl) requestCountEl.textContent = data.requestCount;

  if (alertListEl) {
    alertListEl.innerHTML = "";
    if (data.paused) {
      alertListEl.innerHTML += "<li class='alert-paused'>Contract is PAUSED</li>";
    } else {
      alertListEl.innerHTML += "<li class='alert-active'>Contract is ACTIVE</li>";
    }
    alertListEl.innerHTML += `<li>Total requests: ${data.requestCount}</li>`;
    const missingPrices = (data.providers || []).filter(
      (p) => p.approved && (!p.priceWei || p.priceWei <= 0)
    );
    if (missingPrices.length) {
      alertListEl.innerHTML += `<li class='alert-paused'>${missingPrices.length} approved provider(s) without on-chain price</li>`;
    }
    if (data.pendingPaymentCount > 0) {
      alertListEl.innerHTML += `<li class='alert-paused'>${data.pendingPaymentCount} request(s) with unsettled escrow — use actions in Recent Requests</li>`;
    }
  }

  if (data.address) updateConfiguredContractAddress(data.address);
  if (policyApprovedCountEl) {
    policyApprovedCountEl.textContent = String((data.approvedProviders || []).length);
  }

  renderRecentRequests(data);
  renderProviderRegistry(data.providers || []);

  const refreshedAt = new Date().toLocaleTimeString();
  contractInfo.textContent = `${contractAttachedStatus} | Owner: ${shortenAddress(data.owner)} | Balance: ${formatWei(data.balanceWei)} | Updated: ${refreshedAt}`;
  contractInfo.style.color = "#4b4b4b";
}

async function refreshDashboard() {
  setButtonBusy(refreshButton, true);
  const previousLabel = refreshButton.textContent;
  refreshButton.textContent = "Refreshing...";
  try {
    setActionMessage("Loading on-chain state...");
    await loadHealth();
    await loadDashboardData();
    setActionMessage("On-chain state loaded. Use Refresh after each transaction.");
  } catch (error) {
    statusElement.textContent = "Unavailable";
    setActionMessage(`Refresh failed: ${error.message}`, true);
  } finally {
    refreshButton.textContent = previousLabel;
    setButtonBusy(refreshButton, false);
  }
}

refreshButton.addEventListener("click", refreshDashboard);

bindRecentRequestActions();

pauseButton.addEventListener("click", async () => {
  const nextPaused = !currentPaused;
  const actionLabel = nextPaused ? "Pausing" : "Resuming";
  setButtonBusy(pauseButton, true);
  try {
    setActionMessage(`${actionLabel} agent on chain...`);
    const result = await postJson("/set-paused", { paused: nextPaused });
    setActionMessage(`${actionLabel} sent. Tx: ${result.transactionHash}. Click Refresh to update UI.`);
  } catch (error) {
    setActionMessage(`${actionLabel} failed: ${error.message}`, true);
  } finally {
    setButtonBusy(pauseButton, false);
  }
});

editLimitsButton.addEventListener("click", async () => {
  const defaultWei = currentMaxAmountWei > 0 ? String(currentMaxAmountWei) : "1000";
  const input = window.prompt("New max payment per request (wei):", defaultWei);
  if (input === null) return;

  setButtonBusy(editLimitsButton, true);
  try {
    const maxAmountWei = parseWeiInput(input);
    setActionMessage("Updating payment limit on chain...");
    const result = await postJson("/set-max-amount", { maxAmountWei });
    setActionMessage(`Limit update sent. Tx: ${result.transactionHash}. Click Refresh to update UI.`);
  } catch (error) {
    setActionMessage(`Limit update failed: ${error.message}`, true);
  } finally {
    setButtonBusy(editLimitsButton, false);
  }
});

addProviderButton.addEventListener("click", async () => {
  setButtonBusy(addProviderButton, true);
  try {
    const provider = promptProviderAddress("Approve");
    if (!provider) return;

    setActionMessage(`Approving ${provider} on chain...`);
    const result = await postJson("/approve-provider", { provider, approved: true });
    setActionMessage(`${result.message} Tx: ${result.transactionHash}.`);

    const priceResult = await promptAndSetProviderPrice(provider);
    if (priceResult) {
      setActionMessage(`${priceResult.message} Tx: ${priceResult.transactionHash}. Click Refresh.`);
    } else {
      setActionMessage(`${result.message} Set on-chain price before agent purchases. Click Refresh.`);
    }
  } catch (error) {
    setActionMessage(`Approve failed: ${error.message}`, true);
  } finally {
    setButtonBusy(addProviderButton, false);
  }
});

removeProviderButton.addEventListener("click", async () => {
  setButtonBusy(removeProviderButton, true);
  try {
    const provider = promptProviderAddress("Revoke");
    if (!provider) return;

    setActionMessage(`Revoking ${provider} on chain...`);
    const result = await postJson("/approve-provider", { provider, approved: false });
    setActionMessage(`${result.message} Tx: ${result.transactionHash}. Click Refresh to update list.`);
  } catch (error) {
    setActionMessage(`Revoke failed: ${error.message}`, true);
  } finally {
    setButtonBusy(removeProviderButton, false);
  }
});

setActionMessage("Click Refresh contract status to load on-chain data.");

loadHealth().catch((error) => {
  setActionMessage(`Backend unavailable: ${error.message}`, true);
});
