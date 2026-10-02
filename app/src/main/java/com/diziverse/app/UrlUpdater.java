package com.diziverse.app;

import android.content.Context;
import org.json.JSONObject;

/**
 * Auto-updates the backend server URL when the Cloudflare tunnel domain changes.
 *
 * The agent keeps https://raw.githubusercontent.com/diziverse-app/dashboard/main/url.json
 * updated with the current tunnel URL. On every app launch, we fetch it; if the
 * URL differs from the stored one (and validates live), we switch automatically —
 * the user never has to paste a new link.
 */
public class UrlUpdater {

    private static final String CONFIG_URL =
        "https://raw.githubusercontent.com/diziverse-app/dashboard/main/url.json";

    /** Result of an update check. */
    public static class Result {
        public final boolean updated;
        public final String url;      // current effective URL (may be unchanged)
        public final String error;    // non-null if something went wrong

        Result(boolean updated, String url, String error) {
            this.updated = updated;
            this.url = url;
            this.error = error;
        }
    }

    /**
     * Check for a new server URL. Call off the main thread.
     * Returns the effective URL to use (updated or existing).
     */
    public static Result checkForUpdate(Context ctx) {
        String stored = Prefs.getServerUrl(ctx);
        String remote = fetchRemoteUrl();
        if (remote == null) {
            // Could not reach the config endpoint — keep using stored URL.
            return new Result(false, stored, "config-unreachable");
        }
        if (remote.equals(stored)) {
            return new Result(false, stored, null);
        }
        // Validate the new URL live before switching.
        if (validateUrl(remote)) {
            Prefs.setServerUrl(ctx, remote);
            return new Result(true, remote, null);
        }
        return new Result(false, stored, "remote-invalid");
    }

    /** Fetch the URL string from the stable JSON endpoint. Null on failure. */
    private static String fetchRemoteUrl() {
        try {
            String body = Api.get(CONFIG_URL);
            String url = new JSONObject(body).optString("url", "").trim();
            if (url.startsWith("https://") && Prefs.rootOf(url) != null
                    && Prefs.keyOf(url) != null) {
                return url;
            }
            return null;
        } catch (Exception e) {
            return null;
        }
    }

    /** Live-validate: /api/status must return JSON with ch2. */
    private static boolean validateUrl(String url) {
        try {
            String root = Prefs.rootOf(url);
            String key = Prefs.keyOf(url);
            if (root == null || key == null) return false;
            String json = Api.get(root + "/api/status?key=" +
                java.net.URLEncoder.encode(key, "UTF-8"));
            return new JSONObject(json).getJSONObject("channels").has("ch2");
        } catch (Exception e) {
            return false;
        }
    }
}
