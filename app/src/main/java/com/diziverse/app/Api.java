package com.diziverse.app;

import java.io.ByteArrayOutputStream;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.net.URLDecoder;
import java.net.URLEncoder;
import java.util.Map;

/** Minimal HTTP client: HttpURLConnection only, zero dependencies. */
public class Api {
    private static final int TIMEOUT = 25000;

    /** GET, returns body as UTF-8 string. Throws on non-2xx. */
    public static String get(String urlStr) throws Exception {
        HttpURLConnection c = (HttpURLConnection) new URL(urlStr).openConnection();
        c.setConnectTimeout(TIMEOUT);
        c.setReadTimeout(TIMEOUT);
        c.setRequestProperty("User-Agent", "DiziVerse-App/1.0");
        int code = c.getResponseCode();
        InputStream in = code >= 400 ? c.getErrorStream() : c.getInputStream();
        String body = readAll(in);
        c.disconnect();
        if (code < 200 || code >= 300) throw new Exception("HTTP " + code);
        return body;
    }

    /**
     * POST form fields with redirects DISABLED (server answers 303).
     * Returns the Location header value (may be null).
     */
    public static String postNoRedirect(String urlStr, Map<String, String> fields) throws Exception {
        HttpURLConnection c = (HttpURLConnection) new URL(urlStr).openConnection();
        c.setInstanceFollowRedirects(false);
        c.setConnectTimeout(TIMEOUT);
        c.setReadTimeout(TIMEOUT);
        c.setRequestMethod("POST");
        c.setDoOutput(true);
        c.setRequestProperty("User-Agent", "DiziVerse-App/1.0");
        c.setRequestProperty("Content-Type", "application/x-www-form-urlencoded");

        StringBuilder sb = new StringBuilder();
        for (Map.Entry<String, String> e : fields.entrySet()) {
            if (sb.length() > 0) sb.append('&');
            sb.append(URLEncoder.encode(e.getKey(), "UTF-8"));
            sb.append('=');
            sb.append(URLEncoder.encode(e.getValue() == null ? "" : e.getValue(), "UTF-8"));
        }
        byte[] data = sb.toString().getBytes("UTF-8");
        c.setRequestProperty("Content-Length", String.valueOf(data.length));
        OutputStream out = c.getOutputStream();
        out.write(data);
        out.flush();
        out.close();

        int code = c.getResponseCode();
        String loc = c.getHeaderField("Location");
        // Drain body so the connection can be reused/closed cleanly.
        try {
            InputStream in = code >= 400 ? c.getErrorStream() : c.getInputStream();
            if (in != null) readAll(in);
        } catch (Exception ignored) {
        }
        c.disconnect();
        return loc;
    }

    /** Extracts the msg query param from a redirect Location. */
    public static String extractMsg(String location) {
        if (location == null) return null;
        int q = location.indexOf('?');
        String query = q >= 0 ? location.substring(q + 1) : location;
        for (String part : query.split("&")) {
            int eq = part.indexOf('=');
            if (eq > 0 && part.substring(0, eq).equals("msg")) {
                String v = part.substring(eq + 1);
                try {
                    return URLDecoder.decode(v, "UTF-8");
                } catch (Exception e) {
                    return v;
                }
            }
        }
        return null;
    }

    /** Server msg codes -> Roman Urdu. */
    public static String trMsg(String msg) {
        if (msg == null) return "Kuch garbar hui — dobara try karein.";
        switch (msg) {
            case "ok":
                return "Ho gaya! Link queue mein daal diya.";
            case "bad":
                return "Ye YouTube link nahi lag raha.";
            case "notoken":
                return "Channel ka YouTube connect nahi hai.";
            case "dup":
                return "Ye link pehle se queue mein hai.";
            case "alreadydone":
                return "Ye video pehle hi upload ho chuki hai.";
            case "gone":
                return "Ye video YouTube par nahi mili.";
            case "unplayable":
                return "Ye video server se download nahi ho sakti.";
            case "badcookies":
                return "Server cookies expire — refresh chahiye.";
            case "retry":
                return "Ho gaya! Dobara queue mein daal diya.";
            default:
                return "Server: " + msg;
        }
    }

    private static String readAll(InputStream in) throws Exception {
        ByteArrayOutputStream bos = new ByteArrayOutputStream();
        byte[] buf = new byte[8192];
        int n;
        while ((n = in.read(buf)) != -1) bos.write(buf, 0, n);
        in.close();
        return bos.toString("UTF-8");
    }
}
