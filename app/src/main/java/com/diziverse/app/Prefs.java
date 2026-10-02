package com.diziverse.app;

import android.content.Context;
import android.net.Uri;

/** Stores the server link (root + key) in SharedPreferences. Never hardcoded. */
public class Prefs {
    private static final String NAME = "diziverse_prefs";
    private static final String K_URL = "server_url";

    public static String getServerUrl(Context c) {
        return c.getSharedPreferences(NAME, Context.MODE_PRIVATE).getString(K_URL, null);
    }

    public static void setServerUrl(Context c, String url) {
        c.getSharedPreferences(NAME, Context.MODE_PRIVATE).edit().putString(K_URL, url).apply();
    }

    /** "https://host" part of a pasted server link. */
    public static String rootOf(String url) {
        if (url == null) return null;
        Uri u = Uri.parse(url.trim());
        if (u.getScheme() == null || u.getHost() == null) return null;
        return u.getScheme() + "://" + u.getHost();
    }

    /** The ?key= value of a pasted server link. */
    public static String keyOf(String url) {
        if (url == null) return null;
        return Uri.parse(url.trim()).getQueryParameter("key");
    }

    public static String root(Context c) {
        return rootOf(getServerUrl(c));
    }

    public static String key(Context c) {
        return keyOf(getServerUrl(c));
    }
}
