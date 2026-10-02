package com.diziverse.app;

import android.app.Activity;
import android.app.AlertDialog;
import android.content.Intent;
import android.graphics.Color;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.text.util.Linkify;
import android.view.Gravity;
import android.widget.Button;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.TextView;

import org.json.JSONObject;

import java.net.URLEncoder;
import java.util.HashMap;
import java.util.Map;

/** Main dashboard: status, submit, queue/done/failed, settings. */
public class MainActivity extends Activity {

    private String channelId = "ch2"; // Channel 1 is the default
    private final Handler handler = new Handler(Looper.getMainLooper());
    private final Runnable autoRefresh = new Runnable() {
        @Override
        public void run() {
            refresh();
            handler.postDelayed(this, 30000);
        }
    };

    private TextView tvChTitle, tvStatus, tvSubmitMsg;
    private EditText etLink;
    private LinearLayout llQueue, llDone, llFailed;
    private Button btnCh1, btnCh2;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        if (Prefs.getServerUrl(this) == null) {
            // No stored URL yet — try auto-fetch from the stable endpoint first.
            // If that works, skip the manual setup screen entirely.
            new Thread(() -> {
                UrlUpdater.Result r = UrlUpdater.checkForUpdate(this);
                runOnUiThread(() -> {
                    if (r.url != null) {
                        initMain();
                    } else {
                        startActivity(new Intent(this, SetupActivity.class));
                        finish();
                    }
                });
            }).start();
            return;
        }
        // Stored URL exists — check for tunnel-domain change in background,
        // then init. The UI loads immediately with the stored URL.
        new Thread(() -> {
            UrlUpdater.Result r = UrlUpdater.checkForUpdate(this);
            if (r.updated) {
                runOnUiThread(() -> refresh());
            }
        }).start();
        initMain();
    }

    private void initMain() {
        setContentView(R.layout.activity_main);

        tvChTitle = findViewById(R.id.tvChTitle);
        tvStatus = findViewById(R.id.tvStatus);
        tvSubmitMsg = findViewById(R.id.tvSubmitMsg);
        etLink = findViewById(R.id.etLink);
        llQueue = findViewById(R.id.llQueue);
        llDone = findViewById(R.id.llDone);
        llFailed = findViewById(R.id.llFailed);
        btnCh1 = findViewById(R.id.btnCh1);
        btnCh2 = findViewById(R.id.btnCh2);

        btnCh1.setOnClickListener(v -> switchChannel("ch2"));
        btnCh2.setOnClickListener(v -> switchChannel("ch3"));
        findViewById(R.id.btnRefresh).setOnClickListener(v -> refresh());
        findViewById(R.id.btnSubmit).setOnClickListener(v -> submit());
        findViewById(R.id.btnSettings).setOnClickListener(v -> openSettings());

        updateChannelButtons();
    }

    @Override
    protected void onResume() {
        super.onResume();
        refresh();
        handler.postDelayed(autoRefresh, 30000);
    }

    @Override
    protected void onPause() {
        super.onPause();
        handler.removeCallbacks(autoRefresh);
    }

    private void switchChannel(String id) {
        if (!channelId.equals(id)) {
            channelId = id;
            updateChannelButtons();
            refresh();
        }
    }

    private void updateChannelButtons() {
        boolean isCh2 = channelId.equals("ch2");
        btnCh1.setEnabled(!isCh2);
        btnCh2.setEnabled(isCh2);
        btnCh1.setAlpha(isCh2 ? 1f : 0.55f);
        btnCh2.setAlpha(isCh2 ? 0.55f : 1f);
    }

    /** Fetch /api/status and render the selected channel. */
    private void refresh() {
        final String root = Prefs.root(this);
        final String key = Prefs.key(this);
        if (root == null || key == null) {
            tvStatus.setText(R.string.conn_fail);
            return;
        }
        new Thread(() -> {
            try {
                String json = Api.get(root + "/api/status?key=" + URLEncoder.encode(key, "UTF-8"));
                JSONObject channels = new JSONObject(json).getJSONObject("channels");
                if (!channels.has(channelId)) throw new Exception("no channel");
                final ChannelData d = ChannelData.fromJson(channelId, channels.getJSONObject(channelId));
                runOnUiThread(() -> render(d));
            } catch (Exception e) {
                runOnUiThread(() -> {
                    tvStatus.setText(R.string.conn_fail);
                    tvStatus.setTextColor(Color.parseColor("#C62828"));
                });
            }
        }).start();
    }

    private void render(ChannelData d) {
        tvChTitle.setText(d.label + " — Status");

        if (d.running) {
            String t = getString(R.string.status_running);
            if (!d.stage.isEmpty()) t += ": " + d.stage;
            if (d.progressPct != null) t += " (" + d.progressPct + "%)";
            tvStatus.setText(t);
            tvStatus.setTextColor(Color.parseColor("#E65100"));
        } else {
            String t = getString(R.string.status_free);
            if (!d.stage.isEmpty()) t += " — " + d.stage;
            tvStatus.setText(t);
            tvStatus.setTextColor(Color.parseColor("#2E7D32"));
        }

        fillSimpleList(llQueue, d.queue, R.string.empty_queue, false);
        fillSimpleList(llDone, d.done, R.string.empty_done, true);
        fillFailedList(llFailed, d.failed);
    }

    private void fillSimpleList(LinearLayout ll, java.util.List<String> items, int emptyRes, boolean linkify) {
        ll.removeAllViews();
        if (items.isEmpty()) {
            ll.addView(makeText(getString(emptyRes), 13, "#757575"));
            return;
        }
        for (String s : items) {
            TextView tv = makeText(s, 13, "#212121");
            tv.setPadding(0, 6, 0, 6);
            if (linkify) {
                tv.setAutoLinkMask(Linkify.WEB_URLS);
                tv.setLinksClickable(true);
            }
            ll.addView(tv);
        }
    }

    private void fillFailedList(LinearLayout ll, java.util.List<String> items) {
        ll.removeAllViews();
        if (items.isEmpty()) {
            ll.addView(makeText(getString(R.string.empty_failed), 13, "#757575"));
            return;
        }
        for (String link : items) {
            LinearLayout row = new LinearLayout(this);
            row.setOrientation(LinearLayout.HORIZONTAL);
            row.setGravity(Gravity.CENTER_VERTICAL);
            row.setPadding(0, 4, 0, 4);

            TextView tv = makeText(link, 13, "#212121");
            LinearLayout.LayoutParams lp = new LinearLayout.LayoutParams(
                    0, LinearLayout.LayoutParams.WRAP_CONTENT, 1f);
            tv.setLayoutParams(lp);
            row.addView(tv);

            Button retry = new Button(this);
            retry.setText(R.string.retry);
            retry.setOnClickListener(v -> retryLink(link));
            row.addView(retry);

            ll.addView(row);
        }
    }

    private TextView makeText(String s, int sp, String color) {
        TextView tv = new TextView(this);
        tv.setText(s);
        tv.setTextSize(sp);
        tv.setTextColor(Color.parseColor(color));
        return tv;
    }

    /** Submit a YouTube link to the queue. */
    private void submit() {
        final String link = etLink.getText().toString().trim();
        if (link.isEmpty()) {
            tvSubmitMsg.setText(R.string.submit_empty);
            return;
        }
        final String root = Prefs.root(this);
        final String key = Prefs.key(this);
        if (root == null || key == null) {
            tvSubmitMsg.setText(R.string.conn_fail);
            return;
        }
        tvSubmitMsg.setText(R.string.submit_sending);
        tvSubmitMsg.setTextColor(Color.parseColor("#757575"));
        new Thread(() -> {
            try {
                Map<String, String> f = new HashMap<>();
                f.put("key", key);
                f.put("ch", channelId);
                f.put("link", link);
                String loc = Api.postNoRedirect(root + "/submit", f);
                final String out = Api.trMsg(Api.extractMsg(loc));
                runOnUiThread(() -> {
                    tvSubmitMsg.setText(out);
                    tvSubmitMsg.setTextColor(Color.parseColor("#212121"));
                    etLink.setText("");
                });
                refresh();
            } catch (Exception e) {
                runOnUiThread(() -> tvSubmitMsg.setText(R.string.submit_fail));
            }
        }).start();
    }

    /** Retry one failed link. */
    private void retryLink(String link) {
        final String root = Prefs.root(this);
        final String key = Prefs.key(this);
        if (root == null || key == null) return;
        tvSubmitMsg.setText(R.string.submit_sending);
        new Thread(() -> {
            try {
                Map<String, String> f = new HashMap<>();
                f.put("key", key);
                f.put("ch", channelId);
                f.put("link", link);
                String loc = Api.postNoRedirect(root + "/retry", f);
                final String out = Api.trMsg(Api.extractMsg(loc));
                runOnUiThread(() -> tvSubmitMsg.setText(out));
                refresh();
            } catch (Exception e) {
                runOnUiThread(() -> tvSubmitMsg.setText(R.string.submit_fail));
            }
        }).start();
    }

    /** Settings dialog: update the server link (re-validated live). */
    private void openSettings() {
        final EditText et = new EditText(this);
        et.setText(Prefs.getServerUrl(this));
        et.setInputType(android.text.InputType.TYPE_TEXT_VARIATION_URI);
        et.setHint(R.string.setup_hint);
        int pad = (int) (16 * getResources().getDisplayMetrics().density);
        et.setPadding(pad, pad, pad, pad);

        new AlertDialog.Builder(this)
                .setTitle(R.string.settings_title)
                .setView(et)
                .setPositiveButton(R.string.save, (d, w) -> {
                    final String url = et.getText().toString().trim();
                    final String root = Prefs.rootOf(url);
                    final String key = Prefs.keyOf(url);
                    if (root == null || key == null || key.isEmpty()) {
                        tvSubmitMsg.setText(R.string.connect_bad_link);
                        return;
                    }
                    tvSubmitMsg.setText(R.string.checking);
                    new Thread(() -> {
                        try {
                            String json = Api.get(root + "/api/status?key=" + URLEncoder.encode(key, "UTF-8"));
                            JSONObject channels = new JSONObject(json).getJSONObject("channels");
                            if (!channels.has("ch2")) throw new Exception("no ch2");
                            Prefs.setServerUrl(MainActivity.this, url);
                            runOnUiThread(() -> {
                                tvSubmitMsg.setText("Save ho gaya.");
                                refresh();
                            });
                        } catch (Exception e) {
                            runOnUiThread(() -> tvSubmitMsg.setText(R.string.connect_fail));
                        }
                    }).start();
                })
                .setNegativeButton(R.string.cancel, null)
                .show();
    }
}
