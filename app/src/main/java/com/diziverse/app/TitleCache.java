package com.diziverse.app;

import android.content.Context;
import android.content.SharedPreferences;
import android.os.Handler;
import android.os.Looper;

import org.json.JSONObject;

import java.util.HashMap;
import java.util.Map;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/**
 * Resolves YouTube video IDs to titles.
 *
 * Titles are cached in SharedPreferences (video_id -> title) so each video
 * is looked up only once. Uncached titles are fetched from YouTube's free
 * oEmbed endpoint in the background; the callback fires on the UI thread
 * when the title arrives (or immediately if cached).
 */
public class TitleCache {

    private static final String PREFS = "diziverse_titles";
    private static final Pattern ID_PATTERNS[] = {
        Pattern.compile("youtu\\.be/([A-Za-z0-9_-]{11})"),
        Pattern.compile("[?&]v=([A-Za-z0-9_-]{11})"),
        Pattern.compile("/embed/([A-Za-z0-9_-]{11})"),
        Pattern.compile("/shorts/([A-Za-z0-9_-]{11})"),
    };

    private static final Handler UI = new Handler(Looper.getMainLooper());
    // in-flight guard: one network fetch per video id at a time
    private static final Map<String, Boolean> fetching = new HashMap<>();

    public interface Callback {
        void onTitle(String videoId, String title);
    }

    /** Extract the 11-char video id from any YouTube URL. Null if none. */
    public static String videoIdOf(String url) {
        if (url == null) return null;
        for (Pattern p : ID_PATTERNS) {
            Matcher m = p.matcher(url);
            if (m.find()) return m.group(1);
        }
        return null;
    }

    /** Cached title, or null if not resolved yet. */
    public static String getCached(Context ctx, String videoId) {
        if (videoId == null) return null;
        SharedPreferences sp = ctx.getSharedPreferences(PREFS, Context.MODE_PRIVATE);
        String t = sp.getString(videoId, null);
        return (t == null || t.isEmpty()) ? null : t;
    }

    private static void putCached(Context ctx, String videoId, String title) {
        if (videoId == null || title == null || title.isEmpty()) return;
        ctx.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
                .edit().putString(videoId, title).apply();
    }

    /**
     * Resolve a video id to its title. Calls back on the UI thread with the
     * cached title immediately, otherwise fetches via oEmbed and calls back
     * again when it arrives. If the fetch fails, falls back to the video id.
     */
    public static void resolve(final Context ctx, final String videoId,
                               final Callback cb) {
        if (videoId == null) return;
        String cached = getCached(ctx, videoId);
        if (cached != null) {
            cb.onTitle(videoId, cached);
            return;
        }
        synchronized (fetching) {
            if (fetching.containsKey(videoId)) return; // already fetching
            fetching.put(videoId, Boolean.TRUE);
        }
        new Thread(() -> {
            String title = null;
            try {
                String api = "https://www.youtube.com/oembed?url="
                        + java.net.URLEncoder.encode(
                                "https://www.youtube.com/watch?v=" + videoId, "UTF-8")
                        + "&format=json";
                String body = Api.get(api);
                title = new JSONObject(body).optString("title", null);
                if (title != null && !title.isEmpty()) {
                    putCached(ctx.getApplicationContext(), videoId, title);
                }
            } catch (Exception ignored) {
            } finally {
                synchronized (fetching) {
                    fetching.remove(videoId);
                }
            }
            final String out = (title == null || title.isEmpty()) ? videoId : title;
            UI.post(() -> cb.onTitle(videoId, out));
        }).start();
    }

    /** Resolve a full URL: extracts the id, then resolves. */
    public static void resolveUrl(final Context ctx, final String url,
                                  final Callback cb) {
        String vid = videoIdOf(url);
        if (vid == null) {
            // Not a recognizable YouTube URL — show as-is.
            cb.onTitle(null, url);
            return;
        }
        resolve(ctx, vid, cb);
    }
}
