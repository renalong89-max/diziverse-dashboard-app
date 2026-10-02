package com.diziverse.app;

import android.app.Activity;
import android.content.Intent;
import android.os.Bundle;
import android.widget.Button;
import android.widget.EditText;
import android.widget.TextView;

import org.json.JSONObject;

import java.net.URLEncoder;

/** First-run screen: paste the server link, validated live via /api/status. */
public class SetupActivity extends Activity {

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        if (Prefs.getServerUrl(this) != null) {
            goMain();
            return;
        }
        setContentView(R.layout.activity_setup);

        EditText etUrl = findViewById(R.id.etUrl);
        TextView tvMsg = findViewById(R.id.tvMsg);
        Button btn = findViewById(R.id.btnConnect);

        btn.setOnClickListener(v -> {
            String url = etUrl.getText().toString().trim();
            String root = Prefs.rootOf(url);
            String key = Prefs.keyOf(url);
            if (root == null || key == null || key.isEmpty()) {
                tvMsg.setText(R.string.connect_bad_link);
                return;
            }
            tvMsg.setText(R.string.checking);
            btn.setEnabled(false);
            new Thread(() -> {
                try {
                    String json = Api.get(root + "/api/status?key=" + URLEncoder.encode(key, "UTF-8"));
                    JSONObject channels = new JSONObject(json).getJSONObject("channels");
                    if (!channels.has("ch2")) throw new Exception("no ch2");
                    Prefs.setServerUrl(SetupActivity.this, url);
                    runOnUiThread(this::goMain);
                } catch (Exception e) {
                    runOnUiThread(() -> {
                        tvMsg.setText(R.string.connect_fail);
                        btn.setEnabled(true);
                    });
                }
            }).start();
        });
    }

    private void goMain() {
        startActivity(new Intent(this, MainActivity.class));
        finish();
    }
}
