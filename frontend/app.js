const statusElement = document.getElementById("agent-status");
const oracleAddress = document.getElementById("oracle-address");
const oracleLast = document.getElementById("oracle-last");
const oracleHealth = document.getElementById("oracle-health");
const contractInfo = document.getElementById("contract-info");
const refreshButton = document.getElementById("refresh-status");

// Contract state elements
const maxAmountEl = document.getElementById("max-amount");
const contractBalanceEl = document.getElementById("contract-balance");
const contractStatusEl = document.getElementById("contract-status");
const requestCountEl = document.getElementById("request-count");

// New elements
const alertListEl = document.querySelector(".alert-list");
const recentRequestsContainerEl = document.querySelector(".card-6");
const approveddProvidersEl = document.getElementById("approved-providers");
const allowedEndpointsEl = document.getElementById("allowed-endpoints");

// Store contract attachment status from health check
let contractAttachedStatus = null;

// Format ETH with dynamic decimal places
function formatEth(weiValue) {
  const eth = weiValue / 1e18;
  if (eth === 0) return "0 ETH";
  if (eth < 0.0001) return eth.toLocaleString('en-US', { minimumFractionDigits: 10, maximumFractionDigits: 18 }) + " ETH";
  if (eth < 1) return eth.toFixed(8) + " ETH";
  return eth.toFixed(4) + " ETH";
}

async function loadHealth() {
  try {
    const response = await fetch("/health");
    if (!response.ok) {
      throw new Error("Backend unavailable");
    }
    const result = await response.json();
    console.log("Health check result:", result);
    contractAttachedStatus = result.contractAttached ? "Contract: attached" : "Contract: not attached";
    console.log("Set contractAttachedStatus to:", contractAttachedStatus);
    contractInfo.textContent = `${contractAttachedStatus} | Web3 provider: ${result.web3Provider} (connected: ${result.connected})`;
    console.log("Updated contractInfo.textContent:", contractInfo.textContent);
    return result;
  } catch (error) {
    contractInfo.textContent = "Backend not available";
    console.error("Health check error:", error);
  }
}

async function loadDashboardData() {
  try {
    const response = await fetch("/dashboard-summary");
    if (!response.ok) {
      throw new Error("Cannot load dashboard data");
    }
    const data = await response.json();
    
    // Oracle status
    oracleAddress.textContent = data.oracle.substring(0, 10) + "..." + data.oracle.substring(data.oracle.length - 4);
    oracleLast.textContent = new Date().toLocaleTimeString();
    oracleHealth.textContent = data.paused ? "Paused" : "OK";
    statusElement.textContent = data.paused ? "Paused" : "Active";
    
    // Contract budget info
    if (maxAmountEl) {
      maxAmountEl.textContent = formatEth(data.maxAmountWei);
    }
    if (contractBalanceEl) {
      contractBalanceEl.textContent = formatEth(data.balanceWei);
    }
    if (contractStatusEl) {
      contractStatusEl.textContent = data.paused ? "PAUSED" : "ACTIVE";
      contractStatusEl.style.color = data.paused ? "red" : "green";
    }
    if (requestCountEl) {
      requestCountEl.textContent = data.requestCount;
    }
    
    // Alerts
    if (alertListEl) {
      alertListEl.innerHTML = "";
      if (data.paused) {
        alertListEl.innerHTML += "<li style='color: red;'>⚠️ Contract is PAUSED</li>";
      } else {
        alertListEl.innerHTML += "<li style='color: green;'>✓ Contract is ACTIVE</li>";
      }
      const usagePercent = data.balanceWei > 0 ? Math.min(100, (data.maxAmountWei / data.balanceWei) * 100) : 0;
      alertListEl.innerHTML += "<li>Balance usage: " + usagePercent.toFixed(1) + "%</li>";
      alertListEl.innerHTML += "<li>Total requests: " + data.requestCount + "</li>";
    }

    // Load recent requests
    if (data.requests && data.requests.length > 0) {
      const requestsHTML = data.requests.map(r => `
        <div class="request-row">
          <span>#${r.requestId}</span>
          <strong>${r.resourceId}</strong>
          <span>${formatEth(r.amountWei)}</span>
          <span style="color: ${r.fulfilled ? 'green' : 'orange'}">${r.fulfilled ? 'Fulfilled' : 'Pending'}</span>
        </div>`
      ).join("");
      const requestSection = document.getElementById("recent-requests-list");
      if (requestSection) {
        requestSection.innerHTML = requestsHTML + `<div class="request-row"><span>Total:</span><strong id="request-count">${data.requestCount}</strong></div>`;
      }
    }
    
    // Update contract info footer (preserve attachment status from loadHealth)
    if (contractAttachedStatus) {
      contractInfo.textContent = `${contractAttachedStatus} | Owner: ${data.owner.substring(0, 10)}... | Balance: ${formatEth(data.balanceWei)}`;
    } else {
      contractInfo.textContent = `Contract: ${data.address.substring(0, 10)}... | Owner: ${data.owner.substring(0, 10)}... | Balance: ${formatEth(data.balanceWei)}`;
    }

    // Load approved providers/endpoints
    if (allowedEndpointsEl) {
      if (data.approvedProviders && data.approvedProviders.length > 0) {
        allowedEndpointsEl.innerHTML = data.approvedProviders
          .map(provider => `<li>${provider.substring(0, 10)}...${provider.substring(provider.length - 4)}</li>`)
          .join("");
      } else {
        allowedEndpointsEl.innerHTML = "<li>No approved endpoints</li>";
      }
    }
  } catch (error) {
    console.error("Dashboard data error:", error);
    statusElement.textContent = "Unavailable";
    // Don't overwrite the status if we already got it from loadHealth
    if (!contractAttachedStatus) {
      contractInfo.textContent = error.message || "Dashboard data unavailable";
    }
  }
}

refreshButton.addEventListener("click", async () => {
  console.log("Refresh button clicked");
  await loadHealth();
  await loadDashboardData();
});

console.log("Refresh button element:", refreshButton);

window.addEventListener("load", async () => {
  await loadHealth();
  await loadDashboardData();
});
