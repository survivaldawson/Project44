
/* =========================================
   PROJECT 44
   Main Frontend JavaScript
========================================= */

// =========================================
// 1. CONFIGURATION
// =========================================

const API_BASE = "";

const ADSGRAM_BLOCK_ID = "YOUR_BLOCK_ID";

const tg = window.Telegram?.WebApp;

if (tg) {
    tg.ready();
    tg.expand();
}


// =========================================
// 2. DOM ELEMENTS
// =========================================

const elements = {
    userName: document.getElementById("userName"),
    userAvatar: document.getElementById("userAvatar"),
    pointsBalance: document.getElementById("pointsBalance"),
    completedAds: document.getElementById("completedAds"),
    referralCount: document.getElementById("referralCount"),

    watchAdBtn: document.getElementById("watchAdBtn"),
    adMessage: document.getElementById("adMessage"),

    referralLink: document.getElementById("referralLink"),
    copyReferralBtn: document.getElementById("copyReferralBtn"),
    inviteBtn: document.getElementById("inviteBtn"),
    referralMessage: document.getElementById("referralMessage"),

    withdrawAsset: document.getElementById("withdrawAsset"),
    withdrawPoints: document.getElementById("withdrawPoints"),
    walletAddress: document.getElementById("walletAddress"),
    network: document.getElementById("network"),
    giftTelegram: document.getElementById("giftTelegram"),

    walletFields: document.getElementById("walletFields"),
    giftFields: document.getElementById("giftFields"),

    estimatedValue: document.getElementById("estimatedValue"),
    withdrawBtn: document.getElementById("withdrawBtn"),
    withdrawMessage: document.getElementById("withdrawMessage"),

    withdrawalHistory: document.getElementById("withdrawalHistory")
};


// =========================================
// 3. TELEGRAM AUTHENTICATION
// =========================================

function getTelegramInitData() {
    return tg?.initData || "";
}


// =========================================
// 4. API HELPER
// =========================================

async function apiRequest(endpoint, options = {}) {

    const initData = getTelegramInitData();

    const headers = {
        "Content-Type": "application/json",
        "X-Telegram-Init-Data": initData,
        ...options.headers
    };

    const response = await fetch(API_BASE + endpoint, {
        ...options,
        headers
    });

    let data;

    try {
        data = await response.json();
    } catch {
        throw new Error("The server returned an invalid response.");
    }

    if (!response.ok) {
        throw new Error(data.message || "Something went wrong.");
    }

    return data;
}


// =========================================
// 5. MESSAGE HELPER
// =========================================

function showMessage(element, message, type = "") {

    if (!element) return;

    element.textContent = message;
    element.className = "message " + type;
}


// =========================================
// 6. USER INFORMATION
// =========================================

function loadTelegramProfile() {

    if (!tg?.initDataUnsafe?.user) {
        elements.userName.textContent = "Project 44 Member";
        elements.userAvatar.textContent = "P44";
        return;
    }

    const user = tg.initDataUnsafe.user;

    const fullName = [
        user.first_name,
        user.last_name
    ].filter(Boolean).join(" ");

    elements.userName.textContent = fullName || "Member";

    elements.userAvatar.textContent =
        (user.first_name || "P").charAt(0).toUpperCase();
}


// =========================================
// 7. LOAD USER DASHBOARD
// =========================================

async function loadDashboard() {

    try {

        const data = await apiRequest("/api/status");

        elements.pointsBalance.innerHTML =
            `${Number(data.points || 0).toLocaleString()} <small>PTS</small>`;

        elements.completedAds.textContent =
            data.completed_ads ?? 0;

        if (data.rates) {
            window.project44Rates = data.rates;
            updateEstimatedValue();
        }

    } catch (error) {

        console.error("Dashboard error:", error);

    }
}


// =========================================
// 8. ADSGRAM INITIALIZATION
// =========================================

let AdController = null;

function initializeAdsGram() {

    if (ADSGRAM_BLOCK_ID === "YOUR_BLOCK_ID") {
        console.warn("AdsGram Block ID has not been added yet.");
        return;
    }

    if (!window.Adsgram) {
        console.error("AdsGram SDK did not load.");
        return;
    }

    try {

        AdController = window.Adsgram.init({
            blockId: ADSGRAM_BLOCK_ID
        });

    } catch (error) {

        console.error("AdsGram initialization failed:", error);

    }
}


// =========================================
// 9. WATCH REWARDED AD
// =========================================

