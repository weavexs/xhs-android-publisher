package com.codex.xhsinput;

import android.content.BroadcastReceiver;
import android.content.ClipData;
import android.content.ClipboardManager;
import android.content.Context;
import android.content.Intent;
import android.util.Base64;

import java.nio.charset.StandardCharsets;

public final class ClipboardReceiver extends BroadcastReceiver {
    @Override
    public void onReceive(Context context, Intent intent) {
        String encoded = intent.getStringExtra("text_b64");
        if (encoded == null) {
            return;
        }
        byte[] bytes = Base64.decode(encoded, Base64.DEFAULT);
        String text = new String(bytes, StandardCharsets.UTF_8);
        ClipboardManager clipboard =
                (ClipboardManager) context.getSystemService(Context.CLIPBOARD_SERVICE);
        clipboard.setPrimaryClip(ClipData.newPlainText("xhs-operator", text));
    }
}
