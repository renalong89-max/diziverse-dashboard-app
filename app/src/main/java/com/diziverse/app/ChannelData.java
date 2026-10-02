package com.diziverse.app;

import org.json.JSONArray;
import org.json.JSONObject;

import java.util.ArrayList;
import java.util.List;

/** One channel's state from /api/status. Tolerant of missing/null fields. */
public class ChannelData {
    public String id = "";
    public String label = "";
    public boolean running = false;
    public Integer progressPct = null;
    public String stage = "";
    public List<String> queue = new ArrayList<>();
    public List<String> done = new ArrayList<>();
    public List<String> failed = new ArrayList<>();

    public static ChannelData fromJson(String id, JSONObject o) {
        ChannelData d = new ChannelData();
        d.id = id;
        d.label = o.optString("label", id);
        d.running = o.optBoolean("running", false);
        d.progressPct = o.isNull("progress_pct") ? null : o.optInt("progress_pct");
        d.stage = o.optString("stage", "");
        d.queue = toList(o.optJSONArray("queue"));
        d.done = toList(o.optJSONArray("done"));
        d.failed = toList(o.optJSONArray("failed"));
        return d;
    }

    private static List<String> toList(JSONArray a) {
        List<String> l = new ArrayList<>();
        if (a == null) return l;
        for (int i = 0; i < a.length(); i++) {
            String s = a.optString(i, null);
            if (s != null) l.add(s);
        }
        return l;
    }
}