async function watchAd() {

    if (!getTelegramInitData()) {
        showMessage(
            elements.adMessage,
            "Open Project 44 through Telegram to continue.",
            "error"
        );
        return;
    }

    if (!AdController) {
        showMessage(
            elements.adMessage,
            "Rewarded ads are not configured yet.",
            "error"
        );
        return;
    }

    elements.watchAdBtn.disabled = true;

    showMessage(
        elements.adMessage,
        "Loading advertisement..."
    );

    try {

        // Ask backend to create an ad session.
        const session = await apiRequest("/api/ad/start", {
            method: "POST",
            body: JSON.stringify({})
        });

        if (!session.session_id) {
            throw new Error("Could not create an ad session.");
        }

        showMessage(
            elements.adMessage,
            "Please watch the advertisement to the end."
        );

        const result = await AdController.show();

        if (!result || result.done !== true) {
            throw new Error("The advertisement was not completed.");
        }

        // Report completion to backend.
        // Backend must validate provider evidence before crediting points.
        const completion = await apiRequest("/api/ad/complete", {
            method: "POST",
            body: JSON.stringify({
                session_id: session.session_id,
                provider: "adsgram",
                client_completed: true
            })
        });

        if (completion.credited === true) {

            showMessage(
                elements.adMessage,
                "Reward verified! Your points have been added.",
                "success"
            );

            await loadDashboard();

        } else {

            showMessage(
                elements.adMessage,
                completion.message ||
                "Ad completion received. Your reward is awaiting verification."
            );

        }

    } catch (error) {

        console.error("Ad error:", error);

        showMessage(
            elements.adMessage,
            error.message || "Advertisement skipped or unavailable.",
            "error"
        );

    } finally {

        elements.watchAdBtn.disabled = false;

    }
}


// =========================================
// 10. LOAD REFERRAL INFORMATION
// =========================================

async function loadReferrals() {

    try {

        const data = await apiRequest("/api/referrals");

        elements.referralLink.textContent =
            data.referral_link || "Referral link unavailable";

        elements.referralCount.textContent =
            data.verified_referrals ?? 0;

    } catch (error) {

        console.error("Referral error:", error);

        elements.referralLink.textContent =
            "Unable to load referral link.";

    }

}


// =========================================
// 11. COPY REFERRAL LINK
// =========================================

async function copyReferralLink() {

    const link = elements.referralLink.textContent;

    if (!link || link.includes("unavailable") ||
        link.includes("Loading")) {
        showMessage(
            elements.referralMessage,
            "Referral link is not available.",
            "error"
        );
        return;
    }

    try {

        await navigator.clipboard.writeText(link);

        showMessage(
            elements.referralMessage,
            "Referral link copied!",
            "success"
        );

    } catch {

        showMessage(
            elements.referralMessage,
            "Unable to copy. Please copy the link manually.",
            "error"
        );

    }

}


// =========================================
// 12. INVITE FRIENDS
// =========================================

function inviteFriends() {

    const link = elements.referralLink.textContent;

    if (!link || link.includes("unavailable") ||
        link.includes("Loading")) {
        showMessage(
            elements.referralMessage,
            "Referral link is not ready.",
            "error"
        );
        return;
    }

    const message =
        "Join me on Project 44! Earn rewards through verified activities.\n\n" +
        link;

    if (tg) {

        tg.openTelegramLink(
            "https://t.me/share/url?url=" +
            encodeURIComponent(link) +
            "&text=" +
            encodeURIComponent("Join me on Project 44!")
        );

    } else {

        window.open(
            "https://t.me/share/url?url=" +
            encodeURIComponent(link) +
            "&text=" +
            encodeURIComponent("Join me on Project 44!"),
            "_blank"
        );

    }

}


// =========================================
// 13. WITHDRAWAL ASSET FIELDS
// =========================================

function updateAssetFields() {

    const asset = elements.withdrawAsset.value;

    const isGift = asset === "GIFT";

    elements.walletFields.hidden = isGift;
    elements.giftFields.hidden = !isGift;

    if (!isGift) {
        elements.network.value = "TON";
    }

    updateEstimatedValue();

}


// =========================================
// 14. ESTIMATE WITHDRAWAL VALUE
// =========================================

function updateEstimatedValue() {

    const points = Number(elements.withdrawPoints.value) || 0;

    const rates = window.project44Rates || {};

    const usdPerPoint = Number(rates.usd_per_point ?? 0.0026);

    const value = points * usdPerPoint;

    elements.estimatedValue.textContent =
        "$" + value.toFixed(2);

}


