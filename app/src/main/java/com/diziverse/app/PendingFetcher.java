package com.diziverse.app;

import android.os.Handler;
import android.os.Looper;

import org.json.JSONArray;
import org.json.JSONObject;

import java.util.ArrayList;
import java.util.List;

/**
 * Fetches the pending-video list that the watcher publishes to GitHub
 * (pending.json next to url.json). Each item carries its title, so no
 * per-video lookup is needed.
 */
public class PendingFetcher {

    private static final String PENDING_URL =
        "https://raw.githubusercontent.com/renalong89-max/diziverse-dashboard-app/main/pending.json";

    private static final Handler UI = new Handler(Looper.getMainLooper());

    public static class Item {
        public final String title;
        public final String channel;
        public final String addedUtc;

        Item(String title, String channel, String addedUtc) {
            this.title = title;
            this.channel = channel;
            this.addedUtc = addedUtc;
        }
    }

    public interface Callback {
        void onResult(List<Item> items);
    }

    /** Fetch pending list in background; callback on UI thread (empty list on failure). */
    public static void fetch(final Callback cb) {
        new Thread(() -> {
            final List<Item> out = new ArrayList<>();
            try {
                String body = Api.get(PENDING_URL);
                JSONArray arr = new JSONObject(body).optJSONArray("items");
                if (arr != null) {
                    for (int i = 0; i < arr.length(); i++) {
                        JSONObject o = arr.optJSONObject(i);
                        if (o == null) continue;
                        String t = o.optString("title", "").trim();
                        if (t.isEmpty()) continue;
                        out.add(new Item(t,
                                o.optString("channel", ""),
                                o.optString("added_utc", "")));
                    }
                }
            } catch (Exception ignored) {
            }
            UI.post(() -> cb.onResult(out));
        }).start();
    }
}