// =========================================
// 15. SUBMIT WITHDRAWAL
// =========================================

async function submitWithdrawal() {

    const asset = elements.withdrawAsset.value;

    const points = Number(elements.withdrawPoints.value);

    const isGift = asset === "GIFT";

    if (!getTelegramInitData()) {
        showMessage(
            elements.withdrawMessage,
            "Open Project 44 through Telegram first.",
            "error"
        );
        return;
    }

    if (!Number.isInteger(points) || points < 500 || points > 2000) {

        showMessage(
            elements.withdrawMessage,
            "Enter a whole number between 500 and 2,000 points.",
            "error"
        );

        return;
    }

    if (isGift && !elements.giftTelegram.value.trim()) {

        showMessage(
            elements.withdrawMessage,
            "Enter the Telegram recipient username or ID.",
            "error"
        );

        return;
    }

    if (!isGift && !elements.walletAddress.value.trim()) {

        showMessage(
            elements.withdrawMessage,
            "Enter your TON wallet address.",
            "error"
        );

        return;
    }

    const payload = {
        asset: asset,
        points: points,
        network: isGift ? null : "TON",
        wallet_address: isGift
            ? null
            : elements.walletAddress.value.trim(),
        gift_telegram: isGift
            ? elements.giftTelegram.value.trim()
            : null
    };

    elements.withdrawBtn.disabled = true;

    showMessage(
        elements.withdrawMessage,
        "Submitting your withdrawal request..."
    );

    try {

        const result = await apiRequest("/api/withdrawals", {
            method: "POST",
            body: JSON.stringify(payload)
        });

        showMessage(
            elements.withdrawMessage,
            result.message ||
            "Your withdrawal request has been received.",
            "success"
        );

        elements.withdrawPoints.value = "";

        await loadDashboard();
        await loadWithdrawalHistory();

    } catch (error) {

        showMessage(
            elements.withdrawMessage,
            error.message || "Withdrawal request failed.",
            "error"
        );

    } finally {

        elements.withdrawBtn.disabled = false;

    }

}


// =========================================
// 16. WITHDRAWAL HISTORY
// =========================================

async function loadWithdrawalHistory() {

    try {

        const data = await apiRequest("/api/withdrawals");

        const withdrawals = Array.isArray(data)
            ? data
            : data.withdrawals || [];

        elements.withdrawalHistory.innerHTML = "";

        if (withdrawals.length === 0) {

            elements.withdrawalHistory.innerHTML =
                '<p class="empty-state">No withdrawal requests yet.</p>';

            return;
        }

        withdrawals.forEach(item => {

            const card = document.createElement("div");

            card.className = "history-item";

            const title = document.createElement("h3");

            title.textContent =
                `${item.asset || "Reward"} • ${Number(item.points || 0).toLocaleString()} points`;

            const status = document.createElement("p");

            status.textContent =
                "Status: " + (item.status || "Pending");

            const request = document.createElement("p");

            request.textContent =
                "Request ID: " + (item.request_id || "Unavailable");

            card.appendChild(title);
            card.appendChild(status);
            card.appendChild(request);

            elements.withdrawalHistory.appendChild(card);

        });

    } catch (error) {

        console.error("Withdrawal history error:", error);

        elements.withdrawalHistory.innerHTML =
            '<p class="empty-state">Unable to load withdrawal history.</p>';

    }

}


// =========================================
// 17. BUTTON EVENTS
// =========================================

elements.watchAdBtn.addEventListener(
    "click",
    watchAd
);

elements.copyReferralBtn.addEventListener(
    "click",
    copyReferralLink
);

elements.inviteBtn.addEventListener(
    "click",
    inviteFriends
);

elements.withdrawAsset.addEventListener(
    "change",
    updateAssetFields
);

elements.withdrawPoints.addEventListener(
    "input",
    updateEstimatedValue
);

elements.withdrawBtn.addEventListener(
    "click",
    submitWithdrawal
);


// =========================================
// 18. APPLICATION STARTUP
// =========================================

async function initializeProject44() {

    loadTelegramProfile();

    updateAssetFields();

    initializeAdsGram();

    await Promise.all([
        loadDashboard(),
        loadReferrals(),
        loadWithdrawalHistory()
    ]);

}

document.addEventListener(
    "DOMContentLoaded",
    initializeProject44
);
                                 
